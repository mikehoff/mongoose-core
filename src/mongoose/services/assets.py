"""Uploaded-asset store, the single brain's record of build materials, plugins, and other files the
user uploads from the phone (or any device).

What this gives the user: upload a file from the phone, get back a stable ASSET ID (e.g.
asset_7f3a2b...), the file is saved under data/assets/, and the single brain tracks it (kind, mime,
size, sha256, who uploaded it, when, and a classification). The classification routes it:
  - a correctly-structured Mongoose build zip (pyproject.toml + src/mongoose) becomes available for
    SELF-UPDATE, it can be staged/applied via the updater without re-uploading anywhere.
  - any other code/document is recorded and made available for RTM ENRICHMENT (the RTM workflows can
    pull from the asset store), and for general reference.

Everything is content-addressed by sha256 so re-uploading the same bytes is idempotent (same asset id).
The store is defensive and never trusts the uploaded bytes beyond recording them; staging/applying a
build still goes through the updater's full check->approve->apply with backup+rollback.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ASSET_KINDS = ("build_zip", "code", "document", "archive", "other")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _assets_dir() -> Path:
    from ..settings import get_settings
    s = get_settings()
    base = Path(getattr(s, "data_dir", "data"))
    d = base / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db_path() -> Path:
    from ..settings import get_settings
    s = get_settings()
    return Path(getattr(s, "db_path", None) or (Path(getattr(s, "data_dir", "data")) / "mongoose.sqlite3"))


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(str(_db_path()))
    con.row_factory = sqlite3.Row
    return con


def ensure_table() -> None:
    """Create the assets table if missing. Called lazily so the store works even if the global
    init_db migration hasn't been re-run."""
    con = _con()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS assets (
                asset_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                kind TEXT NOT NULL,
                mime_type TEXT,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                uploaded_by TEXT,
                source_surface TEXT,
                classification TEXT,
                available_for_self_update INTEGER NOT NULL DEFAULT 0,
                available_for_rtm INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                created_at TEXT NOT NULL
            );
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_assets_sha ON assets(sha256);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_assets_kind ON assets(kind);")
        con.commit()
    finally:
        con.close()


def _asset_id(sha: str) -> str:
    return "asset_" + sha[:24]


def _classify(path: Path, filename: str) -> dict[str, Any]:
    """Decide what the upload is and what it can be used for. A correctly-structured build zip is made
    available for self-update; other code/archives for RTM enrichment + reference."""
    name = filename.lower()
    is_zip = zipfile.is_zipfile(str(path))
    kind = "other"
    self_update = False
    rtm = False
    reason = ""
    if is_zip:
        kind = "archive"
        try:
            with zipfile.ZipFile(str(path)) as z:
                names = z.namelist()
            # A Mongoose build candidate has pyproject.toml AND src/mongoose somewhere in the tree
            # (possibly under a top-level dir like mongoose-alpha/).
            has_pyproject = any(n.endswith("pyproject.toml") for n in names)
            has_pkg = any("/src/mongoose/" in ("/" + n) or n.startswith("src/mongoose/") for n in names)
            if has_pyproject and has_pkg:
                kind = "build_zip"
                self_update = True
                rtm = True   # a build is also code, usable for enrichment/reference
                reason = "Correctly-structured Mongoose build (pyproject.toml + src/mongoose); available for self-update."
            else:
                rtm = True
                reason = "Archive without a Mongoose package layout; available for RTM enrichment/reference."
        except Exception as exc:
            reason = f"Zip could not be inspected ({exc}); recorded as an archive."
    else:
        # Heuristic for code vs document.
        code_exts = (".py", ".js", ".ts", ".sh", ".rb", ".go", ".rs", ".java", ".sql", ".json",
                     ".yaml", ".yml", ".toml", ".html", ".css", ".tsx", ".jsx")
        if name.endswith(code_exts):
            kind = "code"
            rtm = True
            reason = "Code file; available for RTM enrichment/reference."
        else:
            kind = "document"
            rtm = True
            reason = "Document; available for RTM enrichment/reference."
    return {"kind": kind, "available_for_self_update": self_update,
            "available_for_rtm": rtm, "reason": reason}


