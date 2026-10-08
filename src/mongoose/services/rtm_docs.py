"""RTM document ingest + the RTM subcontext.

Any file (receipt, invoice, contract, ticket, feedback, complaint, photo evidence, other evidence) is:
  1. stored on the Mac (local-first) with its type and provenance,
  2. text-extracted (PDF/Word via content_extract; images kept as evidence with filename metadata),
  3. indexed into the owner's UNIFIED file/document search (the shared `files` table), so it shows up
     in global search alongside everything else with zero extra wiring, and
  4. CORRELATED against the voicemails and IMAP emails already exposed to Mongoose (matched by flat,
     payment reference, amount and document number) so Mongoose starts building its own meaning of a
     flat's situation instead of holding isolated files.

Roles: RTM will have director / admin / leaseholder / auditor. Every document is stamped with the
uploader and their role now (the seam), but today only the owner ingests via the Cockpit. The
RTM-plugin upload path (owner/admin gated) and full role enforcement are the staged next step.
"""
from __future__ import annotations

import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .. import profile as _profile
from ..db import audit, connect, utc_now_iso

# The dropdown the Cockpit (and later the plugin) offers. Keep the keys stable; labels are UI-side.
DOC_TYPES: list[str] = [
    "receipt", "invoice", "contract", "ticket", "feedback",
    "complaint", "photo_evidence", "other_evidence", "export",
]
# Roles RTM recognises.
ROLES: list[str] = ["director", "admin", "leaseholder", "auditor", "owner"]
# Roles permitted to INGEST a document. Auditor is deliberately excluded: it is a read-only reviewer
# and may only attach notes (see add_note). Pinned with M 2026-07-08.
UPLOAD_ROLES: set[str] = {"owner", "director", "admin", "leaseholder"}


def can_upload(role: str) -> bool:
    """True if the role may ingest documents. Auditor (read-only) returns False."""
    return (role or "").strip().lower() in UPLOAD_ROLES

_MAX_BYTES = 25 * 1024 * 1024  # 25MB per document, a sane cap for receipts/invoices/photos
# The owner's local intake accepts any file: it is stored on the Mac, never web-served, so a .zip or a
# script here cannot execute and is harmless. The portal brief section 12.4 executable/markup allowlist
# and 10MB/5-file cap apply to the LEASEHOLDER web upload path (in the plugin), not this local store.


def _storage_dir() -> Path:
    base = os.getenv("MONGOOSE_DATA_DIR", "").strip()
    root = Path(base) if base else Path(os.path.expanduser("~/.mongoose/data"))
    d = root / "rtm_documents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure_tables(con) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS rtm_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_type TEXT NOT NULL,
            filename TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            content_hash TEXT,
            size_bytes INTEGER,
            mime TEXT,
            flat_id TEXT,
            uploaded_by TEXT,
            role TEXT,
            source TEXT,
            extracted_text TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS rtm_doc_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            ref TEXT,
            title TEXT,
            snippet TEXT,
            score REAL,
            why TEXT,
            created_at TEXT NOT NULL
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS rtm_doc_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER NOT NULL,
            author TEXT,
            role TEXT,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
    # Analysis columns, added by guarded ALTER so an existing live table gains them without a migration
    # tool: thumbnail path, image-classification labels (comma-joined), a short description, and the
    # timestamp of the last analysis pass (NULL/'' means it still needs analysing).
    existing = {r[1] for r in con.execute("PRAGMA table_info(rtm_documents)").fetchall()}
    # description: the owner's own short label for the document ("cctv signs", "stationery"). It used to
    # be typed into the Flat box because the upload form had no other field, so the flat column filled
    # up with descriptions that matched no flat.
    for col, decl in (("summary", "TEXT"), ("thumb_path", "TEXT"),
                      ("labels", "TEXT"), ("analysed_at", "TEXT"), ("description", "TEXT"),
                      ("total_override", "TEXT")):
        if col not in existing:
            con.execute(f"ALTER TABLE rtm_documents ADD COLUMN {col} {decl}")
    # One-off, idempotent: move any non-flat text out of flat_id into description.
    if "description" not in existing:
        for did, raw in con.execute("SELECT id, flat_id FROM rtm_documents WHERE COALESCE(flat_id,'')!=''").fetchall():
            canon = canonical_flat(raw)
            if canon:
                if canon != raw:
                    con.execute("UPDATE rtm_documents SET flat_id=? WHERE id=?", (canon, did))
            else:
                con.execute("UPDATE rtm_documents SET description=COALESCE(NULLIF(description,''),?), "
                            "flat_id=NULL WHERE id=?", (str(raw).strip(), did))
    con.execute("CREATE INDEX IF NOT EXISTS idx_rtm_docs_type ON rtm_documents(doc_type)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_rtm_docs_flat ON rtm_documents(flat_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_rtm_doc_links_doc ON rtm_doc_links(doc_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_rtm_doc_notes_doc ON rtm_doc_notes(doc_id)")


def _extract_text(path: Path, filename: str) -> str:
    """Best-effort text from a stored document. Tries the document extractor first (PDF/Word/etc); for
    images, falls back to on-device Apple Vision OCR so photographed contracts/receipts become full
    text. Returns '' only when nothing can read it: the file is still stored, named-searchable, and
    correlatable regardless."""
    try:
        from .content_extract import extract_text, extractable
        if extractable(filename):
            got = (extract_text(path) or "").strip()
            if got:
                return got
    except Exception:
        pass
    try:
        from .ocr import is_image, is_pdf, ocr_file
        # Reaches here only when the document extractor produced nothing: an image, or a scanned/photo
        # PDF with no text layer. Apple Vision OCR handles both (PDFs page by page via PDFKit).
        if is_image(filename) or is_pdf(filename):
            return ocr_file(str(path))
    except Exception:
        pass
    return ""


# ── entity extraction (for correlation): the distinctive tokens that tie a document to a call/email ──
_FLAT_RE = re.compile(r"\b(?:flat|apartment|apt|unit)\s*#?\s*([0-9]{1,3}[a-z]?)\b", re.IGNORECASE)
_REF_RE = re.compile(r"\b(?:ref(?:erence)?|invoice|receipt|document|doc|demand)\s*(?:no\.?|number|#|:)?\s*([A-Z0-9][A-Z0-9\-/]{3,})\b", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"£\s?([0-9][0-9,]*(?:\.[0-9]{2})?)")


