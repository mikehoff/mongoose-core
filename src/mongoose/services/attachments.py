from __future__ import annotations

import base64
import hashlib
from email.message import Message
from typing import Any

from ..db import safe_fts_query, audit, connect, utc_now_iso

# Attachment index. The goal: find any attachment you've ever received, by
# filename or (later) by content, and jump to the email it came in.
#
# Phase 1 (this module, fully built): FINDABILITY. We record every attachment's
# metadata, filename, type, size, which email (sender/subject/date/mailbox),
# and a privacy tag, and make it searchable. Content extraction (reading the text
# inside a PDF/docx) is a deliberate follow-up: the schema already carries
# content_text + content_indexed, and index_attachment accepts extracted text, so
# turning it on later is additive, no migration.


def _dedupe_key(source: str, msg_id: str | None, filename: str, size: int | None) -> str:
    raw = "\x1f".join((source or "", msg_id or "", filename or "", str(size or "")))
    return "att:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def index_attachment(
    *,
    source: str,
    filename: str,
    source_message_id: str | None = None,
    email_id: int | None = None,
    mime_type: str | None = None,
    size_bytes: int | None = None,
    sender: str | None = None,
    subject: str | None = None,
    email_date: str | None = None,
    mailbox: str | None = None,
    privacy: str = "normal",
    content_text: str | None = None,
    payload_b64: str | None = None,
) -> dict[str, Any]:
    """Record one attachment's metadata (and optionally its extracted text).
    Idempotent: re-indexing the same attachment updates rather than duplicates."""
    if not filename or not filename.strip():
        return {"ok": False, "reason": "no_filename"}
    key = _dedupe_key(source, source_message_id, filename, size_bytes)
    content_indexed = 1 if content_text else 0
    with connect() as con:
        existing = con.execute(
            "SELECT id, filename, subject, sender, content_text FROM attachments WHERE dedupe_key = ?",
            (key,),
        ).fetchone()
        if existing:
            rowid = existing["id"]
            con.execute(
                "INSERT INTO attachment_fts(attachment_fts, rowid, filename, subject, sender, content_text) "
                "VALUES('delete', ?, ?, ?, ?, ?)",
                (rowid, existing["filename"], existing["subject"] or "",
                 existing["sender"] or "", existing["content_text"] or ""),
            )
            con.execute(
                """
                UPDATE attachments SET email_id=?, source_message_id=?, mime_type=?, size_bytes=?,
                    sender=?, subject=?, email_date=?, mailbox=?, content_text=?, content_indexed=?,
                    payload_b64=?, privacy=?, indexed_at=?
                WHERE id=?
                """,
                (email_id, source_message_id, mime_type, size_bytes, sender, subject, email_date,
                 mailbox, content_text, content_indexed, payload_b64, privacy, utc_now_iso(), rowid),
            )
        else:
            cur = con.execute(
                """
                INSERT INTO attachments(email_id, source, source_message_id, filename, mime_type,
                    size_bytes, sender, subject, email_date, mailbox, content_text, content_indexed,
                    payload_b64, privacy, dedupe_key, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (email_id, source, source_message_id, filename, mime_type, size_bytes, sender,
                 subject, email_date, mailbox, content_text, content_indexed, payload_b64, privacy, key,
                 utc_now_iso()),
            )
            rowid = cur.lastrowid
        con.execute(
            "INSERT INTO attachment_fts(rowid, filename, subject, sender, content_text) VALUES (?, ?, ?, ?, ?)",
            (rowid, filename, subject or "", sender or "", content_text or ""),
        )
    audit("attachments", "index", True, privacy=privacy,
          payload={"filename": filename, "source": source})
    return {"ok": True, "id": rowid, "content_indexed": bool(content_indexed)}


def extract_attachments_from_message(msg: Message, *, extract_content: bool = True) -> list[dict[str, Any]]:
    """Pull attachment metadata from a parsed email Message, and (when
    extract_content) the searchable TEXT inside PDF/Word attachments via the
    content extractor. Each dict is ready to pass to index_attachment. Content
    extraction is best-effort: a failure leaves content_text None and the
    attachment is still findable by filename."""
    out: list[dict[str, Any]] = []
    if not msg.is_multipart():
        return out
    for part in msg.walk():
        disposition = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        # An attachment is a part with a filename or an explicit attachment
        # disposition. Skip the body parts.
        if not filename and "attachment" not in disposition:
            continue
        if not filename:
            filename = "unnamed-attachment"
        payload = None
        size = None
        try:
            payload = part.get_payload(decode=True)
            if payload is not None:
                size = len(payload)
        except Exception:
            payload = None
        content_text = None
        if extract_content and payload:
            content_text = _extract_attachment_content(filename, payload)
        out.append({
            "filename": filename,
            "mime_type": part.get_content_type(),
            "size_bytes": size,
            "content_text": content_text,
            "payload_b64": base64.b64encode(payload).decode("ascii") if payload is not None else None,
        })
    return out


def _extract_attachment_content(filename: str, payload: bytes) -> str | None:
    """Write an attachment's bytes to a temp file and run the content extractor.
    Best-effort: returns None on any failure or unsupported type."""
    import os
    import tempfile
    from pathlib import Path
    try:
        from .content_extract import extractable, extract_text
    except Exception:
        return None
    if not extractable(filename):
        return None
    suffix = Path(filename).suffix.lower()
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        return extract_text(tmp)
    except Exception:
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass


def search_attachments(q: str, limit: int = 20) -> list[dict[str, Any]]:
    """Find attachments by filename, subject, sender, or extracted content.
    Returns metadata plus which email each came from, so you can locate and open
    the original. Privacy is returned so callers can mask sensitive ones."""
    match = safe_fts_query(q)
    if match is None:
        return []
    with connect() as con:
        rows = con.execute(
            """
            SELECT a.id, a.filename, a.mime_type, a.size_bytes, a.sender, a.subject,
                   a.email_date, a.mailbox, a.source, a.source_message_id, a.privacy,
                   a.content_indexed, a.email_id, a.payload_b64 IS NOT NULL AS downloadable
            FROM attachment_fts f
            JOIN attachments a ON a.id = f.rowid
            WHERE attachment_fts MATCH ?
            ORDER BY bm25(attachment_fts), a.email_date DESC
            LIMIT ?
            """,
            (match, limit),
        ).fetchall()
        return [dict(r) for r in rows]



def attachments_for_email(message_id: int, source_message_id: str | None = None) -> list[dict[str, Any]]:
    """Attachment metadata for a message. Bytes are only returned by the download helper."""
    with connect() as con:
        rows = con.execute(
            """
            SELECT id, filename, mime_type, size_bytes, sender, subject, email_date, mailbox,
                   source, source_message_id, privacy, content_indexed, email_id,
                   payload_b64 IS NOT NULL AS downloadable
            FROM attachments
            WHERE email_id = ? OR (? IS NOT NULL AND source_message_id = ?)
            ORDER BY filename ASC
            """,
            (message_id, source_message_id, source_message_id),
        ).fetchall()
        return [dict(r) for r in rows]


def attachment_payload(attachment_id: int) -> dict[str, Any] | None:
    """Return one stored attachment's bytes, or None if the item predates byte storage."""
    with connect() as con:
        row = con.execute(
            "SELECT id, filename, mime_type, payload_b64 FROM attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    if not data.get("payload_b64"):
        data["bytes"] = None
        return data
    try:
        data["bytes"] = base64.b64decode(data["payload_b64"])
    except Exception:
        data["bytes"] = None
    data.pop("payload_b64", None)
    return data

def get_attachment_full(attachment_id: int) -> dict[str, Any] | None:
    """Return one attachment's full record for retrieval: metadata + the REAL extracted content_text +
    payload_b64 (already base64). This is what lets a caller read the genuine file (e.g. an invoice PDF)
    instead of guessing at its contents. None if no such attachment."""
    with connect() as con:
        row = con.execute(
            "SELECT id, filename, mime_type, sender, subject, email_date, mailbox, "
            "content_text, content_indexed, payload_b64 FROM attachments WHERE id = ?",
            (attachment_id,),
        ).fetchone()
    return dict(row) if row else None


def attachment_stats() -> dict[str, int]:
    """Quick counts for doctor/CLI: total attachments and how many have content
    indexed (so the content-extraction follow-up's progress is visible)."""
    with connect() as con:
        total = con.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
        with_content = con.execute(
            "SELECT COUNT(*) FROM attachments WHERE content_indexed = 1").fetchone()[0]
    return {"total": total, "content_indexed": with_content}
