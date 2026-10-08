from __future__ import annotations

import base64
import email
import imaplib
import json
import re as _re
from email.message import Message
from html.parser import HTMLParser as _HTMLParser
from typing import Any

from pydantic import BaseModel, Field

from ..db import safe_fts_query, audit, connect, utc_now_iso
from ..settings import get_settings


class EmailIn(BaseModel):
    source: str = "manual"
    source_message_id: str | None = None
    mailbox: str | None = None
    sender: str | None = None
    recipients: str | None = None
    subject: str
    body: str
    sent_at: str | None = None
    received_at: str | None = None
    privacy: str = "medium"
    raw: dict[str, Any] = Field(default_factory=dict)


def _body_from_message(msg: Message) -> str:
    if msg.is_multipart():
        plain: list[str] = []
        html_parts: list[str] = []
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = part.get("Content-Disposition", "")
            if "attachment" in disposition:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if content_type == "text/plain":
                plain.append(text)
            elif content_type == "text/html":
                html_parts.append(text)
        if plain:
            return "\n".join(plain).strip()
        # HTML-only email (e.g. Docmail): store READABLE TEXT, not raw markup, so it reads cleanly
        # everywhere and FTS indexes words instead of tags. The raw message is preserved separately
        # (raw_rfc822_b64) for faithful .eml/.txt download.
        return html_to_text("\n".join(html_parts)).strip()
    payload = msg.get_payload(decode=True)
    if payload:
        charset = msg.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace").strip()
        if msg.get_content_type() == "text/html" or _looks_like_html(text):
            return html_to_text(text).strip()
        return text
    return str(msg.get_payload() or "")


