from __future__ import annotations

import os
import bisect
from pathlib import Path
from typing import Any, Iterator

from ..db import safe_fts_query, audit, connect, utc_now_iso

TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".py", ".js", ".ts", ".html", ".css"}

# Directories that make a home-folder crawl feel broken after a day or two: dependency caches,
# virtualenvs, package managers and browser/system stores are huge, noisy and often permission-hostile.
# Users can still point Mongoose at a specific project folder if they want those files indexed.
_SKIP_DIR_NAMES = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "Library", ".Trash",
}


def _max_text_bytes() -> int:
    try:
        return max(64 * 1024, int(os.getenv("MONGOOSE_FILE_INDEX_MAX_TEXT_BYTES", str(1024 * 1024))))
    except Exception:
        return 1024 * 1024


def _safe_file_paths(base: Path) -> Iterator[Path]:
    """Yield files under ``base`` without letting one inaccessible folder abort the whole crawl.

    ``Path.rglob`` is pleasant in tests but harsh in real home folders: one iCloud/Photos/cache
    permission error can throw out of the walk and leave scheduled freshness permanently red.
    ``os.walk(..., onerror=...)`` lets us skip bad branches and keep indexing the useful parts.
    """
    base_s = str(base)
    for root, dirs, files in os.walk(base_s, topdown=True, followlinks=False, onerror=lambda _e: None):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIR_NAMES and not d.endswith(".app")]
        for name in files:
            try:
                yield Path(root) / name
            except Exception:
                continue


def _bounded_sorted_after(base: Path, after_path: str, limit: int) -> tuple[list[str], bool]:
    """Smallest ``limit`` file paths after ``after_path`` without materialising the whole tree.

    The previous implementation pushed every candidate into a heap, so a 200k-file home folder could
    spend ages and memory just deciding the next 1k files. This keeps only ``limit + 1`` paths.
    """
    keep = max(1, int(limit)) + 1
    candidates: list[str] = []
    seen = 0
    for p in _safe_file_paths(base):
        sp = str(p)
        if after_path and sp <= after_path:
            continue
        seen += 1
        if len(candidates) < keep:
            bisect.insort(candidates, sp)
        elif sp < candidates[-1]:
            bisect.insort(candidates, sp)
            candidates.pop()
    return candidates[:max(1, int(limit))], seen > max(1, int(limit))


def _read_text(path: Path) -> str | None:
    """Extract searchable text from a file. Plain text types are read directly;
    richer types (PDF, docx) go through the content extractor, which degrades to
    None if the optional library is missing. Returns None when there's nothing to
    index as content (the caller still records the filename/path). Large plain-text
    files are prefix-indexed so a log dump cannot freeze a scheduled crawl. """
    suffix = path.suffix.lower()
    max_bytes = _max_text_bytes()
    try:
        size = path.stat().st_size
    except Exception:
        return None
    if suffix in TEXT_EXTENSIONS:
        try:
            with path.open("rb") as fh:
                data = fh.read(max_bytes + 1)
            text = data[:max_bytes].decode("utf-8", errors="replace")
            if len(data) > max_bytes:
                text += "\n\n[... truncated by Mongoose file indexer; file is larger than the configured text limit ...]"
            return text
        except Exception:
            return None
    # Avoid trying expensive extractors on giant rich documents during unattended scheduled crawls.
    if size > max_bytes * 20:
        return None
    # PDF / docx and other extractable types via the content extractor.
    try:
        from .content_extract import extract_text, extractable
        if extractable(path.name):
            return extract_text(path)
    except Exception:
        return None
    return None


def _index_one_file(con, path: Path, device: str) -> str:
    """Index a single file (content if extractable, else by name). Returns one of
    'indexed' | 'skipped'. Shared by the full and resumable walks."""
    body = _read_text(path)
    if body is None:
        body = ""  # binary/unknown/inaccessible content: still findable by name
    try:
        mtime = path.stat().st_mtime
    except Exception:
        return "skipped"
    existing = con.execute("SELECT id, mtime FROM files WHERE device = ? AND path = ?", (device, str(path))).fetchone()
    if existing and float(existing["mtime"]) == float(mtime):
        return "skipped"
    if existing:
        rowid = existing["id"]
        old = con.execute("SELECT title, body, path FROM files WHERE id = ?", (rowid,)).fetchone()
        con.execute("UPDATE files SET title = ?, body = ?, mtime = ?, device = ?, indexed_at = ? WHERE id = ?", (path.name, body, mtime, device, utc_now_iso(), rowid))
        if old:
            _delete_fts_row(con, rowid, old["title"], old["body"], old["path"])
    else:
        cur = con.execute("INSERT INTO files(path, title, body, mtime, device, indexed_at) VALUES (?, ?, ?, ?, ?, ?)", (str(path), path.name, body, mtime, device, utc_now_iso()))
        rowid = cur.lastrowid
    con.execute("INSERT INTO file_fts(rowid, title, body, path) VALUES (?, ?, ?, ?)", (rowid, path.name, body, str(path)))
    return "indexed"


