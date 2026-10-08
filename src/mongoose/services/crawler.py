from __future__ import annotations

import os
from typing import Any, Callable

from ..db import connect, utc_now_iso

# Crawler v1.
#
# The scheduler decides WHEN to run; the crawler owns WHAT remains to be indexed.
# The key idea the periodic re-scan was missing: separate HISTORICAL BACKFILL from
# incremental FRESHNESS, and give each source a CURSOR so neither one re-does work.
#
#   backfill  = slow, resumable, paginated: build the corpus once (all mail, all
#               files), tracked by a cursor so a restart resumes where it stopped.
#   freshness = frequent, cheap, incremental: fetch only what changed since the
#               last-seen marker (an IMAP UID, a file mtime watermark, later a
#               Gmail historyId / Graph deltaLink).
#
# Load controls keep a backfill from hogging the machine: each run is bounded by a
# wall-clock budget and an item cap, and stops early if free disk is low. A bounded
# run that resumes next tick is the whole point of "doesn't need babysitting".


# --- load controls (env-tunable) ---------------------------------------------
def _limits() -> dict[str, float]:
    g = os.getenv
    return {
        "max_seconds_per_run": float(g("MONGOOSE_CRAWLER_MAX_SECONDS_PER_RUN", "120")),
        "max_items_per_run": float(g("MONGOOSE_CRAWLER_MAX_FILES_PER_RUN", "1000")),
        "max_messages_per_run": float(g("MONGOOSE_CRAWLER_MAX_MESSAGES_PER_RUN", "250")),
        "min_free_disk_gb": float(g("MONGOOSE_CRAWLER_MIN_FREE_DISK_GB", "5")),
    }


def _free_disk_gb(path: str = ".") -> float:
    try:
        st = os.statvfs(path)
        return (st.f_bavail * st.f_frsize) / (1024 ** 3)
    except Exception:
        return float("inf")  # if we can't tell, don't block


def _disk_ok() -> bool:
    return _free_disk_gb() >= _limits()["min_free_disk_gb"]


# --- source state ------------------------------------------------------------
def register_source(source: str, kind: str, *, enabled: bool = True) -> None:
    """Idempotently register a crawl source so it has a row to carry cursors."""
    with connect() as con:
        con.execute(
            "INSERT INTO crawl_sources(source, kind, enabled, backfill_status) "
            "VALUES (?, ?, ?, 'pending') ON CONFLICT(source) DO UPDATE SET "
            "kind=excluded.kind, enabled=excluded.enabled",
            (source, kind, 1 if enabled else 0),
        )


def get_source(source: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM crawl_sources WHERE source = ?", (source,)).fetchone()
        return dict(row) if row else None


def _update_source(source: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE crawl_sources SET {cols} WHERE source = ?",
                    (*fields.values(), source))


def _start_run(source: str, mode: str) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO crawl_runs(source, mode, started_at, status) VALUES (?, ?, ?, 'running')",
            (source, mode, utc_now_iso()))
        return int(cur.lastrowid)


def _finish_run(run_id: int, status: str, *, scanned=0, indexed=0, skipped=0, errored=0) -> None:
    with connect() as con:
        con.execute(
            "UPDATE crawl_runs SET finished_at=?, status=?, scanned=?, indexed=?, skipped=?, errored=? "
            "WHERE id=?",
            (utc_now_iso(), status, scanned, indexed, skipped, errored, run_id))


# --- the crawl primitives ----------------------------------------------------
# A source's backfill and freshness are each a callable that does a BOUNDED chunk
# of work and returns (cursor, counts, more_remaining). The crawler loops the
# chunk until the per-run budget is hit, persisting the cursor each time.