def ingest_email(item: EmailIn) -> dict[str, Any]:
    # Items without an upstream Message-ID get a deterministic local id so
    # re-ingesting the same message doesn't create duplicates (SQLite allows
    # multiple NULLs, so a NULL-keyed dedupe never matches).
    msg_id = item.source_message_id
    if msg_id is None:
        import hashlib
        raw = "\x1f".join((item.source or "", item.mailbox or "", item.sender or "",
                           item.subject or "", item.body or "", item.sent_at or ""))
        msg_id = "local:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    raw_rfc822_b64 = item.raw.get("raw_rfc822_b64") if isinstance(item.raw, dict) else None
    with connect() as con:
        existing = con.execute(
            "SELECT id, subject, body, sender, mailbox FROM email_messages "
            "WHERE source = ? AND source_message_id = ?",
            (item.source, msg_id),
        ).fetchone()
        if existing:
            rowid = existing["id"]
            # Remove the OLD FTS entry first (contentless FTS5 needs the 'delete'
            # command carrying the old indexed column values), then re-insert.
            con.execute(
                "INSERT INTO email_fts(email_fts, rowid, subject, body, sender, mailbox) "
                "VALUES('delete', ?, ?, ?, ?, ?)",
                (rowid, existing["subject"], existing["body"], existing["sender"] or "", existing["mailbox"] or ""),
            )
            con.execute(
                """
                UPDATE email_messages SET mailbox=?, sender=?, recipients=?, subject=?, body=?,
                    sent_at=?, received_at=?, privacy=?, raw_json=?, raw_rfc822_b64=COALESCE(?, raw_rfc822_b64), indexed_at=?
                WHERE id=?
                """,
                (item.mailbox, item.sender, item.recipients, item.subject, item.body,
                 item.sent_at, item.received_at, item.privacy, json.dumps(item.raw), raw_rfc822_b64, utc_now_iso(), rowid),
            )
        else:
            cur = con.execute(
                """
                INSERT INTO email_messages(source, source_message_id, mailbox, sender, recipients, subject, body, sent_at, received_at, privacy, raw_json, raw_rfc822_b64, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item.source, msg_id, item.mailbox, item.sender, item.recipients,
                 item.subject, item.body, item.sent_at, item.received_at, item.privacy,
                 json.dumps(item.raw), raw_rfc822_b64, utc_now_iso()),
            )
            rowid = cur.lastrowid
        con.execute(
            "INSERT INTO email_fts(rowid, subject, body, sender, mailbox) VALUES (?, ?, ?, ?, ?)",
            (rowid, item.subject, item.body, item.sender or "", item.mailbox or ""),
        )
    audit("email", "ingest", True, privacy=item.privacy, payload={"subject": item.subject})
    return {"ok": True, "id": rowid}


def _parse_email_dt(value: str | None):
    """Parse an email date that may be ISO ("2026-07-06T09:00:00+00:00") or RFC 2822
    ("Wed, 6 May 2026 16:02:37 +0000"), returning a timezone-aware datetime or None. Both
    formats occur in email_messages depending on the source, so a caller comparing dates must
    parse, not string-compare."""
    if not value or not isinstance(value, str):
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _owner_email_identifiers() -> list[str]:
    """The owner's own email addresses, so the brief never surfaces mail the owner SENT as
    'new mail from <owner>' to themselves (a real bug: a Sent-folder sync made the brief tell
    M he had new mail from A Morgan)."""
    try:
        from ..settings import get_settings
        s = get_settings()
    except Exception:
        return []
    addrs: list[str] = []
    for attr in ("imap_username", "imap_user", "gmail_address", "microsoft_user",
                 "account_email", "owner_email", "from_address"):
        v = getattr(s, attr, None)
        if isinstance(v, str) and "@" in v:
            addrs.append(v.strip().lower())
    return list(dict.fromkeys(addrs))


def recent_email(hours: int = 24, limit: int = 20) -> list[dict[str, Any]]:
    """Emails received within the last `hours`, newest first. Used by the daily
    brief's deterministic signal layer. Returns metadata only (no body), and the
    `privacy` field so callers can decide what is safe to surface aloud.

    Recency is the EMAIL's own date, never when Mongoose indexed it: a mailbox re-sync
    re-stamps indexed_at on old mail, which previously made months-old email show up in the
    brief as 'new'. Mail the owner themselves sent is also excluded, so the owner is never
    reported as a correspondent in their own brief."""
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    owner_addrs = _owner_email_identifiers()
    # The stored dates are mixed-format: IMAP writes RFC 2822 ("Wed, 6 May 2026 16:02:37
    # +0000"), other sources write ISO. A SQL string comparison against an ISO cutoff is
    # therefore meaningless (a weekday-name string sorts after any digit, so EVERY row
    # passed, which is why months-old mail was surfaced as new). So pull the most-recently
    # indexed candidates and decide recency in Python by parsing the real email date.
    with connect() as con:
        rows = con.execute(
            """
            SELECT id, source, mailbox, sender, subject, sent_at, received_at, privacy, indexed_at
            FROM email_messages
            ORDER BY COALESCE(indexed_at, '') DESC
            LIMIT 500
            """
        ).fetchall()
    scored: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        d = dict(row)
        sender = (d.get("sender") or "").lower()
        if owner_addrs and any(a in sender for a in owner_addrs):
            continue  # mail the owner sent is not 'new mail' to glance at
        dt = _parse_email_dt(d.get("received_at")) or _parse_email_dt(d.get("sent_at"))
        if dt is None or dt < cutoff:
            continue  # recency is the EMAIL's own date, not when we indexed it
        d.pop("indexed_at", None)
        scored.append((dt, d))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [d for _dt, d in scored[:limit]]


def search_email(q: str, limit: int = 10) -> list[dict[str, Any]]:
    match = safe_fts_query(q)
    if match is None:
        return []
    with connect() as con:
        rows = con.execute(
            """
            SELECT m.id, m.source, m.mailbox, m.sender, m.subject, substr(m.body, 1, 500) AS snippet, m.sent_at, m.received_at, m.privacy
            FROM email_fts f
            JOIN email_messages m ON m.id = f.rowid
            WHERE email_fts MATCH ?
            ORDER BY bm25(email_fts), m.indexed_at DESC
            LIMIT ?
            """,
            (match, limit),
        ).fetchall()
        return [dict(row) for row in rows]



class _HTMLTextExtractor(_HTMLParser):
    """Minimal, dependency-free HTML->text. Drops script/style/head, turns block elements into line
    breaks and list items into bullets, decodes entities. Enough to make an HTML-only email (which
    stores its raw markup in `body`) READ as text instead of showing '<!DOCTYPE html ...>'."""
    _SKIP = {"script", "style", "head", "title", "meta", "link"}
    _BLOCK = {"p", "div", "br", "tr", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
              "table", "blockquote", "section", "article", "header", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self._parts.append("\n")
            if tag == "li":
                self._parts.append("• ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self._parts.append(data)

    def get_text(self) -> str:
        text = "".join(self._parts)
        text = _re.sub(r"[^\S\n]+", " ", text)   # collapse horizontal whitespace incl &nbsp; (\xa0)
        text = _re.sub(r" *\n *", "\n", text)
        text = _re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _looks_like_html(s: str) -> bool:
    if not s:
        return False
    head = s.lstrip()[:256].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        return True
    return bool(_re.search(r"<(?:html|body|table|div|p|br|a|span|td|tr|img|style)\b", s[:4000], _re.I))


def html_to_text(s: str) -> str:
    """HTML to readable plain text. Never raises: on any parser trouble it falls back to a crude tag
    strip so a message always shows SOMETHING readable rather than raw markup."""
    if not s:
        return ""
    try:
        # Pre-clean messy email HTML before parsing: drop comments (incl. MSO conditional comments),
        # and whole <style>/<script>/<head> blocks, so embedded CSS/JS never leaks into the text.
        cleaned = _re.sub(r"(?is)<!--.*?-->", " ", s)
        cleaned = _re.sub(r"(?is)<(style|script|head)\b[^>]*>.*?</\1>", " ", cleaned)
        p = _HTMLTextExtractor()
        p.feed(cleaned)
        p.close()
        out = p.get_text()
        if out:
            return out
    except Exception:
        pass
    return _re.sub(r"<[^>]+>", " ", s).strip()


def display_body(body: str | None) -> str:
    """Body as the owner should READ it: HTML emails become text, plain text stays as-is."""
    body = body or ""
    return html_to_text(body) if _looks_like_html(body) else body


def _mailbox_account() -> str:
    """Address of the mailbox Mongoose syncs (info@example.com), used to split sent from received."""
    try:
        return (getattr(get_settings(), "imap_username", "") or "").strip().lower()
    except Exception:
        return ""


def _decode_mime_header(value: str | None) -> str:
    """Decode RFC 2047 encoded-words in a header (e.g. '=?utf-8?Q?alex=5Fmorgan?=' -> 'alex_morgan').
    Never raises: returns the original string on any decode trouble."""
    if not value:
        return value or ""
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def latest_email(limit: int = 10, box: str = "inbox") -> list[dict[str, Any]]:
    """The newest N emails for an inbox-style glance (Outlook-like), split by box: box='inbox' is mail
    RECEIVED (sender is not this mailbox), box='sent' is mail SENT (sender IS this mailbox). Each row
    carries a text snippet plus MIME-decoded sender/recipients/subject. No full body: the row is a
    preview and the caller opens the full message via get_email_message on tap."""
    from datetime import datetime, timezone
    limit = max(1, min(int(limit or 10), 50))
    acct = _mailbox_account()
    where, params = "", []
    if acct:
        where = ("WHERE lower(COALESCE(sender,'')) LIKE ?" if box == "sent"
                 else "WHERE lower(COALESCE(sender,'')) NOT LIKE ?")
        params = [f"%{acct}%"]
    with connect() as con:
        rows = con.execute(
            f"""
            SELECT id, source, mailbox, sender, recipients, subject,
                   substr(body, 1, 4000) AS body_head, sent_at, received_at, privacy
            FROM email_messages
            {where}
            ORDER BY COALESCE(indexed_at, '') DESC
            LIMIT 400
            """,
            params,
        ).fetchall()
    floor = datetime.min.replace(tzinfo=timezone.utc)
    scored: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        d = dict(row)
        head = d.pop("body_head", "") or ""
        d["snippet"] = display_body(head)[:200].strip()
        d["sender"] = _decode_mime_header(d.get("sender"))
        d["recipients"] = _decode_mime_header(d.get("recipients"))
        d["subject"] = _decode_mime_header(d.get("subject"))
        dt = _parse_email_dt(d.get("received_at")) or _parse_email_dt(d.get("sent_at"))
        scored.append((dt or floor, d))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [d for _dt, d in scored[:limit]]


def _email_row(message_id: int) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute(
            """
            SELECT id, source, source_message_id, mailbox, sender, recipients, subject, body,
                   sent_at, received_at, privacy, raw_json, raw_rfc822_b64, indexed_at
            FROM email_messages WHERE id = ?
            """,
            (message_id,),
        ).fetchone()
    return dict(row) if row else None


def get_email_message(message_id: int, *, include_body: bool = True) -> dict[str, Any] | None:
    """Full local email detail for the cockpit search expansion."""
    row = _email_row(int(message_id))
    if not row:
        return None
    try:
        from .attachments import attachments_for_email
        attachments = attachments_for_email(int(message_id), row.get("source_message_id"))
    except Exception:
        attachments = []
    return {
        "id": row.get("id"),
        "source": row.get("source"),
        "mailbox": row.get("mailbox"),
        "sender": _decode_mime_header(row.get("sender")),
        "recipients": _decode_mime_header(row.get("recipients")),
        "subject": _decode_mime_header(row.get("subject")),
        "body": display_body(row.get("body")) if include_body else "",
        "sent_at": row.get("sent_at"),
        "received_at": row.get("received_at"),
        "privacy": row.get("privacy"),
        "indexed_at": row.get("indexed_at"),
        "attachments": attachments,
        "has_original_eml": bool(row.get("raw_rfc822_b64")),
    }


def _synthetic_eml(row: dict[str, Any]) -> bytes:
    """Build a usable RFC822-like export when the original raw message predates byte storage."""
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = row.get("subject") or "(no subject)"
    if row.get("sender"):
        msg["From"] = row.get("sender")
    if row.get("recipients"):
        msg["To"] = row.get("recipients")
    if row.get("sent_at") or row.get("received_at"):
        msg["Date"] = row.get("sent_at") or row.get("received_at")
    msg.set_content(row.get("body") or "")
    return msg.as_bytes()


def email_as_bytes(message_id: int, fmt: str = "txt") -> tuple[bytes, str, str] | None:
    """Return (bytes, media_type, filename) for txt/eml/pdf download."""
    row = _email_row(int(message_id))
    if not row:
        return None
    subject = (row.get("subject") or "email").strip() or "email"
    safe_subject = "".join(c if c.isalnum() or c in "-_. " else "_" for c in subject)[:80].strip() or "email"
    fmt = (fmt or "txt").lower()
    if fmt == "eml":
        raw = row.get("raw_rfc822_b64")
        if raw:
            try:
                return base64.b64decode(raw), "message/rfc822", f"{safe_subject}.eml"
            except Exception:
                pass
        return _synthetic_eml(row), "message/rfc822", f"{safe_subject}.eml"
    text = (
        f"Subject: {row.get('subject') or ''}\n"
        f"From: {row.get('sender') or ''}\n"
        f"To: {row.get('recipients') or ''}\n"
        f"Date: {row.get('sent_at') or row.get('received_at') or ''}\n"
        f"Mailbox: {row.get('mailbox') or ''}\n\n"
        f"{row.get('body') or ''}\n"
    )
    if fmt == "pdf":
        return _simple_pdf(text), "application/pdf", f"{safe_subject}.pdf"
    return text.encode("utf-8"), "text/plain; charset=utf-8", f"{safe_subject}.txt"


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _simple_pdf(text: str) -> bytes:
    """Tiny dependency-free text PDF generator for cockpit downloads."""
    import textwrap
    lines: list[str] = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        lines.extend(textwrap.wrap(raw, width=92, replace_whitespace=False, drop_whitespace=False) or [""])
    page_lines = [lines[i:i + 52] for i in range(0, len(lines), 52)] or [[""]]

    objects: dict[int, bytes] = {}
    pages_id = 2
    font_id = 3
    next_id = 4
    page_ids: list[int] = []

    for chunk in page_lines:
        content_lines = ["BT", "/F1 10 Tf", "50 790 Td", "14 TL"]
        for idx, line in enumerate(chunk):
            if idx:
                content_lines.append("T*")
            content_lines.append(f"({_pdf_escape(line)}) Tj")
        content_lines.append("ET")
        stream = "\n".join(content_lines).encode("latin-1", errors="replace")
        content_id = next_id; next_id += 1
        page_id = next_id; next_id += 1
        objects[content_id] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        objects[page_id] = f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode()
        page_ids.append(page_id)

    kids_ref = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    objects[pages_id] = f"<< /Type /Pages /Kids [{kids_ref}] /Count {len(page_ids)} >>".encode()
    objects[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    max_id = max(objects)
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0] * (max_id + 1)
    for obj_id in range(1, max_id + 1):
        offsets[obj_id] = len(out)
        out.extend(f"{obj_id} 0 obj\n".encode())
        out.extend(objects[obj_id])
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {max_id+1}\n0000000000 65535 f \n".encode())
    for obj_id in range(1, max_id + 1):
        out.extend(f"{offsets[obj_id]:010d} 00000 n \n".encode())
    out.extend(f"trailer\n<< /Size {max_id+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(out)

def _ingest_imap_message(raw: bytes, mailbox: str, imap_id: str = "") -> None:
    """Parse and index one raw RFC822 message plus its attachments. Shared by the
    legacy last-N sync and the UID-cursor incremental sync."""
    msg = email.message_from_bytes(raw)
    ingested = ingest_email(EmailIn(
        source="imap",
        source_message_id=msg.get("Message-ID"),
        mailbox=mailbox,
        sender=_decode_mime_header(msg.get("From")),
        recipients=_decode_mime_header(msg.get("To")),
        subject=_decode_mime_header(msg.get("Subject") or "(no subject)"),
        body=_body_from_message(msg),
        sent_at=msg.get("Date"),
        received_at=msg.get("Date"),
        privacy="medium",
        raw={"imap_id": imap_id, "raw_rfc822_b64": base64.b64encode(raw).decode("ascii")},
    ))
    email_id = ingested.get("id")
    indexed_attachments: list[dict[str, Any]] = []
    try:
        from .attachments import extract_attachments_from_message, index_attachment
        for att in extract_attachments_from_message(msg):
            got = index_attachment(
                source="imap",
                source_message_id=msg.get("Message-ID"),
                email_id=email_id if isinstance(email_id, int) else None,
                filename=att["filename"],
                mime_type=att.get("mime_type"),
                size_bytes=att.get("size_bytes"),
                sender=msg.get("From"),
                subject=msg.get("Subject") or "(no subject)",
                email_date=msg.get("Date"),
                mailbox=mailbox,
                privacy="medium",
                content_text=att.get("content_text"),
                payload_b64=att.get("payload_b64"),
            )
            merged = dict(att)
            if isinstance(got, dict) and got.get("ok"):
                merged["id"] = got.get("id")
            indexed_attachments.append(merged)
    except Exception:
        pass  # attachment indexing must never break email sync
    # If this is a VoIP provider voicemail notice, promote it into the searchable voicemail category linked
    # to the audio attachment (one row, reconciled with any Pi/HA-staged copy of the same call). Must
    # never break email sync.
    try:
        from .voicemail import index_voicemail_from_email
        index_voicemail_from_email(
            msg=msg, body=_body_from_message(msg), source="imap",
            source_message_id=msg.get("Message-ID"),
            email_id=email_id if isinstance(email_id, int) else None, mailbox=mailbox,
            attachments=indexed_attachments, privacy="medium",
        )
    except Exception:
        pass  # voicemail categorisation must never break email sync


def _imap_client(klass, host: str, port: int):
    """Create an IMAP client with a real network timeout where supported. Without this, a bad host or
    half-open connection can freeze mail sync with no progress hint."""
    try:
        return klass(host, port, timeout=20)
    except TypeError:
        return klass(host, port)


def _mail_progress(stage: str, message: str, **extra: Any) -> None:
    try:
        from ..mail_status import _set_progress
        payload = {"running": True, "stage": stage, "message": message}
        payload.update(extra)
        _set_progress(payload)
    except Exception:
        pass


def sync_imap_incremental(mailbox: str = "INBOX", *, last_uid: int = 0,
                          max_messages: int = 250) -> dict[str, Any]:
    """Efficient freshness: fetch ONLY messages with UID greater than last_uid,
    using the IMAP UID cursor, instead of re-pulling the last N every cycle. Honours
    UIDVALIDITY: if the mailbox's validity changed (renumbering), it signals a reset
    so the caller restarts the cursor. Returns the new highest UID seen."""
    settings = get_settings()
    if not settings.imap_host or not settings.imap_username or not settings.imap_password:
        return {"ok": False, "reason": "imap_not_configured"}
    klass = imaplib.IMAP4_SSL if settings.imap_use_ssl else imaplib.IMAP4
    count = 0
    highest = last_uid
    client = None
    try:
        _mail_progress("connecting", f"Connecting to IMAP {settings.imap_host}:{settings.imap_port}")
        try:
            client = _imap_client(klass, settings.imap_host, settings.imap_port)
        except (OSError, imaplib.IMAP4.error) as exc:
            # Dead host / refused / DNS: never raise into a scheduler or crawler. Clear the
            # progress state so the UI does not sit on "connecting" forever (external audit B2).
            _mail_progress("error", f"IMAP unreachable: {settings.imap_host}:{settings.imap_port}")
            return {"ok": False, "reason": "imap_unreachable",
                    "hint": f"could not reach {settings.imap_host}:{settings.imap_port}; "
                            f"check the mail host/port and network ({type(exc).__name__})"}
        _mail_progress("login", "Signing in to IMAP")
        client.login(settings.imap_username, settings.imap_password)
        _mail_progress("select", f"Opening mailbox {mailbox}")
        # readonly=True issues EXAMINE instead of SELECT: the server opens the mailbox read-only, so it
        # will not change any message flags no matter what we fetch. Combined with BODY.PEEK[] below,
        # this guarantees indexing can never mark the user's mail as read (defense in depth).
        status, sel = client.select(mailbox, readonly=True)
        if status != "OK":
            return {"ok": False, "reason": "select_failed"}
        # UIDVALIDITY check: if it changed, our stored UID cursor is meaningless.
        validity = None
        try:
            sv, vd = client.status(mailbox, "(UIDVALIDITY)")
            if sv == "OK" and vd:
                import re as _re
                m = _re.search(rb"UIDVALIDITY (\d+)", vd[0])
                validity = int(m.group(1)) if m else None
        except Exception:
            pass
        # Fetch UIDs strictly greater than the cursor.
        _mail_progress("search", f"Looking for new mail since UID {last_uid}")
        status, data = client.uid("search", None, f"UID {last_uid + 1}:*")
        if status != "OK":
            return {"ok": False, "reason": "uid_search_failed"}
        uids = data[0].split() if data and data[0] else []
        # UID n:* always returns at least the highest message even if <= last_uid;
        # filter to genuinely-new ones.
        new_uids = [u for u in uids if int(u) > last_uid][:max_messages]
        _mail_progress("fetch", f"Fetching {len(new_uids)} new message(s)", total=len(new_uids), done=0)
        for idx, uid in enumerate(new_uids, start=1):
            if idx == 1 or idx % 10 == 0 or idx == len(new_uids):
                _mail_progress("fetch", f"Fetching mail {idx}/{len(new_uids)}", total=len(new_uids), done=idx)
            # BODY.PEEK[] returns the full RFC822 message WITHOUT setting the \Seen flag on the server.
            # Plain (RFC822) / BODY[] implicitly marks the message read, which would silently flip the
            # user's unread mail to read just by Mongoose indexing it. Mongoose reads mail; it must never
            # change its read state. PEEK is the standard way to fetch non-destructively.
            status, msg_data = client.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            _ingest_imap_message(msg_data[0][1], mailbox, uid.decode(errors="ignore"))
            highest = max(highest, int(uid))
            count += 1
        return {"ok": True, "count": count, "highest_uid": highest,
                "uidvalidity": validity, "more": len(new_uids) >= max_messages}
    finally:
        try:
            client.logout()
        except Exception:
            pass


def sync_imap(mailbox: str = "INBOX", limit: int = 50) -> dict[str, Any]:
    settings = get_settings()
    if not settings.imap_host or not settings.imap_username or not settings.imap_password:
        return {"ok": False, "reason": "imap_not_configured"}

    klass = imaplib.IMAP4_SSL if settings.imap_use_ssl else imaplib.IMAP4
    count = 0
    client = None
    try:
        _mail_progress("connecting", f"Connecting to IMAP {settings.imap_host}:{settings.imap_port}")
        try:
            client = _imap_client(klass, settings.imap_host, settings.imap_port)
        except (OSError, imaplib.IMAP4.error) as exc:
            _mail_progress("error", f"IMAP unreachable: {settings.imap_host}:{settings.imap_port}")
            return {"ok": False, "reason": "imap_unreachable",
                    "hint": f"could not reach {settings.imap_host}:{settings.imap_port}; "
                            f"check the mail host/port and network ({type(exc).__name__})"}
        _mail_progress("login", "Signing in to IMAP")
        client.login(settings.imap_username, settings.imap_password)
        _mail_progress("select", f"Opening mailbox {mailbox}")
        client.select(mailbox, readonly=True)   # EXAMINE: never let indexing change message flags
        _mail_progress("search", "Listing messages")
        status, data = client.search(None, "ALL")
        if status != "OK":
            return {"ok": False, "reason": "search_failed"}
        ids = data[0].split()[-limit:]
        _mail_progress("fetch", f"Fetching {len(ids)} recent message(s)", total=len(ids), done=0)
        for idx, msg_id in enumerate(ids, start=1):
            if idx == 1 or idx % 10 == 0 or idx == len(ids):
                _mail_progress("fetch", f"Fetching mail {idx}/{len(ids)}", total=len(ids), done=idx)
            # BODY.PEEK[] fetches the full message without setting \Seen (see note above). Indexing mail
            # must not mark it read.
            status, msg_data = client.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK" or not msg_data:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            item = EmailIn(
                source="imap",
                source_message_id=msg.get("Message-ID"),
                mailbox=mailbox,
                sender=msg.get("From"),
                recipients=msg.get("To"),
                subject=msg.get("Subject") or "(no subject)",
                body=_body_from_message(msg),
                sent_at=msg.get("Date"),
                received_at=msg.get("Date"),
                privacy="medium",
                raw={"imap_id": msg_id.decode(errors="ignore"), "raw_rfc822_b64": base64.b64encode(raw).decode("ascii")},
            )
            ingested = ingest_email(item)
            email_id = ingested.get("id")
            # Index any attachments on this message (findability: filename, type,
            # size, and which email they're in). Content extraction is a follow-up.
            try:
                from .attachments import extract_attachments_from_message, index_attachment
                for att in extract_attachments_from_message(msg):
                    index_attachment(
                        source="imap",
                        source_message_id=msg.get("Message-ID"),
                        email_id=email_id if isinstance(email_id, int) else None,
                        filename=att["filename"],
                        mime_type=att.get("mime_type"),
                        size_bytes=att.get("size_bytes"),
                        sender=msg.get("From"),
                        subject=msg.get("Subject") or "(no subject)",
                        email_date=msg.get("Date"),
                        mailbox=mailbox,
                        privacy="medium",
                        content_text=att.get("content_text"),
                        payload_b64=att.get("payload_b64"),
                    )
            except Exception:
                pass  # attachment indexing must never break email sync
            # Voicemail promotion: same as _ingest_imap_message. If this email is a VoIP provider
            # voicemail notification, create/update a canonical voicemail record. Best-effort.
            try:
                from .attachments import extract_attachments_from_message
                from .voicemail import index_voicemail_from_email
                atts = [{"filename": a["filename"],
                         "mime_type": a.get("mime_type"),
                         "size_bytes": a.get("size_bytes"),
                         "payload_b64": a.get("payload_b64"),
                         "id": None}
                        for a in extract_attachments_from_message(msg)]
                index_voicemail_from_email(
                    msg=msg, body=item.body, source="imap",
                    source_message_id=msg.get("Message-ID"),
                    email_id=email_id if isinstance(email_id, int) else None,
                    mailbox=mailbox, attachments=atts,
                )
            except Exception:
                pass  # voicemail promotion must never break email sync
            count += 1
        return {"ok": True, "count": count}
    finally:
        try:
            client.logout()
        except Exception:
            pass
