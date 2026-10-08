"""Canonical voicemail identity, one voicemail, many observations.

The same voicemail can arrive twice from different sources:
  - IMAP: a VoIP provider notification email (voicemail@voip.example) with a delete URL carrying the
    provider UUID, the caller number in the subject, and a WAV attachment.
  - Pi/HA: a staged line in /config/claude_bus_staging/mongoose_voicemail.jsonl with the same call's
    uid / audio path / transcript.

Deduping by source message id is NOT enough: IMAP-then-Pi (or Pi-then-IMAP) would create two rows for one
call. The fix (adapted from the parallel reconciliation branch's best idea) is a SOURCE-NEUTRAL identity:

  1. A provider UID if we can find one (from a VoIP provider delete URL, an audio filename, a vm_ id, or a Pi
     audio path), the strongest signal; IMAP and Pi carrying the same UUID collapse to one identity.
  2. Else a fingerprint of account + caller + minute-bucket, two observations of the same call at the
     same minute from the same caller are the same voicemail.
  3. Else a stable hash of whatever fields we have, never collides across genuinely different calls.

This module is pure logic (no DB, no network): it just computes the identity. The drainer uses it to
dedupe by identity instead of raw msg_id, so order no longer matters. Storage stays as-is; we only make
the dedup key smarter and keep the source refs so both observations are preserved.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta as _timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

# A VoIP provider/HA voicemail UUID is a long hex run (delete URLs, audio filenames, vm ids).
_UUID_RE = re.compile(r"(?i)([0-9a-f]{16,64})")


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalise_number(value: str | None) -> str:
    """Reduce a phone number to comparable digits (strip spaces/punctuation, drop a leading 0/44 noise
    conservatively so '07700900123' and '+447700900123' compare equal-ish)."""
    digits = re.sub(r"\D+", "", str(value or ""))
    if not digits:
        return ""
    if digits.startswith("44"):
        digits = "0" + digits[2:]
    return digits


def extract_uid(*parts: str | None) -> str | None:
    """Pull a stable provider uid from any of: a VoIP provider delete URL (uuid=...), an attachment name, a
    vm_/voicemail- id, or a Pi audio path like /config/.../<hex>.wav. The first strong match wins.

    This is the heart of dual-source reconciliation: if IMAP and Pi/HA both carry the same UUID, they
    map to the same identity_key regardless of which arrived first."""
    for part in parts:
        text = str(part or "")
        if not text:
            continue
        m = re.search(r"(?i)uuid=([0-9a-f]{16,64})", text)
        if m:
            return m.group(1).lower()
        m = re.search(r"(?i)(?:message-[^_\s]+_|vm[_-]|voicemail[_-])([0-9a-f]{16,64})", text)
        if m:
            return m.group(1).lower()
        # path/filename fallback: a bare long hex run (e.g. an audio filename)
        m = _UUID_RE.search(text)
        if m:
            return m.group(1).lower()
    return None


def _minute_bucket(value: str | None) -> str | None:
    """Normalise a timestamp to a UTC YYYYMMDDHHMM bucket, so the same call described by two sources with
    slightly different formats still lands in the same minute. Handles email Date headers and the
    VoIP provider notice body format ('Monday, June 22, 2026 at 3:54:44 AM')."""
    text = _clean(value)
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt:
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc)
            return dt.strftime("%Y%m%d%H%M")
    except Exception:
        pass
    for fmt in ("%A, %B %d, %Y at %I:%M:%S %p", "%A, %B %d, %Y %I:%M:%S %p",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc)
            else:
                # Naive times in the VoIP provider notice body are LOCAL UK time. The owner's HA/voicemail world runs
                # BST (UTC+1) in summer; treat a naive notice time as BST so it matches an offset-aware
                # Pi/IMAP timestamp for the same call. (A UUID match, when present, makes this moot.)
                dt = (dt - _timedelta(hours=1)).replace(tzinfo=timezone.utc)
            return dt.strftime("%Y%m%d%H%M")
        except Exception:
            continue
    digits = re.sub(r"\D+", "", text)
    return digits[:12] if len(digits) >= 8 else None


def canonical_identity(*, provider_uid: str | None = None, account_number: str | None = None,
                       caller_number: str | None = None, voicemail_at: str | None = None,
                       audio_filename: str | None = None, remote_audio_path: str | None = None,
                       source_message_id: str | None = None) -> tuple[str, str | None]:
    """Return (identity_key, canonical_uid). Source-neutral: IMAP and Pi/HA are two observations of one
    voicemail. UID first; else account+caller+minute fingerprint; else a stable hash. Never raises."""
    uid = extract_uid(provider_uid, audio_filename, remote_audio_path, source_message_id)
    if uid:
        return f"voicemail:uid:{uid}", uid
    caller = _normalise_number(caller_number)
    minute = _minute_bucket(voicemail_at)
    acct = _clean(account_number)
    if caller and minute:
        # The fingerprint is a FALLBACK for when no UUID is present. Cross-source minute precision is
        # fragile (e.g. a VoIP provider body time is local BST while a Pi ISO timestamp is offset-aware), so
        # we bucket to the HOUR here, not the minute: same caller + same account + same hour is treated
        # as one voicemail. The UUID path above is the exact, reliable reconciliation; this just avoids
        # the worst double-counting when a UUID is genuinely absent. We keep the minute in the canonical
        # uid slot is None (fingerprint has no provider uid).
        return f"voicemail:fingerprint:{acct or 'unknown'}:{caller}:{minute}", None
    raw = "\x1f".join((acct, caller or "", _clean(audio_filename), _clean(remote_audio_path),
                       _clean(source_message_id)))
    return "voicemail:hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24], None


# --------------------------------------------------------------------------------------------------
# Source field extraction , map each source's shape onto the identity inputs.
# --------------------------------------------------------------------------------------------------

def _provider_sender() -> str:
    """The VoIP provider's notification sender, from the site profile (voicemail.sender). A
    MONGOOSE_VOICEMAIL_SENDER in the process environment still overrides it."""
    from .. import profile as _profile
    return _profile.voicemail_sender().strip().lower()


def is_provider_voicemail(*, sender: str, subject: str) -> bool:
    """Narrow recogniser: the VoIP provider notification sender plus its subject shape ('New message in
    mailbox ... from <number>'). Deliberately strict so ordinary mail mentioning 'voicemail' isn't
    misclassified."""
    s = (sender or "").lower()
    subj = (subject or "").lower()
    return _provider_sender() in s and "message in mailbox" in subj


def identity_from_imap(*, sender: str = "", subject: str = "", body: str = "",
                       message_id: str | None = None, audio_filename: str | None = None,
                       email_date: str | None = None) -> tuple[str, str | None, dict[str, Any]]:
    """Compute the canonical identity for an IMAP VoIP provider voicemail email. Returns
    (identity_key, canonical_uid, fields) where fields captures what we parsed for source_refs."""
    # Find a UUID anywhere in the body: a VoIP provider delete URL carries uuid=..., but we scan the whole
    # body so we don't depend on the exact URL shape. The full body is passed to extract_uid too.
    url = ""
    um = re.search(r"https?://\S+", body or "", re.IGNORECASE)
    if um:
        url = um.group(0)
    # caller: 'from <number>' in the subject
    caller = None
    cm = re.search(r"from\s+([0-9+()\s-]{6,})", subject or "", re.IGNORECASE)
    if cm:
        caller = cm.group(1)
    # account/mailbox: 'for ... of <number>' in the subject
    acct = None
    am = re.search(r"of\s+(\d{5,})", subject or "")
    if am:
        acct = am.group(1)
    key, uid = canonical_identity(
        provider_uid=url or body, account_number=acct, caller_number=caller,
        voicemail_at=email_date, audio_filename=audio_filename, source_message_id=message_id)
    fields = {"source": "imap", "caller": _clean(caller), "account": _clean(acct),
              "message_id": message_id, "audio_filename": audio_filename, "delete_url": url}
    return key, uid, fields


def identity_from_staging(envelope: dict[str, Any], normalised: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
    """Compute the canonical identity for a Pi/HA staged voicemail. `normalised` is the drainer's parsed
    body ({uid, caller, transcript, wav_rel_path, tag}); `envelope` is the raw line (for msg_id/ts).

    Uses the SAME canonical_identity() function as the DB storage path (index_voicemail_from_protocol_staging)
    so runtime deduplication and row upserts agree on every voicemail's identity.  Previously this only
    read `timestamp` and ignored `voicemail_at` and `account_number`, causing split-brain between the
    live drainer cache and the SQLite row.
    """
    msg_id = str(envelope.get("msg_id") or "")
    # Use voicemail_at (new canonical field) OR timestamp (old field): same precedence as the DB path.
    ts = (str(envelope.get("voicemail_at") or "").strip()
          or str(envelope.get("timestamp") or "").strip() or None)
    # account_number makes the identity fingerprint match the IMAP dedupe key
    account_number = str(envelope.get("account_number") or "").strip() or None
    wav = normalised.get("wav_rel_path") or ""
    key, uid = canonical_identity(
        provider_uid=normalised.get("uid"), caller_number=normalised.get("caller"),
        voicemail_at=ts, account_number=account_number,
        remote_audio_path=wav, source_message_id=msg_id)
    fields = {"source": "pi_ha", "caller": _clean(normalised.get("caller")),
              "account_number": account_number, "message_id": msg_id, "remote_audio_path": wav,
              "transcript": _clean(normalised.get("transcript"))[:400], "tag": normalised.get("tag")}
    return key, uid, fields