def run_chunked(source: str, mode: str, chunk: Callable[[str | None], "ChunkResult"],
                *, item_kind: str = "messages") -> dict[str, Any]:
    """Run a source's backfill/freshness in bounded chunks until the per-run budget
    (time or items) is exhausted, persisting the cursor after each chunk so a
    restart resumes cleanly. `chunk(cursor)` does one page of work. `mode` is
    'backfill' or 'fresh' (drives which cursor column and backfill state); `item_kind`
    is 'files' or 'messages' (drives which per-run cap applies)."""
    import time
    if not _disk_ok():
        # Return the SAME shape as a normal run (zeroed) so callers that read
        # result["scanned"] etc. don't KeyError when disk is low. The skip is
        # signalled by status="skipped_low_disk", not by a different dict shape.
        return {"source": source, "mode": mode, "status": "skipped_low_disk",
                "more_remaining": True, "free_gb": round(_free_disk_gb(), 1),
                "scanned": 0, "indexed": 0, "skipped": 0, "errored": 0}
    src = get_source(source) or {}
    cursor_col = "backfill_cursor" if mode == "backfill" else "fresh_cursor"
    cursor = src.get(cursor_col)
    limits = _limits()
    deadline = time.time() + limits["max_seconds_per_run"]
    cap = limits["max_items_per_run"] if item_kind == "files" else limits["max_messages_per_run"]

    run_id = _start_run(source, mode)
    _update_source(source, last_started=utc_now_iso(),
                   backfill_status="running" if mode == "backfill" else src.get("backfill_status", "pending"))
    totals = {"scanned": 0, "indexed": 0, "skipped": 0, "errored": 0}
    status = "done"
    more = True
    try:
        while more and time.time() < deadline and totals["scanned"] < cap:
            res = chunk(cursor)
            cursor = res.cursor
            for k in totals:
                totals[k] += getattr(res, k)
            more = res.more
            _update_source(source, **{cursor_col: cursor or ""})
        # If we stopped because of the budget (not exhaustion), it's a partial run.
        if more:
            status = "partial"
    except Exception as exc:
        status = "error"
        _update_source(source, last_status="error", last_error=str(exc)[:500])
        _finish_run(run_id, "error", **totals)
        return {"source": source, "mode": mode, "status": "error", "error": str(exc), **totals}

    backfill_done = (mode == "backfill" and not more)
    _update_source(
        source,
        last_completed=utc_now_iso(),
        last_status=status,
        # Clear the previous failure on a good run. Without this a one-off error (a transient
        # imap_unreachable) sat on the source FOREVER beside last_status='done', so anything reading
        # crawl_sources , a health card, an audit, a person , saw a live-looking error on a sync that
        # was in fact healthy. A stale alarm is a false alarm, and false alarms get ignored.
        **({"last_error": None} if status != "error" else {}),
        scanned=(src.get("scanned", 0) + totals["scanned"]),
        indexed=(src.get("indexed", 0) + totals["indexed"]),
        **({"backfill_status": "done"} if backfill_done else {}),
    )
    _finish_run(run_id, status, **totals)
    return {"source": source, "mode": mode, "status": status, "more_remaining": more, **totals}


class ChunkResult:
    """One bounded page of crawl work."""
    __slots__ = ("cursor", "scanned", "indexed", "skipped", "errored", "more")

    def __init__(self, cursor: str | None, *, scanned=0, indexed=0, skipped=0, errored=0, more=False):
        self.cursor = cursor
        self.scanned = scanned
        self.indexed = indexed
        self.skipped = skipped
        self.errored = errored
        self.more = more


# --- status ------------------------------------------------------------------
def crawler_status() -> list[dict[str, Any]]:
    """Per-source progress, the visibility the periodic re-scan never gave you."""
    with connect() as con:
        rows = con.execute(
            "SELECT source, kind, enabled, backfill_status, backfill_cursor, fresh_cursor, "
            "last_started, last_completed, last_status, last_error, scanned, indexed "
            "FROM crawl_sources ORDER BY source").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # human one-liner
            d["summary"] = _summarize(d)
            out.append(d)
        return out


def _summarize(d: dict[str, Any]) -> str:
    kind = d["kind"]
    bf = d["backfill_status"]
    scanned = d.get("scanned", 0)
    indexed = d.get("indexed", 0)
    if bf == "done":
        head = f"{kind} backfill complete"
    elif bf == "running":
        head = f"{kind} backfill in progress"
    else:
        head = f"{kind} backfill {bf}"
    tail = f"{scanned} scanned, {indexed} indexed"
    if d.get("fresh_cursor"):
        tail += f"; fresh cursor {d['fresh_cursor']}"
    last = d.get("last_completed") or d.get("last_started") or "never"
    return f"{head}: {tail}; last run {last}"



def _parse_imap_cursor(cursor: str | None) -> dict[str, Any]:
    """Return {uid, uidvalidity} from either the legacy numeric cursor or the
    new JSON cursor. UID cursors are only valid within one IMAP UIDVALIDITY; after a
    server rebuild/migration/restore, the same mailbox can reuse lower UIDs.
    """
    text = str(cursor or "").strip()
    if not text:
        return {"uid": 0, "uidvalidity": None}
    if text.isdigit():
        return {"uid": int(text), "uidvalidity": None}
    try:
        import json as _json
        data = _json.loads(text)
        if isinstance(data, dict):
            uid = data.get("uid", 0)
            return {
                "uid": int(uid) if str(uid).isdigit() else 0,
                "uidvalidity": str(data.get("uidvalidity")) if data.get("uidvalidity") not in (None, "") else None,
            }
    except Exception:
        pass
    return {"uid": 0, "uidvalidity": None}


