from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from ..db import connect, utc_now_iso


_RESULT_SET_KEEP = 50
_ITEM_KEEP = 1000


def _ensure_tables() -> None:
    """Create the lightweight persisted item/result model used by unified search.

    This deliberately lives in the service rather than the global schema version: it is additive,
    idempotent, and safe to create on an already-running user's existing brain. Search must not lose
    the last useful result merely because a migration step did not run.
    """
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS item_result_sets (
                id TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                surface TEXT NOT NULL DEFAULT 'search',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                result_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS item_result_handles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                result_set_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                item_ref TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT,
                where_text TEXT,
                connector TEXT,
                native_id TEXT,
                actions_json TEXT NOT NULL DEFAULT '[]',
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                UNIQUE(result_set_id, ordinal),
                UNIQUE(result_set_id, item_ref)
            );
            CREATE INDEX IF NOT EXISTS idx_item_result_handles_ref ON item_result_handles(item_ref);
            CREATE INDEX IF NOT EXISTS idx_item_result_sets_created ON item_result_sets(created_at DESC);
            """
        )


def _prune() -> None:
    now = utc_now_iso()
    with connect() as con:
        con.execute("DELETE FROM item_result_handles WHERE expires_at < ?", (now,))
        con.execute("DELETE FROM item_result_sets WHERE expires_at < ?", (now,))
        # Keep recent sets/items bounded so repeated searches over weeks do not bloat the local DB.
        old_sets = con.execute(
            "SELECT id FROM item_result_sets ORDER BY created_at DESC LIMIT -1 OFFSET ?",
            (_RESULT_SET_KEEP,),
        ).fetchall()
        for row in old_sets:
            con.execute("DELETE FROM item_result_handles WHERE result_set_id=?", (row["id"],))
            con.execute("DELETE FROM item_result_sets WHERE id=?", (row["id"],))
        old_items = con.execute(
            "SELECT id FROM item_result_handles ORDER BY created_at DESC LIMIT -1 OFFSET ?",
            (_ITEM_KEEP,),
        ).fetchall()
        for row in old_items:
            con.execute("DELETE FROM item_result_handles WHERE id=?", (row["id"],))


def remember_search_results(query: str, results: list[dict[str, Any]], *, surface: str = "search",
                            ttl_hours: int = 48) -> dict[str, Any]:
    """Persist a snapshot of search results so 'the first one' still means something later.

    The snapshots store already-masked result metadata, not full email/file bodies. This gives the UI
    and chat a stable handle without creating another private-content cache.
    """
    _ensure_tables()
    _prune()
    sid = "S" + secrets.token_hex(3).upper()
    now = datetime.now(timezone.utc)
    created = now.isoformat()
    expires = (now + timedelta(hours=max(1, int(ttl_hours)))).isoformat()
    with connect() as con:
        con.execute(
            "INSERT INTO item_result_sets(id, query, surface, created_at, expires_at, result_count) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, query or "", surface, created, expires, len(results)),
        )
        for idx, item in enumerate(results, start=1):
            snap = dict(item)
            snap["result_set_id"] = sid
            snap["ordinal"] = idx
            con.execute(
                """
                INSERT INTO item_result_handles(result_set_id, ordinal, item_ref, kind, title, where_text,
                                                connector, native_id, actions_json, snapshot_json, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (sid, idx, str(item.get("item_ref") or f"item-{idx}"), str(item.get("kind") or "item"),
                 item.get("title"), item.get("where"), item.get("connector") or item.get("source"),
                 str(item.get("native_id") or item.get("open") or ""),
                 json.dumps(item.get("actions") or []), json.dumps(snap, default=str), created, expires),
            )
    return {"result_set_id": sid, "expires_at": expires, "count": len(results)}


def latest_result_set() -> dict[str, Any] | None:
    _ensure_tables()
    with connect() as con:
        row = con.execute(
            "SELECT * FROM item_result_sets ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def result_set(result_set_id: str | None = None) -> dict[str, Any] | None:
    _ensure_tables()
    if not result_set_id:
        head = latest_result_set()
        result_set_id = head["id"] if head else ""
    if not result_set_id:
        return None
    with connect() as con:
        rs = con.execute("SELECT * FROM item_result_sets WHERE id=?", (result_set_id,)).fetchone()
        if not rs:
            return None
        rows = con.execute(
            "SELECT ordinal, item_ref, kind, title, where_text, connector, native_id, actions_json, snapshot_json "
            "FROM item_result_handles WHERE result_set_id=? ORDER BY ordinal",
            (result_set_id,),
        ).fetchall()
    items = []
    for row in rows:
        d = dict(row)
        try:
            d["actions"] = json.loads(d.pop("actions_json") or "[]")
        except Exception:
            d["actions"] = []
        try:
            d["snapshot"] = json.loads(d.pop("snapshot_json") or "{}")
        except Exception:
            d["snapshot"] = {}
        items.append(d)
    out = dict(rs)
    out["items"] = items
    return out


def resolve_item(ref: str | None = None, *, ordinal: int | None = None,
                 result_set_id: str | None = None) -> dict[str, Any] | None:
    """Resolve a persisted search item by item_ref or ordinal in a result set."""
    _ensure_tables()
    params: list[Any] = []
    where = []
    if result_set_id:
        where.append("result_set_id=?"); params.append(result_set_id)
    if ref:
        where.append("UPPER(item_ref)=UPPER(?)"); params.append(ref)
    elif ordinal is not None:
        if not result_set_id:
            latest = latest_result_set()
            if not latest:
                return None
            result_set_id = latest["id"]
            where.append("result_set_id=?"); params.append(result_set_id)
        where.append("ordinal=?"); params.append(int(ordinal))
    else:
        return None
    sql = "SELECT * FROM item_result_handles WHERE " + " AND ".join(where) + " ORDER BY created_at DESC LIMIT 1"
    with connect() as con:
        row = con.execute(sql, params).fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["actions"] = json.loads(out.pop("actions_json") or "[]")
    except Exception:
        out["actions"] = []
    try:
        out["snapshot"] = json.loads(out.pop("snapshot_json") or "{}")
    except Exception:
        out["snapshot"] = {}
    return out


def describe_actions(ref: str | None = None, *, ordinal: int | None = None,
                     result_set_id: str | None = None) -> dict[str, Any]:
    item = resolve_item(ref, ordinal=ordinal, result_set_id=result_set_id)
    if not item:
        return {"ok": False, "error": "item not found", "hint": "Run a search first, then refer to a result number or item_ref."}
    snap = item.get("snapshot") or {}
    return {"ok": True, "item_ref": item.get("item_ref"), "ordinal": item.get("ordinal"),
            "kind": item.get("kind"), "title": item.get("title"), "where": item.get("where_text"),
            "connector": item.get("connector"), "native_id": item.get("native_id"),
            "open": snap.get("open"), "snapshot": snap, "actions": item.get("actions") or [],
            "correlation_keys": snap.get("correlation_keys") or [],
            "related": snap.get("related") or []}
