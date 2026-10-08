from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from ..db import connect, utc_now_iso, audit
from ..settings import get_settings

# Metro CSV folder-watch ingest.
#
# Why this exists: Metro Bank has no scheduled/emailed CSV and decommissioned its
# direct PSD2 APIs, so the ONLY way a CSV leaves the bank is a manual on-screen
# "Export transactions to CSV". That click is irreducible without a licensed AISP
# feed (a cloud third party in the trust-account data path) or a script driving the
# banking login (a credential-security liability). Both are deliberately out of scope
# for this local-first, money-touching-free RTM spine.
#
# What this DOES remove is everything AFTER the click: the user exports to a watched
# folder (e.g. ~/Downloads or a dedicated drop dir), and this watcher picks the file
# up on the scheduler's next tick, runs import_metro_csv, archives the original, and
# records the result. No command to run, no file to move, no manual reconciliation.
#
# Boundaries kept:
#   - reads a local file only; touches no bank, no network, moves no money.
#   - import itself stays conservative (exact ref+amount auto-allocates; everything
#     else drops to the manual review queue) , unchanged.
#   - processed files are fingerprinted (SHA-256) so the same export dropped twice is
#     skipped, not double-imported. This is belt-and-braces on top of the importer's
#     own per-transaction duplicate fingerprinting.

_DEFAULT_GLOBS = ("*.csv", "*.CSV")