def _format_imap_cursor(uid: int, uidvalidity: Any = None) -> str:
    try:
        uid_i = int(uid or 0)
    except Exception:
        uid_i = 0
    if uidvalidity not in (None, ""):
        import json as _json
        return _json.dumps({"uid": uid_i, "uidvalidity": str(uidvalidity)}, sort_keys=True)
    return str(uid_i)

# --- IMAP source: cursor-based freshness -------------------------------------
def crawl_imap_fresh(mailbox: str = "INBOX") -> dict[str, Any]:
    """Incremental IMAP freshness via the UID cursor: fetch only messages newer
    than the last UID we saw, instead of re-pulling the last 50 every cycle. This
    is the efficient-polling design the periodic re-scan lacked."""
    source = f"imap:{mailbox}"
    register_source(source, "imap")

    def chunk(cursor: str | None) -> ChunkResult:
        from .email_index import sync_imap_incremental
        state = _parse_imap_cursor(cursor)
        last_uid = int(state.get("uid") or 0)
        last_validity = state.get("uidvalidity")
        res = sync_imap_incremental(mailbox, last_uid=last_uid,
                                    max_messages=int(_limits()["max_messages_per_run"]))
        if not res.get("ok"):
            raise RuntimeError(str(res.get("error") or res.get("reason") or "IMAP incremental sync failed"))
        validity = res.get("uidvalidity")
        # UIDVALIDITY is the IMAP server's promise that UIDs mean the same thing as last time.
        # If it changes after a mailbox rebuild/restore/provider migration, the old cursor is
        # meaningless. Reset once from UID 0 instead of silently believing there is no new mail.
        if last_uid > 0 and last_validity and validity and str(validity) != str(last_validity):
            res = sync_imap_incremental(mailbox, last_uid=0,
                                        max_messages=int(_limits()["max_messages_per_run"]))
            if not res.get("ok"):
                raise RuntimeError(str(res.get("error") or res.get("reason") or "IMAP incremental sync failed after UIDVALIDITY reset"))
            validity = res.get("uidvalidity") or validity
        new_high = res.get("highest_uid", 0 if (last_validity and validity and str(validity) != str(last_validity)) else last_uid)
        return ChunkResult(_format_imap_cursor(new_high, validity), scanned=res.get("count", 0),
                           indexed=res.get("count", 0), more=bool(res.get("more")))

    return run_chunked(source, "fresh", chunk)


# --- Files source: resumable walk cursor -------------------------------------
def crawl_files(root: str, device: str = "local", mode: str = "fresh") -> dict[str, Any]:
    """Walk a file root, resuming from a path cursor so a large tree isn't fully
    re-walked in one run. mtime-skipping (in index_files) means unchanged files
    cost almost nothing; the cursor means a budget-bounded run resumes next time."""
    source = f"files:{root}"
    register_source(source, "files")

    def chunk(cursor: str | None) -> ChunkResult:
        from .file_index import index_files_resumable, prune_missing_files
        res = index_files_resumable(
            root, device=device, after_path=cursor or "",
            limit=int(_limits()["max_items_per_run"]))
        more = bool(res.get("more"))
        next_cursor = res.get("cursor") or ""
        pruned_count = 0
        # End of tree means the next scheduled freshness pass must wrap to the beginning. Otherwise a
        # root that reached /Users/x/Z... once would never notice later edits/deletes in /Users/x/A...
        # after two weeks of ordinary use. Use the end-of-tree moment to perform bounded stale-pruning.
        if not more:
            next_cursor = ""
            try:
                pruned = prune_missing_files(root, device=device,
                                             limit=max(1000, int(_limits()["max_items_per_run"])))
                pruned_count = int(pruned.get("pruned", 0) or 0)
            except Exception:
                pruned_count = 0
        return ChunkResult(next_cursor, scanned=res.get("scanned", 0),
                           indexed=res.get("indexed", 0), skipped=res.get("skipped", 0),
                           errored=pruned_count, more=more)

    return run_chunked(source, mode, chunk, item_kind="files")
