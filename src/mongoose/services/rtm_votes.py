"""RTM member votes: the director opens a vote, RTM members (and directors, who are members by
definition) cast one choice per flat, and everyone sees the tally.

Mongoose is the ledger and source of truth for the vote record and the tally. Because Mongoose sits
behind CGNAT and WordPress cannot reach it, the flow is pull-based and mirrors the claims/issues path:
    - the director creates/closes votes here (Cockpit),
    - open votes ride out in the portal snapshot so WordPress can show them to member flats,
    - members cast on WordPress, which stores the cast in its own table,
    - Mongoose PULLS the casts, validates each against the persisted membership flag, records one
      cast per flat, and the refreshed tally rides back out in the next snapshot.

Eligibility is enforced HERE, server-side, against rtm_members.member_flat_ids(): a cast from a flat
that is not a current RTM member is rejected, never tallied. One cast per flat (flat_id is half the
primary key); a re-cast updates the existing choice rather than double-counting.
"""
from __future__ import annotations

import json
from typing import Any


def _ensure() -> None:
    from ..db import connect
    with connect() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS rtm_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                options_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT,
                closes_at TEXT,
                closed_at TEXT
            )"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS rtm_vote_casts (
                vote_id INTEGER NOT NULL,
                flat_id TEXT NOT NULL,
                choice TEXT NOT NULL,
                cast_at TEXT,
                PRIMARY KEY (vote_id, flat_id)
            )"""
        )
        con.commit()


def _norm_flat(flat_id: str) -> str:
    return (flat_id or "").strip().upper().replace("-", "")


def _vote_row(con, vote_id: int) -> dict[str, Any] | None:
    row = con.execute(
        "SELECT id, question, options_json, status, created_at, closes_at, closed_at "
        "FROM rtm_votes WHERE id=?", (vote_id,)).fetchone()
    return dict(row) if row else None


def create_vote(*, question: str, options: list[str], closes_at: str | None = None) -> dict[str, Any]:
    """Open a new vote. Options are de-duplicated and trimmed; at least two are required."""
    from ..db import connect, utc_now_iso
    q = (question or "").strip()
    opts: list[str] = []
    for o in (options or []):
        t = str(o or "").strip()
        if t and t not in opts:
            opts.append(t)
    if not q:
        return {"ok": False, "error": "question required"}
    if len(opts) < 2:
        return {"ok": False, "error": "at least two distinct options required"}
    _ensure()
    with connect() as con:
        cur = con.execute(
            "INSERT INTO rtm_votes(question, options_json, status, created_at, closes_at) "
            "VALUES(?,?,'open',?,?)",
            (q, json.dumps(opts), utc_now_iso(), (closes_at or "").strip() or None),
        )
        con.commit()
        return {"ok": True, "vote_id": int(cur.lastrowid), "question": q, "options": opts}


def close_vote(vote_id: int) -> dict[str, Any]:
    """Close a vote so no further casts are accepted. Idempotent."""
    from ..db import connect, utc_now_iso
    _ensure()
    with connect() as con:
        v = _vote_row(con, int(vote_id))
        if not v:
            return {"ok": False, "error": "vote not found"}
        con.execute("UPDATE rtm_votes SET status='closed', closed_at=? WHERE id=?",
                    (utc_now_iso(), int(vote_id)))
        con.commit()
    return {"ok": True, "vote_id": int(vote_id), "status": "closed"}


def cast_vote(*, vote_id: int, flat_id: str, choice: str) -> dict[str, Any]:
    """Record one flat's cast. Rejects non-members, closed votes, and choices not on the ballot.
    A re-cast by the same flat overwrites its previous choice (never double-counts)."""
    from ..db import connect, utc_now_iso
    from .rtm_members import member_flat_ids
    fid = _norm_flat(flat_id)
    if not fid:
        return {"ok": False, "error": "flat_id required"}
    if fid not in member_flat_ids():
        return {"ok": False, "error": "flat is not a current RTM member", "flat_id": fid}
    _ensure()
    with connect() as con:
        v = _vote_row(con, int(vote_id))
        if not v:
            return {"ok": False, "error": "vote not found"}
        if v["status"] != "open":
            return {"ok": False, "error": "vote is closed"}
        options = json.loads(v["options_json"])
        ch = (choice or "").strip()
        if ch not in options:
            return {"ok": False, "error": "choice is not on the ballot", "options": options}
        con.execute(
            "INSERT INTO rtm_vote_casts(vote_id, flat_id, choice, cast_at) VALUES(?,?,?,?) "
            "ON CONFLICT(vote_id, flat_id) DO UPDATE SET choice=excluded.choice, cast_at=excluded.cast_at",
            (int(vote_id), fid, ch, utc_now_iso()),
        )
        con.commit()
    return {"ok": True, "vote_id": int(vote_id), "flat_id": fid, "choice": ch}


def tally(vote_id: int) -> dict[str, Any]:
    """Count of casts per option for one vote, plus turnout against the current member roll."""
    from ..db import connect
    from .rtm_members import member_flat_ids
    _ensure()
    with connect() as con:
        v = _vote_row(con, int(vote_id))
        if not v:
            return {"ok": False, "error": "vote not found"}
        options = json.loads(v["options_json"])
        counts = {o: 0 for o in options}
        cast_rows = con.execute(
            "SELECT flat_id, choice FROM rtm_vote_casts WHERE vote_id=?", (int(vote_id),)).fetchall()
    members = member_flat_ids()
    # Only count casts from flats that are STILL members (membership can be revoked after a cast).
    valid = [dict(r) for r in cast_rows if _norm_flat(r["flat_id"]) in members]
    for r in valid:
        if r["choice"] in counts:
            counts[r["choice"]] += 1
    total = sum(counts.values())
    return {
        "ok": True,
        "vote_id": int(vote_id),
        "question": v["question"],
        "status": v["status"],
        "options": options,
        "counts": counts,
        "total_cast": total,
        "member_count": len(members),
        "turnout_pct": round((total / len(members)) * 100.0, 1) if members else 0.0,
        "closes_at": v.get("closes_at") or "",
        # Per-flat breakdown for the DIRECTOR audit only (the Cockpit is owner-gated). This is the
        # "secret to members, auditable by directors" model: it never rides out in the portal snapshot.
        "casts": sorted(({"flat_id": r["flat_id"], "choice": r["choice"]} for r in valid),
                        key=lambda c: c["flat_id"]),
    }


def list_votes(*, open_only: bool = False) -> dict[str, Any]:
    """All votes with their tallies, newest first. `open_only` restricts to still-open votes
    (what WordPress shows members)."""
    from ..db import connect
    _ensure()
    with connect() as con:
        q = "SELECT id FROM rtm_votes"
        if open_only:
            q += " WHERE status='open'"
        q += " ORDER BY id DESC"
        ids = [int(r[0]) for r in con.execute(q).fetchall()]
    votes = [tally(i) for i in ids]
    return {"ok": True, "count": len(votes), "votes": votes}


def votes_snapshot() -> list[dict[str, Any]]:
    """Compact open-vote list for the portal snapshot pushed to WordPress. SECRET BALLOT: while a vote is
    open we do NOT send the running tally (counts) or any flat-level cast detail, so members cannot see
    who is winning; results are published only after close (results_visible), and the per-flat breakdown
    never leaves Mongoose. Only the question, options and close time ride out."""
    data = list_votes(open_only=True)
    out = []
    for v in data["votes"]:
        out.append({
            "vote_id": v["vote_id"],
            "question": v["question"],
            "options": v["options"],
            "opens_at": v.get("opens_at") or "",
            "closes_at": v["closes_at"],
            "results_visible": False,
        })
    return out


def ingest_casts(casts: list[dict[str, Any]]) -> dict[str, Any]:
    """Record a batch of casts pulled from WordPress. Each item: {vote_id, flat_id, choice}.
    Non-members / bad choices / closed votes are skipped and counted as rejected, never tallied."""
    accepted, rejected, errored = 0, 0, 0
    errors: list[str] = []
    for c in (casts or []):
        try:
            res = cast_vote(vote_id=int(c.get("vote_id")), flat_id=str(c.get("flat_id", "")),
                            choice=str(c.get("choice", "")))
        except Exception as exc:
            errored += 1  # a TRANSIENT failure (e.g. a DB error), NOT a business reject , must be retried
            errors.append(str(exc))
            continue
        if res.get("ok"):
            accepted += 1
        else:
            rejected += 1
            errors.append(res.get("error", "rejected"))
    # ok=False if any cast errored, so the caller does not ack a batch that was not fully processed and
    # thereby lose an errored vote (business rejects are final and safe to ack; exceptions are not).
    return {"ok": errored == 0, "accepted": accepted, "rejected": rejected, "errored": errored,
            "errors": errors[:10]}