def _ensure_table() -> None:
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS metro_watch_seen (
                sha256 TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                archived_path TEXT NOT NULL,
                imported INTEGER NOT NULL DEFAULT 0,
                exact INTEGER NOT NULL DEFAULT 0,
                partial INTEGER NOT NULL DEFAULT 0,
                unmatched INTEGER NOT NULL DEFAULT 0,
                duplicates INTEGER NOT NULL DEFAULT 0,
                seen_at TEXT NOT NULL
            )
            """
        )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _already_seen(sha: str) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute(
            "SELECT * FROM metro_watch_seen WHERE sha256=?", (sha,)
        ).fetchone()
    return dict(row) if row else None


def _looks_like_metro_csv(path: Path) -> bool:
    """Cheap header sniff so an unrelated CSV dropped in a shared folder (e.g. a
    Downloads dir) isn't fed to the bank importer. Metro's transaction export has a
    date column, a value/amount column, and a running balance column; we accept on
    any plausible date+amount header pairing the importer already understands."""
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as f:
            header = f.readline().lower()
    except Exception:
        return False
    if not header:
        return False
    has_date = any(k in header for k in ("date", "posted", "booking"))
    has_amount = any(k in header for k in ("amount", "value", "paid in", "money in",
                                           "credit", "paid out", "money out", "debit"))
    return has_date and has_amount


def _watch_dir() -> Path | None:
    raw = getattr(get_settings(), "metro_watch_dir", "")
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.exists() else None


def _archive_dir() -> Path:
    raw = getattr(get_settings(), "metro_archive_dir", "")
    if raw:
        p = Path(raw).expanduser()
    else:
        # Default: a sibling 'imported' folder next to the watch dir, or the data dir.
        wd = _watch_dir()
        p = (wd / "imported") if wd else Path("./data/metro_imported")
    p.mkdir(parents=True, exist_ok=True)
    return p


def scan_once() -> dict[str, Any]:
    """One pass over the watch dir: import any new Metro CSV, archive it, record it.
    Idempotent and safe to call every scheduler tick. Returns a per-file summary."""
    _ensure_table()
    wd = _watch_dir()
    if wd is None:
        return {"skipped": "no_watch_dir_configured"}

    globs = _DEFAULT_GLOBS
    candidates: list[Path] = []
    for g in globs:
        candidates.extend(sorted(wd.glob(g)))
    # de-dup while preserving order (case-insensitive FS can match twice)
    seen_paths: set[str] = set()
    files = []
    for c in candidates:
        key = str(c.resolve()).lower()
        if key not in seen_paths and c.is_file():
            seen_paths.add(key)
            files.append(c)

    results: list[dict[str, Any]] = []
    imported_files = 0
    for f in files:
        try:
            sha = _sha256(f)
        except Exception as exc:  # noqa: BLE001
            results.append({"file": f.name, "status": "error", "reason": f"hash: {exc}"})
            continue

        prior = _already_seen(sha)
        if prior:
            results.append({"file": f.name, "status": "already_imported",
                            "archived_path": prior["archived_path"]})
            continue

        if not _looks_like_metro_csv(f):
            results.append({"file": f.name, "status": "skipped_not_metro_csv"})
            continue

        # Import via the existing, unchanged conservative importer.
        from .rtm_management import import_metro_csv
        try:
            res = import_metro_csv(f)
        except Exception as exc:  # noqa: BLE001
            audit("metro_watch", "import", False, reason=f"{f.name}: {exc}")
            results.append({"file": f.name, "status": "import_failed", "reason": str(exc)})
            continue

        # Archive the original so the watch dir drains and re-drops don't re-fire.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = _archive_dir() / f"{stamp}__{f.name}"
        try:
            shutil.move(str(f), str(dest))
        except Exception:
            # If move fails (permissions/cross-device), copy then leave original;
            # the SHA record still prevents a re-import next tick.
            try:
                shutil.copy2(str(f), str(dest))
            except Exception as exc:  # noqa: BLE001
                dest = f  # record where it actually is
                audit("metro_watch", "archive", False, reason=f"{f.name}: {exc}")

        with connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO metro_watch_seen
                    (sha256, original_name, archived_path, imported, exact, partial,
                     unmatched, duplicates, seen_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (sha, f.name, str(dest), int(res.get("imported", 0)),
                 int(res.get("exact", 0)), int(res.get("partial", 0)),
                 int(res.get("unmatched", 0)), int(res.get("duplicates_or_skipped", 0)),
                 utc_now_iso()),
            )
        imported_files += 1
        audit("metro_watch", "import", True,
              reason=f"{f.name}: imported={res.get('imported')} exact={res.get('exact')} "
                     f"partial={res.get('partial')} unmatched={res.get('unmatched')}")
        results.append({
            "file": f.name, "status": "imported", "archived_path": str(dest),
            "imported": res.get("imported", 0), "exact": res.get("exact", 0),
            "partial": res.get("partial", 0), "unmatched": res.get("unmatched", 0),
            "duplicates_or_skipped": res.get("duplicates_or_skipped", 0),
        })

    # Aggregate for a one-line scheduler/notify summary.
    needs_review = sum(int(r.get("partial", 0)) + int(r.get("unmatched", 0))
                       for r in results if r.get("status") == "imported")
    return {
        "ok": True,
        "watch_dir": str(wd),
        "archive_dir": str(_archive_dir()),
        "files_seen": len(files),
        "files_imported": imported_files,
        "needs_review": needs_review,
        "results": results,
    }


def status() -> dict[str, Any]:
    """Readiness/diagnostic view: is a watch dir configured and present, and what was
    imported recently. Used by doctor/dashboard so the user can see the path is live
    without dropping a file to test it."""
    _ensure_table()
    s = get_settings()
    configured = bool(getattr(s, "metro_watch_dir", ""))
    wd = _watch_dir()
    with connect() as con:
        recent = [dict(r) for r in con.execute(
            "SELECT original_name, archived_path, imported, exact, partial, unmatched, "
            "duplicates, seen_at FROM metro_watch_seen ORDER BY seen_at DESC LIMIT 10"
        ).fetchall()]
        total = con.execute("SELECT COUNT(*) AS n FROM metro_watch_seen").fetchone()["n"]
    return {
        "configured": configured,
        "watch_dir": str(wd) if wd else getattr(s, "metro_watch_dir", "") or None,
        "watch_dir_exists": wd is not None,
        "archive_dir": str(_archive_dir()),
        "files_imported_total": total,
        "recent": recent,
        "note": "Metro has no scheduled/emailed CSV; you click 'Export transactions to CSV' "
                "in Metro online banking into this folder and the rest is automatic. "
                "No bank connection, no money movement.",
    }