def _delete_fts_row(con, rowid: int, title: str, body: str, path: str) -> None:
    """Delete a row from the contentless FTS table using the old indexed values."""
    con.execute(
        "INSERT INTO file_fts(file_fts, rowid, title, body, path) VALUES('delete', ?, ?, ?, ?)",
        (rowid, title or "", body or "", path or ""),
    )


def _delete_file_path(con, *, device: str, path: str) -> bool:
    row = con.execute("SELECT id, title, body, path FROM files WHERE device=? AND path=?",
                      (device, path)).fetchone()
    if not row:
        return False
    _delete_fts_row(con, row["id"], row["title"], row["body"], row["path"])
    con.execute("DELETE FROM files WHERE id=?", (row["id"],))
    return True


def index_files(root: str, limit: int = 1000, device: str = "local",
                max_scan: int = 0, prune_stale: bool = True) -> dict[str, int]:
    """Index text files under `root`. `limit` caps indexed files; `max_scan` caps total
    files examined (default: 10x limit) so a folder of binary/skipped files cannot walk
    the entire tree before the index limit is hit, preventing large-folder hangs.

    When the walk completes without hitting a cap, stale rows for files that were deleted/renamed
    under this root are pruned from both the table and FTS. Without that, day-two search shows files
    that no longer exist, which feels like memory rot rather than helpful recall.
    """
    base = Path(root).expanduser().resolve()
    if not base.exists() or not base.is_dir():
        return {"indexed": 0, "skipped": 0, "scanned": 0, "pruned": 0, "device": device}
    if max_scan <= 0:
        max_scan = max(limit * 10, 5000)
    indexed = 0
    skipped = 0
    scanned = 0
    pruned = 0
    seen: set[str] = set()
    completed = True
    with connect() as con:
        for path in _safe_file_paths(base):
            if indexed >= limit or scanned >= max_scan:
                completed = False
                break
            if not path.is_file():
                continue
            scanned += 1
            sp = str(path)
            seen.add(sp)
            if _index_one_file(con, path, device) == "indexed":
                indexed += 1
            else:
                skipped += 1
        if prune_stale and completed:
            import os as _os
            prefix = str(base) + _os.sep
            rows = con.execute(
                "SELECT id, title, body, path FROM files WHERE device=? AND path LIKE ?",
                (device, prefix + "%"),
            ).fetchall()
            for row in rows:
                if row["path"] not in seen:
                    _delete_fts_row(con, row["id"], row["title"], row["body"], row["path"])
                    con.execute("DELETE FROM files WHERE id=?", (row["id"],))
                    pruned += 1
    audit("files", "index", True, payload={"root": str(base), "device": device,
                                            "indexed": indexed, "skipped": skipped,
                                            "scanned": scanned, "pruned": pruned,
                                            "completed": completed})
    return {"indexed": indexed, "skipped": skipped, "scanned": scanned,
            "pruned": pruned, "completed": completed, "device": device}


def index_files_resumable(root: str, *, device: str = "local", after_path: str = "",
                          limit: int = 1000) -> dict[str, Any]:
    """Resumable file walk: process files in sorted path order starting after `after_path`,
    up to `limit` files per call. Returns a cursor so large trees are crawled in bounded
    chunks without materialising the whole tree. We collect exactly limit+1 paths past the
    cursor using a fixed-size heap, then discard the sentinel."""
    base = Path(root).expanduser().resolve()
    if not base.exists() or not base.is_dir():
        return {"indexed": 0, "skipped": 0, "scanned": 0, "cursor": after_path, "more": False}
    indexed = skipped = scanned = 0
    cursor = after_path
    window, more = _bounded_sorted_after(base, after_path, max(1, int(limit)))
    with connect() as con:
        for sp in window:
            result = _index_one_file(con, Path(sp), device)
            scanned += 1
            if result == "indexed":
                indexed += 1
            else:
                skipped += 1
            cursor = sp
    audit("files", "index_resumable", True,
          payload={"root": str(base), "device": device, "indexed": indexed,
                   "scanned": scanned, "more": more})
    return {"indexed": indexed, "skipped": skipped, "scanned": scanned,
            "cursor": cursor, "more": more}