def _entities(text: str, filename: str, flat_id: str | None) -> dict[str, list[str]]:
    blob = f"{filename}\n{text}"
    flats = set()
    if flat_id:
        flats.add(str(flat_id))
    flats.update(m.group(1) for m in _FLAT_RE.finditer(blob))
    refs = {m.group(1) for m in _REF_RE.finditer(blob) if len(m.group(1)) >= 4}
    amounts = {m.group(1) for m in _AMOUNT_RE.finditer(blob)}
    return {"flats": sorted(flats), "refs": sorted(refs), "amounts": sorted(amounts)}


# ── canonical RTM linkage: tie documents to the real flats/demands, not freeform text ──
# The block codes come from the site profile (e.g. 1EC/3EC in the fictional default). Compiled per block
# scheme and cached, so a profile change is picked up without re-importing this module.
@lru_cache(maxsize=4)
def _flat_res_for(codes: tuple[str, ...]) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    alt = "(?:" + "|".join(re.escape(c) for c in codes) + ")"
    canon = re.compile(rf"^({alt})F?([1-9])$")
    inline = re.compile(rf"\b({alt}F[1-9])\b")
    # A demand document number like 26-TO-DEC/<code>F1R0 (see rtm_example_court DOC_SCHEME_EXAMPLE).
    docnum = re.compile(rf"\b(\d{{2}}-[A-Z]{{2}}-[A-Z]{{3}}/{alt}F[1-9]R\d+)\b")
    embedded = re.compile(rf"({alt})F([1-9])")
    return canon, inline, docnum, embedded