def store_asset(data: bytes, filename: str, *, uploaded_by: str | None = None,
                source_surface: str | None = None, mime_type: str | None = None,
                note: str | None = None) -> dict[str, Any]:
    """Save uploaded bytes as an asset, record it in the brain, and classify it. Content-addressed:
    re-uploading identical bytes returns the existing asset (idempotent). Returns the asset record."""
    ensure_table()
    if not data:
        return {"ok": False, "error": "no data"}
    sha = hashlib.sha256(data).hexdigest()
    aid = _asset_id(sha)

    con = _con()
    try:
        existing = con.execute("SELECT * FROM assets WHERE asset_id = ?", (aid,)).fetchone()
        if existing:
            return {"ok": True, "asset_id": aid, "deduped": True, **_row_to_dict(existing)}
    finally:
        con.close()

    # Save bytes under data/assets/<asset_id>/<filename> (keep the original name for usability).
    safe_name = Path(filename).name or "upload.bin"
    dest_dir = _assets_dir() / aid
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name
    dest.write_bytes(data)

    cls = _classify(dest, safe_name)
    rec = {
        "asset_id": aid, "filename": safe_name, "kind": cls["kind"], "mime_type": mime_type,
        "size_bytes": len(data), "sha256": sha, "stored_path": str(dest),
        "uploaded_by": uploaded_by, "source_surface": source_surface,
        "classification": cls["reason"],
        "available_for_self_update": 1 if cls["available_for_self_update"] else 0,
        "available_for_rtm": 1 if cls["available_for_rtm"] else 0,
        "note": note, "created_at": _now_iso(),
    }
    con = _con()
    try:
        con.execute("""
            INSERT INTO assets (asset_id, filename, kind, mime_type, size_bytes, sha256, stored_path,
                                uploaded_by, source_surface, classification, available_for_self_update,
                                available_for_rtm, note, created_at)
            VALUES (:asset_id, :filename, :kind, :mime_type, :size_bytes, :sha256, :stored_path,
                    :uploaded_by, :source_surface, :classification, :available_for_self_update,
                    :available_for_rtm, :note, :created_at)
        """, rec)
        con.commit()
    finally:
        con.close()

    # Record in the audit log so the brain has a first-class trace of the upload.
    try:
        _audit(aid, cls, uploaded_by, source_surface)
    except Exception:
        pass

    return {"ok": True, "deduped": False, **rec,
            "available_for_self_update": cls["available_for_self_update"],
            "available_for_rtm": cls["available_for_rtm"], "reason": cls["reason"]}


def _audit(aid: str, cls: dict, uploaded_by: str | None, surface: str | None) -> None:
    con = _con()
    try:
        con.execute("""
            INSERT INTO audit_log (module, action, route, privacy, success, reason, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("assets", "asset_uploaded", surface or "", "normal", 1, cls["reason"],
              json.dumps({"asset_id": aid, "kind": cls["kind"], "by": uploaded_by, "surface": surface}),
              _now_iso()))
        con.commit()
    except Exception:
        pass
    finally:
        con.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["available_for_self_update"] = bool(d.get("available_for_self_update"))
    d["available_for_rtm"] = bool(d.get("available_for_rtm"))
    return d


def get_asset(asset_id: str) -> dict[str, Any] | None:
    ensure_table()
    con = _con()
    try:
        row = con.execute("SELECT * FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        con.close()


def list_assets(*, kind: str | None = None, self_update_only: bool = False,
                rtm_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    ensure_table()
    q = "SELECT * FROM assets"
    where = []
    params: list[Any] = []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if self_update_only:
        where.append("available_for_self_update = 1")
    if rtm_only:
        where.append("available_for_rtm = 1")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    con = _con()
    try:
        return [_row_to_dict(r) for r in con.execute(q, params).fetchall()]
    finally:
        con.close()


def asset_path(asset_id: str) -> Path | None:
    rec = get_asset(asset_id)
    if not rec:
        return None
    p = Path(rec["stored_path"])
    return p if p.exists() else None


def self_update_candidates() -> list[dict[str, Any]]:
    """Assets that are correctly-structured build zips, ready for the updater to stage/apply, no
    re-upload needed."""
    return list_assets(self_update_only=True)


def rtm_candidates() -> list[dict[str, Any]]:
    """Assets available for RTM enrichment/reference."""
    return list_assets(rtm_only=True)