def prune_missing_files(root: str, *, device: str = "local", limit: int = 10000) -> dict[str, Any]:
    """Prune indexed rows for local files that no longer exist under ``root``.

    The resumable scheduled crawler walks a bounded path window each run, so it cannot safely build
    the complete ``seen`` set that ``index_files(..., prune_stale=True)`` uses. After the crawler reaches
    the end of the tree, run this bounded existence check so two-week-old deleted or renamed files do not
    keep appearing in search forever. ``complete`` is false when the limit is hit; the next end-of-tree
    pass will continue to clean up.
    """
    base = Path(root).expanduser().resolve()
    if not base.exists() or not base.is_dir():
        return {"checked": 0, "pruned": 0, "complete": True, "device": device}
    import os as _os
    prefix = str(base) + _os.sep
    checked = 0
    pruned = 0
    complete = True
    with connect() as con:
        rows = con.execute(
            "SELECT id, title, body, path FROM files WHERE device=? AND path LIKE ? ORDER BY path LIMIT ?",
            (device, prefix + "%", max(1, int(limit))),
        ).fetchall()
        for row in rows:
            checked += 1
            if not Path(row["path"]).exists():
                _delete_fts_row(con, row["id"], row["title"], row["body"], row["path"])
                con.execute("DELETE FROM files WHERE id=?", (row["id"],))
                pruned += 1
        # If we returned exactly the limit, there may be more rows to check. Keep this bounded so a
        # huge stale index cannot monopolise a scheduler tick.
        if checked >= max(1, int(limit)):
            total = con.execute(
                "SELECT COUNT(*) FROM files WHERE device=? AND path LIKE ?",
                (device, prefix + "%"),
            ).fetchone()[0]
            complete = checked >= int(total)
    audit("files", "prune_missing", True, payload={"root": str(base), "device": device,
                                                     "checked": checked, "pruned": pruned,
                                                     "complete": complete})
    return {"checked": checked, "pruned": pruned, "complete": complete, "device": device}

def recent_files(hours: int = 24, limit: int = 20) -> list[dict[str, Any]]:
    """Files modified within the last `hours` (by mtime), newest first. For the
    daily brief: a freshly-edited file often relates to today's work. Metadata
    only (path, title, mtime), no body."""
    import time
    cutoff = time.time() - hours * 3600
    with connect() as con:
        rows = con.execute(
            """
            SELECT id, path, title, mtime, device, indexed_at
            FROM files
            WHERE mtime >= ?
            ORDER BY mtime DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def ingest_device_files(files: list[dict[str, Any]], device: str = "phone") -> dict[str, int]:
    """Ingest a batch of file records PUSHED from another device (e.g. the Android
    agent). Unlike index_files (which reads a local folder), this takes metadata +
    optional text the remote device already extracted, and writes it into the same
    `files` table tagged with the device. This is the no-byte-copy path: the phone
    sends only its index, not the file contents.

    Each record: {path, filename|title, text?, mtime?, device_id?}. The remote
    `path` is stored as-is (it's the phone's path), so search shows where it lives
    on that device."""
    indexed = 0
    skipped = 0
    pruned = 0
    with connect() as con:
        for rec in files:
            path = str(rec.get("path") or "").strip()
            if not path:
                skipped += 1
                continue
            dev = str(rec.get("device_id") or device)
            if bool(rec.get("deleted")):
                if _delete_file_path(con, device=dev, path=path):
                    pruned += 1
                else:
                    skipped += 1
                continue
            title = str(rec.get("filename") or rec.get("title") or path.rsplit("/", 1)[-1])
            body = str(rec.get("text") or "")
            try:
                mtime = float(rec.get("mtime") or 0.0)
            except (TypeError, ValueError):
                mtime = 0.0
            existing = con.execute("SELECT id FROM files WHERE device = ? AND path = ?", (dev, path)).fetchone()
            if existing:
                rowid = existing["id"]
                old = con.execute("SELECT title, body, path FROM files WHERE id = ?", (rowid,)).fetchone()
                con.execute("UPDATE files SET title=?, body=?, mtime=?, device=?, indexed_at=? WHERE id=?",
                            (title, body, mtime, dev, utc_now_iso(), rowid))
                if old:
                    _delete_fts_row(con, rowid, old["title"], old["body"], old["path"])
            else:
                cur = con.execute(
                    "INSERT INTO files(path, title, body, mtime, device, indexed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (path, title, body, mtime, dev, utc_now_iso()))
                rowid = cur.lastrowid
            con.execute("INSERT INTO file_fts(rowid, title, body, path) VALUES (?, ?, ?, ?)",
                        (rowid, title, body, path))
            indexed += 1
    audit("files", "ingest_device", True, payload={"device": device, "indexed": indexed,
                                                        "skipped": skipped, "pruned": pruned})
    return {"indexed": indexed, "skipped": skipped, "pruned": pruned, "device": device}


def search_files(q: str, limit: int = 10) -> list[dict[str, str]]:
    match = safe_fts_query(q)
    if match is None:
        return []
    with connect() as con:
        rows = con.execute(
            """
            SELECT f.id, f.path, f.title, f.device, substr(f.body, 1, 500) AS snippet, f.indexed_at
            FROM file_fts x
            JOIN files f ON f.id = x.rowid
            WHERE file_fts MATCH ?
            ORDER BY bm25(file_fts), f.indexed_at DESC
            LIMIT ?
            """,
            (match, limit),
        ).fetchall()
        return [dict(row) for row in rows]