def _site_key() -> tuple[Any, ...]:
    """Cache key for values derived from the site profile: the env vars that select the profile plus a
    2-second time bucket. The profile accessors re-resolve the profile path through get_settings()
    (which re-reads .env) on every call, too slow for the per-document and per-bank-line loops here."""
    import time
    return (os.environ.get("MONGOOSE_PROFILE_FILE"), os.environ.get("MONGOOSE_DB_PATH"),
            os.environ.get("MONGOOSE_DATA_DIR"), int(time.monotonic() // 2))


@lru_cache(maxsize=8)
def _site_for(_key: tuple[Any, ...]) -> dict[str, Any]:
    owner = _profile.lexicon("rtm.bank_desc_stopwords", ["morgan"]) or []
    return {"codes": tuple(_profile.block_codes()),
            "bank_stop": frozenset(_BANK_DESC_STOP | {w.lower() for w in _profile.rtm_name().split()}
                                   | {str(w).lower() for w in owner})}


def _flat_res() -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    return _flat_res_for(_site_for(_site_key())["codes"])


def canonical_flat(raw: str | None) -> str | None:
    """Normalise a flat reference to the canonical flat id (<block code>F1..F9, codes from the site
    profile), or None. Accepts '1ECF1', '1ecf1', '1EC1', 'Flat 3ECF9' (default-profile codes shown).
    Ambiguous inputs like a bare '6' return None on purpose."""
    if not raw:
        return None
    canon_re, _inline, _docnum, embedded_re = _flat_res()
    s = re.sub(r"[^0-9A-Za-z]", "", str(raw)).upper()
    m = canon_re.match(s)
    if m:
        return f"{m.group(1)}F{m.group(2)}"
    # embedded, e.g. 'FLAT3ECF9' from 'Flat 3ECF9'
    m2 = embedded_re.search(s)
    return f"{m2.group(1)}F{m2.group(2)}" if m2 else None


def _rtm_record_links(doc_id: int, flat_id: str | None, text: str, filename: str) -> list[dict[str, Any]]:
    """Correlate a document with the LIVE Example Court records (flats + demands), not just voicemails
    and emails. Matches by canonical flat id, by a flat's payment reference (surname) appearing in the
    text, and by an explicit demand document number in the text. This is what makes a receipt tie to
    the demand it actually pays, instead of a fuzzy full-text guess."""
    links: list[dict[str, Any]] = []
    try:
        from . import rtm_management as M
    except Exception:
        return links
    blob = f"{filename}\n{text}".upper()
    flats_hit: set[str] = set()
    cf = canonical_flat(flat_id)
    if cf:
        flats_hit.add(cf)
    _canon, inline_re, docnum_re, _emb = _flat_res()
    flats_hit.update(m.group(1) for m in inline_re.finditer(blob))

    try:
        flats = M.list_flats() or []
    except Exception:
        flats = []
    for f in flats:
        pr = (f.get("payment_reference") or "").upper()
        if pr and len(pr) >= 4 and pr in blob:
            if f.get("flat_id"):
                flats_hit.add(f["flat_id"])

    for fid in sorted(x for x in flats_hit if x):
        frow = next((f for f in flats if f.get("flat_id") == fid), None)
        who = f" ({frow.get('leaseholder_name')})" if frow and frow.get("leaseholder_name") else ""
        links.append({"doc_id": doc_id, "kind": "flat", "ref": fid, "title": f"Flat {fid}{who}",
                      "snippet": "", "score": 0.9, "why": f"document relates to flat {fid}"})
        try:
            demands = M.list_demands(flat=fid) or []
        except Exception:
            demands = []
        for dm in demands[:3]:
            dn = dm.get("document_number")
            if dn:
                links.append({"doc_id": doc_id, "kind": "demand", "ref": dn,
                              "title": f"Demand {dn} ({dm.get('status', '')})", "snippet": "",
                              "score": 0.85, "why": f"open demand for flat {fid}"})

    for dn in sorted({m.group(1) for m in docnum_re.finditer(blob)}):
        links.append({"doc_id": doc_id, "kind": "demand", "ref": dn, "title": f"Demand {dn}",
                      "snippet": "", "score": 0.95, "why": "demand number appears in this document"})
    return links


def _register_evidence(doc_id: int, doc_type: str, filename: str, flat_id: str | None,
                       stored_path: str) -> None:
    """Register the document in the GOVERNED RTM evidence store (rtm_mgmt_evidence) so it is not a
    parallel island: it then appears in referral packs, the management dashboard and evidence exports,
    linked to the flat. Only registers when we know the canonical flat; otherwise the document still
    lives in rtm_documents and global search. Best-effort."""
    cf = canonical_flat(flat_id)
    if not cf:
        return
    try:
        from . import rtm_management as M
        M.add_evidence(target_type="flat", target_id=cf, document_kind=f"intake_{doc_type}",
                       title=f"{doc_type.replace('_', ' ')}: {filename}",
                       path=stored_path, notes=f"flat={cf} rtm_doc={doc_id}")
    except Exception:
        pass


def _index_into_search(doc_id: int, doc_type: str, filename: str, text: str) -> None:
    """Upsert the document into the shared `files` table so it surfaces in the owner's unified/global
    search with no extra plumbing. device='rtm-doc' tags the source; the synthetic path is stable, so
    calling this again (e.g. after OCR backfill) UPDATES the same row rather than duplicating it.

    file_fts is a CONTENTLESS fts5 table: rows must be populated with the files rowid, and CANNOT be
    removed with a plain DELETE: they need the special 'delete' insert with the OLD indexed values,
    exactly as file_index does. We reuse file_index._delete_fts_row so both paths stay consistent."""
    try:
        from .file_index import _delete_fts_row
        title = f"[RTM {doc_type.replace('_', ' ')}] {filename}"
        body = text or filename
        path = f"rtm-doc://{doc_id}/{filename}"
        device = "rtm-doc"
        with connect() as con:
            existing = con.execute("SELECT id, title, body, path FROM files WHERE device=? AND path=?",
                                   (device, path)).fetchone()
            if existing:
                rowid = existing[0]
                _delete_fts_row(con, rowid, existing[1], existing[2], existing[3])
                con.execute("UPDATE files SET title=?, body=?, mtime=?, indexed_at=? WHERE id=?",
                            (title, body, 0.0, utc_now_iso(), rowid))
            else:
                cur = con.execute(
                    "INSERT INTO files(path, title, body, mtime, device, indexed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (path, title, body, 0.0, device, utc_now_iso()))
                rowid = cur.lastrowid
            con.execute("INSERT INTO file_fts(rowid, title, body, path) VALUES (?, ?, ?, ?)",
                        (rowid, title, body, path))
            con.commit()
    except Exception:
        pass  # search indexing is best-effort; the document is already stored and listable


def ingest(*, content: bytes, filename: str, doc_type: str, flat_id: str | None = None,
           uploaded_by: str = "owner", role: str = "owner", source: str = "cockpit",
           note: str = "", description: str = "") -> dict[str, Any]:
    """Store, extract, index into global search, and correlate a document. Returns a summary dict."""
    doc_type = (doc_type or "").strip().lower()
    if doc_type not in DOC_TYPES:
        return {"ok": False, "error": f"unknown doc_type; choose one of {', '.join(DOC_TYPES)}"}
    role = (role or "owner").strip().lower()
    if role not in ROLES:
        return {"ok": False, "error": f"unknown role; choose one of {', '.join(ROLES)}"}
    if not can_upload(role):
        return {"ok": False, "error": f"role '{role}' is read-only and cannot upload documents"}
    filename = (os.path.basename(filename or "document")).strip() or "document"
    if not content:
        return {"ok": False, "error": "empty file"}
    if len(content) > _MAX_BYTES:
        return {"ok": False, "error": f"file too large ({len(content) // (1024 * 1024)}MB; max 25MB)"}

    # Normalise the flat to the canonical Example Court id (1ECF1..3ECF9) when recognisable, so the
    # document links to real records. Anything else typed there is the owner describing the document
    # (the old upload form had only a Flat box), so it becomes the description, never a fake flat.
    raw_flat = (flat_id or "").strip()
    flat_id = canonical_flat(raw_flat)
    description = (description or "").strip() or ("" if flat_id else raw_flat)
    content_hash = hashlib.sha256(content).hexdigest()
    created = utc_now_iso()
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[-80:]
    stored = _storage_dir() / f"{content_hash[:12]}_{stem}"
    try:
        stored.write_bytes(content)
    except Exception as exc:
        return {"ok": False, "error": f"could not store file: {exc}"}

    text = _extract_text(stored, filename)
    mime = _guess_mime(filename)
    with connect() as con:
        _ensure_tables(con)
        cur = con.execute(
            "INSERT INTO rtm_documents(doc_type, filename, stored_path, content_hash, size_bytes, mime, "
            "flat_id, uploaded_by, role, source, extracted_text, note, created_at, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_type, filename, str(stored), content_hash, len(content), mime, (flat_id or None),
             uploaded_by, role, source, text, (note or None), created, (description or None)))
        doc_id = int(cur.lastrowid)
        con.commit()

    _index_into_search(doc_id, doc_type, filename, text)
    _register_evidence(doc_id, doc_type, filename, flat_id, str(stored))
    links = correlate(doc_id)
    audit("rtm", "document_ingested", True, route="rtm_docs",
          payload={"doc_type": doc_type, "flat_id": flat_id, "bytes": len(content), "links": len(links)})
    return {"ok": True, "id": doc_id, "doc_type": doc_type, "filename": filename,
            "size_bytes": len(content), "extracted_chars": len(text), "flat_id": flat_id,
            "correlations": len(links), "links": links[:6]}


def _guess_mime(filename: str) -> str:
    import mimetypes
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def reprocess(doc_id: int) -> dict[str, Any]:
    """Re-extract (OCR included) a stored document, refresh its text, its global-search index and its
    correlations. Used to backfill documents ingested before OCR existed. Best-effort and idempotent."""
    with connect() as con:
        _ensure_tables(con)
        row = con.execute("SELECT doc_type, filename, stored_path FROM rtm_documents WHERE id=?",
                          (doc_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "no such document"}
    doc_type, filename, stored_path = row[0], row[1], row[2]
    text = _extract_text(Path(stored_path), filename)
    with connect() as con:
        con.execute("UPDATE rtm_documents SET extracted_text=? WHERE id=?", (text, doc_id))
        con.commit()
    _index_into_search(doc_id, doc_type, filename, text)
    links = correlate(doc_id)
    return {"ok": True, "id": doc_id, "extracted_chars": len(text), "correlations": len(links)}


def delete_document(doc_id: int) -> dict[str, Any]:
    """Delete ONE uploaded document by its unique id and every trace of it: the row, its search-index +
    correlation + note + governed-evidence entries. The stored FILE is removed ONLY when no OTHER document
    row still references it , a re-uploaded duplicate points at the SAME content-hash file, so deleting one
    duplicate must never remove the original's bytes. Unknown id returns an error (never a silent no-op)."""
    from pathlib import Path as _Path
    with connect() as con:
        _ensure_tables(con)
        row = con.execute("SELECT id, stored_path, filename FROM rtm_documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"no document with id {doc_id}"}
        stored = row["stored_path"] if not isinstance(row, tuple) else row[1]
        filename = row["filename"] if not isinstance(row, tuple) else row[2]
        # 1) search index (files + contentless file_fts) , ONLY this doc's synthetic rtm-doc://{id}/ path
        try:
            from .file_index import _delete_fts_row
            for f in con.execute("SELECT id, title, body, path FROM files WHERE device='rtm-doc' AND path LIKE ?",
                                 (f"rtm-doc://{doc_id}/%",)).fetchall():
                _delete_fts_row(con, f[0], f[1], f[2], f[3])
                con.execute("DELETE FROM files WHERE id=?", (f[0],))
        except Exception:
            pass
        # 2) correlations + notes + governed evidence , each keyed on this unique doc_id
        for sql, params in (
            ("DELETE FROM rtm_doc_links WHERE doc_id=?", (doc_id,)),
            ("DELETE FROM rtm_doc_notes WHERE doc_id=?", (doc_id,)),
            ("DELETE FROM rtm_mgmt_evidence WHERE notes LIKE '%rtm_doc=' || ?", (doc_id,)),
        ):
            try:
                con.execute(sql, params)
            except Exception:
                pass
        con.execute("DELETE FROM rtm_documents WHERE id=?", (doc_id,))
        # 3) the file only if no sibling (duplicate) upload still points at it
        siblings = con.execute("SELECT COUNT(*) FROM rtm_documents WHERE stored_path=?", (stored,)).fetchone()[0]
        con.commit()
    file_removed = False
    if stored and int(siblings) == 0:
        try:
            p = _Path(stored)
            if p.exists():
                p.unlink()
                file_removed = True
        except Exception:
            pass
    try:
        audit("rtm", "document_deleted", True, route="rtm_docs",
              payload={"id": doc_id, "filename": filename, "file_removed": file_removed, "siblings_kept": int(siblings)})
    except Exception:
        pass
    return {"ok": True, "id": doc_id, "filename": filename, "file_removed": file_removed, "siblings_kept": int(siblings)}


def set_document_flat(doc_id: int, flat_id: str | None) -> dict[str, Any]:
    """Assign (or clear) a document's flat. The value is canonicalised to a real Example Court id when
    recognisable (so '7' -> the canonical id); a blank or unrecognised value (e.g. 'cleaning') CLEARS it
    rather than storing a bogus flat. Re-correlates so the flat link updates. Unknown id -> error."""
    fid = canonical_flat(flat_id) if (flat_id and str(flat_id).strip()) else None
    with connect() as con:
        _ensure_tables(con)
        row = con.execute("SELECT id FROM rtm_documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"no document with id {doc_id}"}
        con.execute("UPDATE rtm_documents SET flat_id=? WHERE id=?", (fid, doc_id))
        con.commit()
    try:
        correlate(doc_id)   # refresh correlations for the new/cleared flat
    except Exception:
        pass
    return {"ok": True, "id": doc_id, "flat_id": fid}


def set_document_description(doc_id: int, description: str | None) -> dict[str, Any]:
    """Set or clear the owner's short description of a document. Blank clears it."""
    text = re.sub(r"\s+", " ", str(description or "")).strip()[:200] or None
    with connect() as con:
        _ensure_tables(con)
        if not con.execute("SELECT id FROM rtm_documents WHERE id=?", (doc_id,)).fetchone():
            return {"ok": False, "error": f"no document with id {doc_id}"}
        con.execute("UPDATE rtm_documents SET description=? WHERE id=?", (text, doc_id))
        con.commit()
    return {"ok": True, "id": doc_id, "description": text or ""}


def set_document_total(doc_id: int, total: str | None) -> dict[str, Any]:
    """Set the document's total by hand (final: it overrides anything read from the file), or clear it
    with a blank to go back to the computed total. Accepts '40.65', '£40.65', '1,250'."""
    from decimal import Decimal, InvalidOperation
    raw = str(total or "").replace("£", "").replace(",", "").strip()
    value = None
    if raw:
        try:
            value = Decimal(raw).quantize(Decimal("0.01"))
        except InvalidOperation:
            return {"ok": False, "error": f"not an amount: {total!r}"}
        if value <= 0:
            return {"ok": False, "error": "the total must be more than zero"}
    with connect() as con:
        _ensure_tables(con)
        if not con.execute("SELECT id FROM rtm_documents WHERE id=?", (doc_id,)).fetchone():
            return {"ok": False, "error": f"no document with id {doc_id}"}
        con.execute("UPDATE rtm_documents SET total_override=? WHERE id=?",
                    (str(value) if value is not None else None, doc_id))
        con.commit()
    return {"ok": True, "id": doc_id, "total": f"£{value}" if value is not None else ""}


def reprocess_missing(limit: int = 100) -> dict[str, Any]:
    """OCR-backfill every stored document that has no extracted text yet (typically photos ingested
    before OCR). Returns how many gained text."""
    with connect() as con:
        _ensure_tables(con)
        rows = con.execute(
            "SELECT id FROM rtm_documents WHERE COALESCE(extracted_text,'')='' ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()
    ids = [r[0] for r in rows]
    gained = 0
    for did in ids:
        r = reprocess(did)
        if r.get("ok") and r.get("extracted_chars", 0) > 0:
            gained += 1
    return {"ok": True, "checked": len(ids), "gained_text": gained}


def documents_needing_analysis(limit: int = 20) -> list[int]:
    """Ids of documents that have not been through an analysis pass yet (thumbnail + labels + summary).
    A document needs analysis when analysed_at IS NULL or empty. Kept simple on purpose: re-analysis of
    changed files is out of scope here, so a done document is not revisited."""
    with connect() as con:
        _ensure_tables(con)
        rows = con.execute(
            "SELECT id FROM rtm_documents WHERE analysed_at IS NULL OR analysed_at='' "
            "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [r[0] for r in rows]


def analyse_document(doc_id: int) -> dict[str, Any]:
    """Give a document its thumbnail, image labels (photos only), and a short description, then stamp
    analysed_at. Idempotent (safe to re-run) and best-effort: it never raises to the caller, and a
    partial result (e.g. thumbnail but no model summary) still stamps so the scheduler moves on."""
    try:
        with connect() as con:
            _ensure_tables(con)
            row = con.execute(
                "SELECT id, doc_type, filename, stored_path, flat_id, mime, extracted_text "
                "FROM rtm_documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "no such document"}
        cols = ["id", "doc_type", "filename", "stored_path", "flat_id", "mime", "extracted_text"]
        doc = dict(zip(cols, row))
        from . import rtm_doc_analysis as A
        from .ocr import is_image

        thumb = A.generate_thumbnail(doc.get("stored_path") or "", doc_id)

        labels: list[str] = []
        filename = doc.get("filename") or ""
        mime = (doc.get("mime") or "").lower()
        if is_image(filename) or mime.startswith("image/"):
            labels = A.classify_image(doc.get("stored_path") or "")

        summary = A.summarise_document(doc, labels)

        analysed = utc_now_iso()
        with connect() as con:
            con.execute(
                "UPDATE rtm_documents SET thumb_path=?, labels=?, summary=?, analysed_at=? WHERE id=?",
                (thumb, ",".join(labels), summary, analysed, doc_id))
            con.commit()
        return {"ok": True, "id": doc_id, "thumb": bool(thumb), "labels": labels,
                "summary_chars": len(summary or ""), "analysed_at": analysed}
    except Exception as exc:
        return {"ok": False, "id": doc_id, "error": str(exc)[:200]}


def get_thumb_path(doc_id: int) -> str | None:
    """The stored thumbnail path for a document if one exists on disk, else None."""
    with connect() as con:
        _ensure_tables(con)
        row = con.execute("SELECT thumb_path FROM rtm_documents WHERE id=?", (doc_id,)).fetchone()
    if not row or not row[0]:
        return None
    return row[0] if os.path.exists(row[0]) else None


def recorrelate_recent(days: int = 30, limit: int = 200) -> dict[str, Any]:
    """Living correlation: re-run correlate() over recently-created documents so their links pick up
    voicemails, emails and records that arrived AFTER the document was ingested. Correlation is
    otherwise one-shot at ingest, but RTM situations unfold over weeks, so this keeps the subcontext
    current. Called on a schedule; safe to run repeatedly (correlate is idempotent per document)."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).isoformat()
    with connect() as con:
        _ensure_tables(con)
        rows = con.execute(
            "SELECT id FROM rtm_documents WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
            (cutoff, int(limit))).fetchall()
    ids = [r[0] for r in rows]
    total = 0
    for did in ids:
        total += len(correlate(did))
    return {"ok": True, "recorrelated": len(ids), "links": total}


def flat_situation(flat_id: str) -> dict[str, Any]:
    """What Mongoose knows about a flat from ingested documents: the documents plus what each one
    correlates to (records, demands, voicemails, emails). This is the RTM subcontext 'meaning' for a
    single flat, retrieval-backed so the brain answers from real rows rather than guessing."""
    cf = canonical_flat(flat_id) or (flat_id or "")
    docs = list_documents(flat_id=cf)
    out = [{**d, "related": related(d["id"])} for d in docs]
    return {"ok": True, "flat_id": cf, "document_count": len(docs), "documents": out}


def correlate(doc_id: int) -> list[dict[str, Any]]:
    """The RTM subcontext step: tie this document to the voicemails and emails already exposed to
    Mongoose by shared flat / reference / amount, and store the links. Idempotent per document."""
    with connect() as con:
        _ensure_tables(con)
        row = con.execute("SELECT doc_type, filename, flat_id, extracted_text FROM rtm_documents WHERE id=?",
                          (doc_id,)).fetchone()
        if not row:
            return []
        doc_type, filename, flat_id, text = row[0], row[1], row[2], (row[3] or "")
        con.execute("DELETE FROM rtm_doc_links WHERE doc_id=?", (doc_id,))
        con.commit()

    ents = _entities(text, filename, flat_id)
    terms: list[str] = []
    terms += [f"flat {f}" for f in ents["flats"]]
    terms += ents["refs"]
    terms += [f"£{a}" for a in ents["amounts"]]
    # De-dup while keeping order; cap the query fan-out.
    seen, queries = set(), []
    for t in terms:
        t = t.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            queries.append(t)
    queries = queries[:6]

    # First, the strong links: the live Example Court records (flats + demands) this document is about.
    links: list[dict[str, Any]] = _rtm_record_links(doc_id, flat_id, text, filename)
    # Then the softer links: voicemails and emails that share a flat / reference / amount.
    for q in queries:
        for kind, hits in (("voicemail", _safe_voicemails(q)), ("email", _safe_emails(q))):
            for h in hits[:2]:
                links.append({
                    "doc_id": doc_id, "kind": kind,
                    "ref": str(h.get("id") or h.get("message_id") or h.get("source_message_id") or ""),
                    "title": (h.get("subject") or h.get("caller") or h.get("from") or h.get("title") or kind),
                    "snippet": (h.get("snippet") or h.get("text") or h.get("body") or "")[:200],
                    "score": float(h.get("score") or 0.5),
                    "why": f"shares '{q}' with this {doc_type.replace('_', ' ')}",
                })
    # Dedup links by (kind, ref); keep the highest score.
    best: dict[tuple, dict] = {}
    for l in links:
        key = (l["kind"], l["ref"] or l["title"])
        if key not in best or l["score"] > best[key]["score"]:
            best[key] = l
    final = sorted(best.values(), key=lambda x: x["score"], reverse=True)[:12]

    now = utc_now_iso()
    with connect() as con:
        for l in final:
            con.execute(
                "INSERT INTO rtm_doc_links(doc_id, kind, ref, title, snippet, score, why, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (doc_id, l["kind"], l["ref"], l["title"], l["snippet"], l["score"], l["why"], now))
        con.commit()
    return final


def _safe_voicemails(q: str) -> list[dict[str, Any]]:
    try:
        from .voicemail import search_voicemails
        return search_voicemails(q, limit=3, include_email_fallback=False) or []
    except Exception:
        return []


def _safe_emails(q: str) -> list[dict[str, Any]]:
    try:
        from .email_index import search_email
        return search_email(q, limit=3) or []
    except Exception:
        return []


def add_note(*, doc_id: int, text: str, author: str = "owner", role: str = "owner") -> dict[str, Any]:
    """Attach a note to a document WITHOUT uploading a file. This is the auditor seam: an auditor is
    read-only for documents but may record observations here. Any recognised role may annotate."""
    role = (role or "owner").strip().lower()
    if role not in ROLES:
        return {"ok": False, "error": f"unknown role; choose one of {', '.join(ROLES)}"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty note"}
    with connect() as con:
        _ensure_tables(con)
        row = con.execute("SELECT id FROM rtm_documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "no such document"}
        cur = con.execute(
            "INSERT INTO rtm_doc_notes(doc_id, author, role, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (doc_id, author, role, text, utc_now_iso()))
        con.commit()
    audit("rtm", "document_note_added", True, route="rtm_docs",
          payload={"doc_id": doc_id, "role": role})
    return {"ok": True, "id": int(cur.lastrowid), "doc_id": doc_id, "role": role}


def list_notes(doc_id: int) -> list[dict[str, Any]]:
    with connect() as con:
        _ensure_tables(con)
        rows = con.execute(
            "SELECT id, author, role, text, created_at FROM rtm_doc_notes WHERE doc_id=? "
            "ORDER BY id DESC", (doc_id,)).fetchall()
        cols = ["id", "author", "role", "text", "created_at"]
        return [dict(zip(cols, r)) for r in rows]


_SORT_CODE_RE = re.compile(r"\b(\d{2}[-\s]\d{2}[-\s]\d{2})\b")
_ACCOUNT_RE = re.compile(r"(?:account|acc|a/c)\s*(?:number|no\.?|#)?\s*:?\s*(\d{8})\b", re.IGNORECASE)
def _key_facts(text: str, *, override: str | None = None, ledger_pence: int | None = None,
               ledger_shared: bool = False) -> dict[str, str]:
    """The money-critical facts a payment document should show at a glance (bank sort code, account
    number, total), so the card does not force you to open the file. The total comes from doc_totals,
    which confirms it by arithmetic, the linked payment or the owner, and says how sure it is."""
    from .doc_totals import format_total, resolve_total
    t = text or ""
    facts: dict[str, str] = {}
    m = _SORT_CODE_RE.search(t)
    if m:
        facts["sort_code"] = m.group(1).replace(" ", "-")
    m = _ACCOUNT_RE.search(t)
    if m:
        facts["account"] = m.group(1)
    r = resolve_total(t, override=override, ledger_pence=ledger_pence, ledger_shared=ledger_shared)
    if r.get("total") is not None:
        facts["total"] = format_total(r)
        facts["total_confidence"] = r["confidence"]
        facts["total_basis"] = r["basis"]
        for k in ("orders", "invoices"):
            if r.get(k):
                facts[k] = str(r[k])
    return facts


# Payment documents always show their total, marked with how sure it is. For contracts, certificates and
# other evidence, "the largest amount on the page" means nothing (a lease shows £140,000), so only a
# labelled or confirmed total is shown.
_ALWAYS_TOTAL_TYPES = {"invoice", "receipt"}


def _ledger_for_documents(con) -> tuple[dict[int, tuple[int, bool]], dict[str, list[int]], dict[str, int]]:
    """({doc_id: (payment pence, shared)}, {payable: [doc ids]}, {payable: pence}) for documents filed as
    evidence for a payable. shared = other documents are evidence for the same payment, so this one is
    only part of it."""
    links = _payable_linked_docs(con)
    by_pay: dict[str, list[int]] = {}
    for did, pays in links.items():
        for pid in pays:
            by_pay.setdefault(pid, []).append(did)
    amounts: dict[str, int] = {}
    if by_pay:
        marks = ",".join("?" * len(by_pay))
        try:
            for pid, pence, paid in con.execute(
                    f"SELECT id, amount_pence, paid_amount_pence FROM rtm_mgmt_payables WHERE id IN ({marks})",
                    list(by_pay)).fetchall():
                amounts[pid] = int(paid or pence or 0)
        except Exception:
            pass
    out: dict[int, tuple[int, bool]] = {}
    for pid, docs in by_pay.items():
        if pid in amounts:
            for did in docs:
                out[did] = (amounts[pid], len(docs) > 1)
    return out, by_pay, amounts


def list_documents(*, doc_type: str | None = None, flat_id: str | None = None,
                   limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    with connect() as con:
        _ensure_tables(con)
        sql = ("SELECT d.id, d.doc_type, d.filename, d.size_bytes, d.mime, d.flat_id, d.uploaded_by, "
               "d.role, d.source, d.note, d.created_at, "
               "d.summary, d.labels, d.thumb_path, d.extracted_text, "
               "(SELECT COUNT(*) FROM rtm_doc_links l WHERE l.doc_id=d.id) AS links, d.description, "
               "d.total_override "
               "FROM rtm_documents d WHERE 1=1")
        args: list[Any] = []
        if doc_type:
            sql += " AND d.doc_type=?"; args.append(doc_type)
        if flat_id:
            sql += " AND d.flat_id=?"; args.append(flat_id)
        sql += " ORDER BY d.id DESC LIMIT ? OFFSET ?"; args.append(int(limit)); args.append(max(0, int(offset)))
        rows = con.execute(sql, args).fetchall()
        ledger, by_pay, pay_amounts = _ledger_for_documents(con)
    out: list[dict[str, Any]] = []
    for r in rows:
        (did, dtype, fn, sz, mime, flat, upby, role, source, note, created,
         summary, labels_raw, thumb_path, extracted_text, links, description, total_override) = r
        lp, shared = ledger.get(did, (None, False))
        facts = _key_facts(extracted_text or "", override=total_override, ledger_pence=lp, ledger_shared=shared)
        if (dtype or "") not in _ALWAYS_TOTAL_TYPES and facts.get("total_confidence") == "unverified":
            for k in ("total", "total_confidence", "total_basis"):
                facts.pop(k, None)
        labels = [x.strip() for x in (labels_raw or "").split(",") if x.strip()]
        has_thumb = bool(thumb_path) and os.path.exists(thumb_path)
        # Key facts (sort code / account / total) are computed server-side and returned; the full
        # extracted_text is NOT shipped to the client (it can be large and is not needed for the card).
        out.append({"id": did, "doc_type": dtype, "filename": fn, "description": description or "",
                    "size_bytes": sz, "mime": mime,
                    "flat_id": flat, "uploaded_by": upby, "role": role, "source": source,
                    "note": note, "created_at": created, "links": links,
                    "summary": summary or "", "labels": labels, "has_thumb": has_thumb,
                    "key_facts": facts})
    _check_shared_payments(out, by_pay, pay_amounts)
    return out


def _check_shared_payments(docs: list[dict[str, Any]], by_pay: dict[str, list[int]],
                           amounts: dict[str, int]) -> None:
    """Documents that share one payment (five Amazon invoices for one £118.50 card charge) are checked
    together: if their totals add up to the payment, each is verified; if not, each is flagged. Only when
    every document of that payment is in this listing, so a partial page never raises a false alarm."""
    from decimal import Decimal
    by_id = {d["id"]: d for d in docs}
    for pid, members in by_pay.items():
        if len(members) < 2 or pid not in amounts or not all(m in by_id for m in members):
            continue
        facts = [by_id[m]["key_facts"] for m in members]
        if any(not f.get("total") for f in facts):
            continue
        got = sum(Decimal(f["total"].lstrip("£")) for f in facts)
        paid = Decimal(amounts[pid]) / 100
        for f in facts:
            if f.get("total_confidence") == "confirmed":
                continue
            if abs(got - paid) <= Decimal("0.01"):
                f["total_confidence"] = "verified"
                f["total_basis"] = (f.get("total_basis", "") + f"; with {len(members) - 1} other document(s) "
                                    f"it makes up the £{paid:.2f} payment").lstrip("; ")
            else:
                f["total_confidence"] = "conflict"
                f["total_basis"] = (f.get("total_basis", "") + f"; the {len(members)} documents for the £{paid:.2f} "
                                    f"payment add up to £{got}").lstrip("; ")


# Bank-line descriptions are noisy; strip the boilerplate so what's left is a real payee/surname token.
# The generic boilerplate lives here; the site-specific words are added at use time: the RTM company's
# own name (site profile) and the owner's legal-name tokens, which appear on his own transfers and are
# never a payee (lexicon "rtm.bank_desc_stopwords"; the fictional default is the sample owner surname).
_BANK_DESC_STOP = {"invoice", "card", "purchase", "outward", "faster", "inward",
                   "payment", "service", "charge", "received", "limited", "company", "park",
                   "rtm", "transfer", "direct", "debit", "standing", "order", "bank", "from", "with"}


def _bank_desc_stopwords() -> frozenset[str]:
    return _site_for(_site_key())["bank_stop"]


def _payee_tokens(desc: str) -> list[str]:
    stop = _bank_desc_stopwords()
    return [w for w in re.findall(r"[A-Za-z]{4,}", desc or "") if w.lower() not in stop]


def _text_has_amount(blob: str, pence: int) -> bool:
    """Does the text mention this money amount, DISTINCTIVELY enough to be a real signal? An amount with
    pennies (128.93, 44.45) is distinctive, so match the X.XX form. A small round amount (£3, £50, £60)
    appears everywhere, so it is NOT used as a match at all (rely on the payee instead); only whole pounds
    of £100+ are distinctive enough to match as a standalone number."""
    if pence <= 0:
        return False
    if pence % 100 != 0:
        return f"{pence / 100:.2f}" in blob
    if pence >= 10000:  # >= £100
        whole = pence // 100
        return f"{whole}.00" in blob or bool(re.search(rf"(?<![0-9.]){whole}(?![0-9.])", blob))
    return False


_EVIDENCE_DOC_RE = re.compile(r"rtm_doc=(\d+)$")
# An invoice number as banks and suppliers write it: INV0530, INV 0033, INV-0469, inv#12. Leading zeros are
# dropped so INV0530 and INV-530 compare equal.
_INVOICE_NO_RE = re.compile(r"\binv(?:oice)?[\s\-_#.:]*(?:no\.?\s*|number\s*)?0*(\d{2,})\b", re.I)


def _invoice_numbers(text: str) -> set[str]:
    return set(_INVOICE_NO_RE.findall(text or ""))


def link_document_to_payable(doc_id: int, payable_id: str, *, notes: str = "") -> dict[str, Any]:
    """Record that a filed document is the evidence for a payable (a supplier bill or a mapped bank
    payment). Stored as governed evidence, NOT in rtm_doc_links: correlate() rebuilds those links on a
    schedule, so a hand-made link there would silently vanish. The notes END with rtm_doc=<id>, the
    convention delete_document uses to remove the evidence with the document. Idempotent."""
    from . import rtm_management as M
    with connect() as con:
        _ensure_tables(con)
        row = con.execute("SELECT doc_type, filename, stored_path FROM rtm_documents WHERE id=?",
                          (int(doc_id),)).fetchone()
        if not row:
            return {"ok": False, "error": f"unknown document {doc_id}"}
        have = con.execute(
            "SELECT id FROM rtm_mgmt_evidence WHERE target_type='payable' AND target_id=? AND notes LIKE ?",
            (payable_id, f"%rtm_doc={int(doc_id)}")).fetchone()
    if have:
        return {"ok": True, "id": have[0], "already_linked": True}
    tail = f"rtm_doc={int(doc_id)}"
    return M.add_evidence(target_type="payable", target_id=payable_id, document_kind=row[0] or "document",
                          title=row[1] or f"RTM document {doc_id}", path=row[2] or "",
                          notes=(f"{notes.strip()} {tail}" if notes.strip() else tail))


def _payable_linked_docs(con, payable_id: str | None = None) -> dict[int, set[str]]:
    """{doc_id: {payable ids it is evidence for}}, from governed evidence rows. One payable, or all."""
    sql = "SELECT target_id, notes FROM rtm_mgmt_evidence WHERE target_type='payable'"
    params: tuple = ()
    if payable_id:
        sql += " AND target_id=?"
        params = (payable_id,)
    out: dict[int, set[str]] = {}
    try:
        rows = con.execute(sql, params).fetchall()
    except Exception:
        return out
    for target, note in rows:
        m = _EVIDENCE_DOC_RE.search((note or "").strip())
        if m:
            out.setdefault(int(m.group(1)), set()).add(str(target))
    return out


def documents_for_bank_line(matched_document_number: str | None, flat_id: str | None,
                            amount_pence: int | None, description: str | None,
                            *, limit: int = 4) -> list[dict[str, Any]]:
    """Related documents for a reconciled bank line: [{doc_id, doc_type, filename}], best first.
    Strong links first: docs correlated to the same demand document_number (rtm_doc_links kind='demand'),
    then docs recorded as evidence for the same payable (a mapped payment's own invoice). Only when the
    line has no recorded link does it GUESS, scoring every invoice/receipt/contract/other-evidence doc by
    payee-token-in-filename (+2, the doc is named after the payee), amount match (+2), payee-token-in-text
    (+1 each), and same flat (+1); keep only confident matches (score >= 2) to avoid false links. A guess
    never offers a document already linked to a DIFFERENT payable (it is that payment's evidence, not
    this one's), and an invoice number in the bank text narrows the guess to documents carrying it."""
    found: dict[int, dict[str, Any]] = {}
    with connect() as con:
        _ensure_tables(con)
        if matched_document_number:
            for r in con.execute(
                "SELECT d.id, d.doc_type, d.filename FROM rtm_doc_links l JOIN rtm_documents d ON d.id=l.doc_id "
                "WHERE l.kind='demand' AND l.ref=?", (str(matched_document_number),)).fetchall():
                found[r[0]] = {"doc_id": r[0], "doc_type": r[1], "filename": r[2]}
            if found:
                return list(found.values())[:limit]
            own = sorted(_payable_linked_docs(con, str(matched_document_number)))
            if own:
                marks = ",".join("?" * len(own))
                for r in con.execute(f"SELECT id, doc_type, filename FROM rtm_documents WHERE id IN ({marks}) "
                                     "ORDER BY id DESC", own).fetchall():
                    found[r[0]] = {"doc_id": r[0], "doc_type": r[1], "filename": r[2]}
                if found:
                    # Recorded evidence is not capped: a £118.50 card charge covering five invoices shows
                    # all five. The limit only bounds guesses.
                    return list(found.values())
        claimed = _payable_linked_docs(con)
        rows = con.execute(
            "SELECT id, doc_type, filename, flat_id, "
            "COALESCE(extracted_text,'')||' '||COALESCE(note,'')||' '||COALESCE(summary,'') AS body "
            "FROM rtm_documents WHERE doc_type IN ('invoice','receipt','contract','other_evidence') "
            "ORDER BY id DESC").fetchall()
    this = str(matched_document_number or "")
    rows = [r for r in rows if not (claimed.get(r[0]) and this not in claimed[r[0]])]
    wanted_invoices = _invoice_numbers(description or "")
    if wanted_invoices:
        named = [r for r in rows if wanted_invoices & _invoice_numbers(f"{r[2] or ''} {r[4] or ''}")]
        if named:
            rows = named
    toks = [t.lower() for t in _payee_tokens(description or "")]
    want = int(amount_pence or 0)
    flat = str(flat_id).strip().upper() if flat_id else ""
    scored: list[tuple[int, int, str, str]] = []
    for did, dtype, fn, dflat, body in rows:
        fnl = (fn or "").lower()
        textl = (body or "").lower()
        score = 0
        # Payee token in the FILENAME is the strongest signal (the doc is literally named after the payee).
        if toks and any(t in fnl for t in toks):
            score += 2
        if _text_has_amount(textl + " " + fnl, want):
            score += 2
        if flat and str(dflat or "").strip().upper() == flat:
            score += 2
        # Payee tokens in the body, matched as WHOLE WORDS so 'rent' can't hit 'current' nor 'sign' 'design'.
        score += sum(1 for t in toks if re.search(rf"\b{re.escape(t)}\b", textl))
        if score >= 2:
            scored.append((score, did, dtype, fn))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    # When a confident match exists (a distinctive amount or payee-in-filename gives score >= 3), drop the
    # weak single-generic-token matches (e.g. D&O insurance must not also pull in lift-insurance docs).
    top = scored[0][0] if scored else 0
    keep = max(2, top - 1)
    for s, did, dtype, fn in scored:
        if s < keep:
            break
        found.setdefault(did, {"doc_id": did, "doc_type": dtype, "filename": fn})
        if len(found) >= limit:
            break
    return list(found.values())[:limit]


def get_document(doc_id: int) -> dict[str, Any] | None:
    with connect() as con:
        _ensure_tables(con)
        row = con.execute(
            "SELECT id, doc_type, filename, stored_path, size_bytes, mime, flat_id, uploaded_by, role, "
            "source, note, created_at, extracted_text FROM rtm_documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return None
        cols = ["id", "doc_type", "filename", "stored_path", "size_bytes", "mime", "flat_id",
                "uploaded_by", "role", "source", "note", "created_at", "extracted_text"]
        doc = dict(zip(cols, row))
        doc["related"] = related(doc_id)
        doc["notes"] = list_notes(doc_id)
        return doc


def related(doc_id: int) -> list[dict[str, Any]]:
    with connect() as con:
        _ensure_tables(con)
        rows = con.execute(
            "SELECT kind, ref, title, snippet, score, why FROM rtm_doc_links WHERE doc_id=? "
            "ORDER BY score DESC", (doc_id,)).fetchall()
        cols = ["kind", "ref", "title", "snippet", "score", "why"]
        return [dict(zip(cols, r)) for r in rows]


def context_summary() -> dict[str, Any]:
    """The RTM subcontext at a glance: how many documents of each type, and how much Mongoose has been
    able to correlate with the calls and emails it already sees: the beginnings of its own meaning."""
    with connect() as con:
        _ensure_tables(con)
        total = con.execute("SELECT COUNT(*) FROM rtm_documents").fetchone()[0]
        by_type = dict(con.execute("SELECT doc_type, COUNT(*) FROM rtm_documents GROUP BY doc_type").fetchall())
        correlated = con.execute("SELECT COUNT(DISTINCT doc_id) FROM rtm_doc_links").fetchone()[0]
        link_total = con.execute("SELECT COUNT(*) FROM rtm_doc_links").fetchone()[0]
    return {"documents": total, "by_type": by_type, "documents_correlated": correlated,
            "correlations": link_total,
            "doc_types": DOC_TYPES}
