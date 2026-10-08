"""RTM membership: a persisted per-flat "is an RTM member" flag the director toggles, plus the
director's flats/members view (flat, leaseholder name, email, membership, last login).

Members (plus the directors, who are members by definition) are who active votes get shown to. The
flag lives in its own tiny table so it is REMEMBERED across snapshots, imports, and everything else.
Last login comes from WordPress (the leaseholder accounts); until that feed is wired it shows blank.
"""
from __future__ import annotations

from typing import Any


def _ensure() -> None:
    from ..db import connect
    with connect() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS rtm_membership (
                flat_id TEXT PRIMARY KEY,
                is_member INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT
            )"""
        )
        # Cache of WordPress account facts per flat (who has onboarded + last login + RTM role), pulled
        # from WP. accounts_json holds the list of {username,email,last_login,role}; last_login is the
        # newest of them and role is the plugin's already-mapped short RTM role.
        con.execute(
            """CREATE TABLE IF NOT EXISTS rtm_flat_accounts (
                flat_id TEXT PRIMARY KEY,
                accounts_json TEXT,
                last_login TEXT,
                updated_at TEXT
            )"""
        )
        con.commit()


def cache_flat_accounts(accounts: dict[str, Any]) -> int:
    """Upsert the per-flat WordPress account cache from a {flat_id: [{username,email,last_login}]} map
    pulled from WordPress. Returns the number of flats cached. last_login is the newest across a flat's
    accounts (ISO/mysql strings sort chronologically)."""
    import json
    from ..db import connect, utc_now_iso
    _ensure()
    now = utc_now_iso()
    n = 0
    with connect() as con:
        for flat_id, accs in (accounts or {}).items():
            fid = _norm(flat_id)
            if not fid:
                continue
            acc_list = accs if isinstance(accs, list) else []
            logins = [str(a.get("last_login") or "") for a in acc_list if isinstance(a, dict)]
            last_login = max([x for x in logins if x], default="")
            con.execute(
                "INSERT INTO rtm_flat_accounts(flat_id, accounts_json, last_login, updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(flat_id) DO UPDATE SET "
                "accounts_json=excluded.accounts_json, last_login=excluded.last_login, updated_at=excluded.updated_at",
                (fid, json.dumps(acc_list), last_login, now),
            )
            n += 1
        con.commit()
    return n


def _flat_accounts_cache() -> dict[str, dict[str, Any]]:
    """Read the WP account cache keyed by normalised flat_id."""
    import json
    _ensure()
    from ..db import connect
    out: dict[str, dict[str, Any]] = {}
    with connect() as con:
        for r in con.execute("SELECT flat_id, accounts_json, last_login FROM rtm_flat_accounts").fetchall():
            try:
                accs = json.loads(r[1] or "[]")
            except Exception:
                accs = []
            out[str(r[0]).upper()] = {"accounts": accs, "last_login": r[2] or ""}
    return out


def _norm(flat_id: str) -> str:
    return (flat_id or "").strip().upper().replace("-", "")


# Highest-privilege-first ranking, so a flat with several linked accounts reports its strongest role
# (a director who also holds a leaseholder account shows as director). "owner" is the local Cockpit
# path and never arrives from WordPress, but is ranked for completeness.
_ROLE_RANK = {"owner": 4, "admin": 3, "director": 2, "auditor": 1, "leaseholder": 0}


def _highest_account_role(accounts: list[Any]) -> str:
    """The strongest RTM role among a flat's WordPress accounts, normalised through the single
    plugin->RTM role map. Returns "" for a flat with no linked accounts (not onboarded)."""
    from .rtm_pull import map_role
    best, best_rank = "", -1
    for a in accounts or []:
        if not isinstance(a, dict):
            continue
        # Any present account is at least a leaseholder: map_role("") degrades to leaseholder, so a
        # legacy cached account with no role field still counts (invariant: role=="" iff no accounts).
        role = map_role(str(a.get("role") or ""))
        rank = _ROLE_RANK.get(role, 0)
        if rank > best_rank:
            best, best_rank = role, rank
    return best


def set_member(flat_id: str, is_member: bool) -> dict[str, Any]:
    """Persist whether a flat's leaseholder is an RTM member. Remembered until changed again."""
    from ..db import connect, utc_now_iso
    fid = _norm(flat_id)
    if not fid:
        return {"ok": False, "error": "flat_id required"}
    _ensure()
    with connect() as con:
        con.execute(
            "INSERT INTO rtm_membership(flat_id, is_member, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(flat_id) DO UPDATE SET is_member=excluded.is_member, updated_at=excluded.updated_at",
            (fid, 1 if is_member else 0, utc_now_iso()),
        )
        con.commit()
    return {"ok": True, "flat_id": fid, "is_member": bool(is_member)}


def member_flat_ids() -> set[str]:
    """The set of flat_ids currently flagged as RTM members (normalised)."""
    _ensure()
    from ..db import connect
    with connect() as con:
        return {str(r[0]).upper() for r in con.execute(
            "SELECT flat_id FROM rtm_membership WHERE is_member=1").fetchall()}


def director_members() -> dict[str, Any]:
    """Every flat with its leaseholder name, email, RTM-member flag and last login, for the director."""
    from .rtm_management import list_flats
    from .rtm_portal import _flat_email_map
    _ensure()
    emails = _flat_email_map() or {}
    members = member_flat_ids()
    accounts = _flat_accounts_cache()  # who has a WordPress account + last login, pulled from WP
    rows = []
    for f in (list_flats() or []):
        fid = str(f.get("flat_id") or "")
        acc = accounts.get(fid.upper(), {})
        wp_accounts = acc.get("accounts", []) or []
        rows.append({
            "flat_id": fid,
            "block": f.get("block", ""),
            "flat_number": f.get("flat_number", ""),
            "leaseholder_name": f.get("leaseholder_name", ""),
            "email": emails.get(fid.upper(), ""),
            "is_member": fid.upper() in members,
            "last_login": acc.get("last_login", ""),  # real, from the WP wp_login capture
            "wp_account_count": len(wp_accounts),
            "wp_usernames": ", ".join(str(a.get("username", "")) for a in wp_accounts if a.get("username")),
            "role": _highest_account_role(wp_accounts),  # strongest RTM role held on this flat, "" if not onboarded
        })
    return {"ok": True, "count": len(rows), "members": rows,
            "member_count": sum(1 for r in rows if r["is_member"]),
            "onboarded_count": sum(1 for r in rows if r["wp_account_count"] > 0)}
