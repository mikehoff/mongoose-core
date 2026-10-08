from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from functools import lru_cache

from .. import profile as _profile
from ..db import audit, connect, safe_fts_query, utc_now_iso
from ..settings import get_settings

# Provider-specific recogniser for the VoIP provider emails the site's RTM line receives.
# Sample structure from the uploaded .eml:
#   From: VoIP Voicemail <voicemail@voip.example>
#   Subject: New message in mailbox for master group of 30000000 from 07700900123
#   Attachment: message-30000000g0_<uuid>.wav (audio/x-wav)
# The provider's notification sender and short token come from the site profile (voicemail.sender,
# voicemail.provider_token); MONGOOSE_VOICEMAIL_SENDER in the process environment still overrides the sender.


@lru_cache(maxsize=8)
def _sender_re(sender: str) -> re.Pattern[str]:
    return re.compile(r"\b" + re.escape(sender.strip()) + r"\b", re.IGNORECASE)


def _provider_from_re() -> re.Pattern[str]:
    """Matches the provider's notification sender address (profile voicemail.sender)."""
    return _sender_re(_profile.voicemail_sender())


def _provider_token() -> str:
    """The provider tag stored on voicemail rows (profile voicemail.provider_token)."""
    return _profile.voicemail_provider_token()


def _ha_source() -> str:
    """Default bus sender for Home Assistant voicemail envelopes: 'ha/<rtm slug with underscores>'."""
    return "ha/" + _profile.rtm_slug().replace("-", "_")


VOIP_PROVIDER_SUBJECT_RE = re.compile(
    r"^\s*New message in mailbox for\s+(?P<label>.+?)\s+of\s+(?P<account>\d+)\s+from\s+(?P<caller>.+?)\s*$",
    re.IGNORECASE,
)
VOIP_PROVIDER_BODY_RE = re.compile(
    r"new voice mail message in the mailbox for\s+(?P<label>.+?)\s+of account\s+(?P<account>\d+)\s+"
    r"from\s+(?P<caller>.+?)\s+on\s+(?P<when>.+?)(?:\.|\n)",
    re.IGNORECASE | re.DOTALL,
)
COUNTS_RE = re.compile(r"You currently have\s+(?P<new>\d+)\s+new messages?\s+and\s+(?P<old>\d+)\s+old messages?", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>\"']+")
UUID_RE = re.compile(r"(?i)([0-9a-f]{24,64})")
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".aac", ".amr"}

# Generic voicemail-like-email recogniser (broader than the VoIP provider-specific one above). Used by the
# search fallback so a voicemail email that the promoter did not recognise still surfaces. Kept to
# unambiguous voicemail cues to avoid dragging ordinary mail into the voicemail category.
@lru_cache(maxsize=8)
def _vm_sender_re_for(token: str) -> re.Pattern[str]:
    return re.compile(
        r"voice[\s-]?mail|" + re.escape(token) + r"|\bvmail\b|voice[\s-]?message|\bvms\b",
        re.IGNORECASE)


def _vm_email_sender_re() -> re.Pattern[str]:
    """Voicemail-ish sender cues, including the site's provider token (profile voicemail.provider_token)."""
    return _vm_sender_re_for(_provider_token())
_VM_EMAIL_TEXT_RE = re.compile(
    r"\bvoice[\s-]?mail\b|\bvoicemails?\b"
    r"|\bvoice message\b|\bnew voice message\b"
    r"|\byou have (?:a )?(?:new )?voice[\s-]?mail\b|\bvoice[\s-]?mail from\b"
    r"|\bnew voicemail\b|\bvoicemail received\b|\bvoicemail notification\b"
    r"|\bnew message in (?:the )?mailbox\b"
    r"|\bmissed (?:a )?call\b|\bmissed call from\b"
    r"|\bleft (?:you )?a (?:voice ?)?message\b"
    r"|\baudio message\b|\bphone message\b",
    re.IGNORECASE | re.DOTALL,
)


def _custom_voicemail_markers() -> tuple[list[str], list[str]]:
    """Owner-supplied extra markers so an unusual provider is recognised without a code change:
    MONGOOSE_VOICEMAIL_EMAIL_SENDERS and MONGOOSE_VOICEMAIL_EMAIL_MARKERS are comma-separated,
    case-insensitive substrings matched against the sender and the subject+snippet respectively."""
    import os
    def _parts(name: str) -> list[str]:
        return [s.strip().lower() for s in (os.getenv(name, "") or "").split(",") if s.strip()]
    return _parts("MONGOOSE_VOICEMAIL_EMAIL_SENDERS"), _parts("MONGOOSE_VOICEMAIL_EMAIL_MARKERS")


def _email_looks_like_voicemail(sender: Any, subject: Any, snippet: Any,
                                sender_re: re.Pattern[str] | None = None) -> bool:
    """True if an email looks like a voicemail notice, by sender or by subject/body cues (plus any
    owner-configured provider markers). Loops pass `sender_re` (from _vm_email_sender_re()) so the
    profile is read once per scan, not once per row."""
    sender_s = str(sender or "")
    text_s = f"{subject or ''} {snippet or ''}"
    if (sender_re or _vm_email_sender_re()).search(sender_s):
        return True
    if _VM_EMAIL_TEXT_RE.search(text_s):
        return True
    custom_senders, custom_markers = _custom_voicemail_markers()
    low_sender = sender_s.lower()
    if any(pat in low_sender for pat in custom_senders):
        return True
    low_text = text_s.lower()
    return any(pat in low_text for pat in custom_markers)


@dataclass
class VoicemailDetection:
    provider: str
    mailbox_label: str | None
    account_number: str | None
    caller_number: str | None
    voicemail_at: str | None
    provider_uid: str | None
    manage_url: str | None
    delete_url: str | None
    new_message_count: int | None
    old_message_count: int | None
    notice_text: str


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalise_number(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith("+"):
        return "+" + re.sub(r"\D+", "", value[1:])
    digits = re.sub(r"\D+", "", value)
    return digits or value


def _first_url_matching(text: str, needle: str) -> str | None:
    for url in URL_RE.findall(text or ""):
        if needle in url:
            return url.rstrip(".)]")
    return None


def _parse_counts(text: str) -> tuple[int | None, int | None]:
    m = COUNTS_RE.search(text or "")
    if not m:
        return None, None
    try:
        return int(m.group("new")), int(m.group("old"))
    except Exception:
        return None, None


def _extract_uid_from_text(*parts: str | None) -> str | None:
    """Extract a stable provider uid from VoIP provider delete URLs, attachment names, HA uid/path fields.

    This is the key to dual-source reconciliation: if the IMAP email and Pi/HA staging line both carry
    the same VoIP provider UUID, they must collapse into one canonical voicemail identity.
    """
    for part in parts:
        text = str(part or "")
        if not text:
            continue
        m = re.search(r"(?i)uuid=([0-9a-f]{16,64})", text)
        if m:
            return m.group(1).lower()
        m = re.search(r"(?i)(?:message-[^_\s]+_|vm_|voicemail[_-])([0-9a-f]{16,64})", text)
        if m:
            return m.group(1).lower()
        # Fallback for paths like /config/.../2fb7242e082749b9a5107dd849ccd04d.wav.
        m = UUID_RE.search(text)
        if m:
            return m.group(1).lower()
    return None


def _minute_bucket(value: str | None) -> str | None:
    text = _clean(value)
    if not text:
        return None
    # Email dates first.
    try:
        dt = parsedate_to_datetime(text)
        if dt:
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc)
            return dt.strftime("%Y%m%d%H%M")
    except Exception:
        pass
    # VoIP provider notice body: Monday, June 22, 2026 at 3:54:44 AM.
    for fmt in (
        "%A, %B %d, %Y at %I:%M:%S %p",
        "%A, %B %d, %Y %I:%M:%S %p",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc)
            return dt.strftime("%Y%m%d%H%M")
        except Exception:
            continue
    # Weak fallback: pull enough date/time digits to avoid cross-day caller collisions.
    digits = re.sub(r"\D+", "", text)
    return digits[:12] if len(digits) >= 8 else None


def canonical_identity_key(*, provider_uid: str | None = None, account_number: str | None = None,
                           caller_number: str | None = None, voicemail_at: str | None = None,
                           audio_filename: str | None = None, remote_audio_path: str | None = None,
                           source_message_id: str | None = None) -> tuple[str, str | None]:
    """Return (identity_key, canonical_uid).

    The identity key is intentionally source-neutral. IMAP and Pi/HA staging are just two observations
    of the same voicemail. Prefer a provider UID when present; otherwise fall back to account+caller+minute,
    then to a stable audio/source fingerprint.
    """
    uid = _extract_uid_from_text(provider_uid, audio_filename, remote_audio_path, source_message_id)
    if uid:
        return f"voicemail:uid:{uid}", uid
    caller = _normalise_number(caller_number)
    minute = _minute_bucket(voicemail_at)
    acct = _clean(account_number)
    if caller and minute:
        return f"voicemail:fingerprint:{acct or 'unknown'}:{caller}:{minute}", None
    raw = "\x1f".join((acct, caller or "", _clean(audio_filename), _clean(remote_audio_path), _clean(source_message_id)))
    return "voicemail:hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24], None


def detect_voicemail_message(msg: Message, body: str | None = None) -> VoicemailDetection | None:
    """Return provider metadata if this email is a recognised voicemail notice.

    Detection is deliberately narrow: sender + subject/body format + later an audio attachment. This avoids
    misclassifying ordinary email that happens to mention voicemail.
    """
    sender = _clean(msg.get("From") or msg.get("Return-Path") or "")
    subject = _clean(msg.get("Subject") or "")
    text = body if body is not None else ""
    from_ok = bool(_provider_from_re().search(sender))
    sm = VOIP_PROVIDER_SUBJECT_RE.match(subject)
    bm = VOIP_PROVIDER_BODY_RE.search(text or "")
    if not (from_ok and (sm or bm)):
        return None

    label = _clean((sm or bm).group("label")) if (sm or bm) else None
    account = _clean((sm or bm).group("account")) if (sm or bm) else None
    caller = _normalise_number(_clean((sm or bm).group("caller"))) if (sm or bm) else None
    when = _clean(bm.group("when")) if bm else None
    # Fallback: use the email Date header as voicemail_at so the IMAP identity fingerprint
    # (account + caller + minute) matches the Pi/HA one, which is built from the same call's timestamp.
    # Without this, `when` is None from body text, forcing a hash identity that never reconciles.
    if not when:
        raw_date = msg.get("Date") or ""
        if raw_date:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(raw_date)
                when = dt.strftime("%Y-%m-%dT%H:%M:%S%z")
            except Exception:
                pass
    delete_url = _first_url_matching(text or "", "delete_voicemail")
    new_count, old_count = _parse_counts(text or "")
    return VoicemailDetection(
        provider=_provider_token(),
        mailbox_label=label,
        account_number=account,
        caller_number=caller,
        voicemail_at=when,
        provider_uid=_extract_uid_from_text(delete_url, subject, text),
        manage_url=_first_url_matching(text or "", "vm-view"),
        delete_url=delete_url,
        new_message_count=new_count,
        old_message_count=old_count,
        notice_text=(text or "").strip(),
    )


def _is_audio_attachment(att: dict[str, Any]) -> bool:
    mime = str(att.get("mime_type") or "").lower()
    filename = str(att.get("filename") or "")
    return mime.startswith("audio/") or Path(filename).suffix.lower() in AUDIO_EXTS


def _pick_audio_attachment(attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
    for att in attachments:
        if _is_audio_attachment(att):
            return att
    return None


def _source_specific_dedupe_key(source: str, source_message_id: str | None, audio_filename: str | None,
                                caller_number: str | None, voicemail_at: str | None) -> str:
    raw = "\x1f".join((source or "", source_message_id or "", audio_filename or "",
                       caller_number or "", voicemail_at or ""))
    return "vm_source:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _loads_refs(raw: str | None) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw or "[]")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception:
        pass
    return []


def _merge_refs(existing_raw: str | None, ref: dict[str, Any]) -> str:
    refs = _loads_refs(existing_raw)
    signature = json.dumps({k: ref.get(k) for k in sorted(ref) if k not in {"seen_at"}}, sort_keys=True, default=str)
    for old in refs:
        old_sig = json.dumps({k: old.get(k) for k in sorted(old) if k not in {"seen_at"}}, sort_keys=True, default=str)
        if old_sig == signature:
            # Refresh seen_at but keep original shape.
            old["seen_at"] = ref.get("seen_at") or old.get("seen_at")
            return json.dumps(refs, sort_keys=True)
    refs.append(ref)
    return json.dumps(refs[-20:], sort_keys=True)


def _source_label(refs_raw: str | None, fallback: str) -> str:
    refs = _loads_refs(refs_raw)
    kinds = sorted({str(r.get("kind") or r.get("source") or "").strip() for r in refs if r.get("kind") or r.get("source")})
    if len(kinds) > 1:
        return "+".join(kinds)
    return kinds[0] if kinds else fallback


def _delete_fts(con, row: Any) -> None:
    con.execute(
        "INSERT INTO voicemail_fts(voicemail_fts, rowid, provider, mailbox_label, account_number, caller_number, subject, notice_text, transcript, audio_filename) "
        "VALUES('delete', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["id"], row["provider"] or "", row["mailbox_label"] or "",
         row["account_number"] or "", row["caller_number"] or "", row["subject"] or "",
         row["notice_text"] or "", row["transcript"] or "", row["audio_filename"] or ""),
    )


def _insert_fts(con, rowid: int, *, provider: str | None, mailbox_label: str | None, account_number: str | None,
                caller_number: str | None, subject: str | None, notice_text: str | None,
                transcript: str | None, audio_filename: str | None) -> None:
    con.execute(
        "INSERT INTO voicemail_fts(rowid, provider, mailbox_label, account_number, caller_number, subject, notice_text, transcript, audio_filename) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rowid, provider or "", mailbox_label or "", account_number or "", caller_number or "",
         subject or "", notice_text or "", transcript or "", audio_filename or ""),
    )


def _which(binary: str) -> str | None:
    """Locate a binary. Falls back to common bin dirs because the server is often
    launched by launchd with a minimal PATH that omits /opt/homebrew/bin, so a plain
    shutil.which() would wrongly report the transcriber as missing at runtime."""
    import shutil as _shutil
    found = _shutil.which(binary)
    if found:
        return found
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        cand = os.path.join(d, binary)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _resolve_ggml_model(model: str) -> str | None:
    """Find a whisper.cpp ggml model file for the configured model name (e.g. base.en).
    whisper.cpp needs a path to a .bin, not a bare model name."""
    import glob as _glob
    name = (model or "base.en").strip()
    stems = [f"ggml-{name}.bin"]
    if not name.startswith("ggml"):
        stems.append(f"ggml-{name}.bin")
    dirs = [
        os.path.expanduser("~/.mongoose/data/models"),
        os.path.expanduser("~/.mongoose/data"),
        "/opt/homebrew/share/whisper-cpp",
        "/opt/homebrew/share/whisper.cpp",
        "/usr/local/share/whisper-cpp",
    ]
    for d in dirs:
        for stem in stems:
            p = os.path.join(d, stem)
            if os.path.isfile(p):
                return p
    for d in dirs:
        hits = sorted(_glob.glob(os.path.join(d, "ggml-*.bin")))
        if hits:
            return hits[0]
    return None


def _clean_transcript(text: str) -> str:
    """Tame Whisper's degenerate output on non-speech audio. On silence or ambient noise
    Whisper loops a single phrase hundreds of times (e.g. "(water splashing)" x300) or emits
    only bracketed non-lexical markers. Collapse consecutive repeats and, if nothing lexical
    remains, return "" so the message is reported as no-speech rather than a wall of noise."""
    t = (text or "").strip()
    if not t:
        return t
    chunks = [c.strip() for c in re.split(r"(?<=[.!?])\s+|\n+|(?<=\))\s+(?=\()", t) if c.strip()]
    collapsed: list[str] = []
    for c in chunks:
        if not collapsed or collapsed[-1] != c:
            collapsed.append(c)
    # A chunk that is entirely a bracketed/parenthesised marker is non-lexical.
    speechish = [c for c in collapsed if not re.fullmatch(r"[\(\[][^\)\]]*[\)\]]", c)]
    if not speechish:
        return ""
    return " ".join(collapsed).strip()


_DEFAULT_VOICEMAIL_PROMPT = (
    "Voicemail for {{rtm.name}}, a residential leasehold apartment building. Callers usually identify "
    "themselves by flat number, for example 'flat five calling', and may mention service charges, the "
    "managing agent, arrears, a demand, or a leaseholder's name."
)


def _voicemail_prompt() -> str:
    """Domain/context prompt fed to the transcriber so it resolves ambiguous telephony audio correctly,
    e.g. hears 'flat five' not 'fuck five'. Callers to this line identify by flat number and use RTM
    vocabulary; priming the model with that context measurably improves accuracy on short/muffled clips.
    Override with MONGOOSE_VOICEMAIL_PROMPT."""
    return (os.getenv("MONGOOSE_VOICEMAIL_PROMPT", "").strip() or _profile.render(_DEFAULT_VOICEMAIL_PROMPT))


def _auto_transcribe_cmd() -> str | None:
    """Detect the best available local transcription command without requiring env var config.
    Checks for Whisper CLI variants in order of quality. Returns a shell command template
    with {audio}, {model}, {outdir} placeholders, or None if nothing usable is installed.

    Two CLI families are handled distinctly because their argument syntax differs:
      - openai-whisper / whisper-ctranslate2: `--model <name> --output_dir <dir>`, writes <name>.txt
      - whisper.cpp (brew installs it as `whisper-cli`; older name `whisper-cpp`/`main`):
        `-f <audio> -m <model.bin> -otxt`, and requires 16 kHz mono PCM, so we resample with ffmpeg.
    VoIP provider voicemail is 8 kHz, which whisper.cpp rejects outright, hence the mandatory resample."""
    ffmpeg = _which("ffmpeg")
    # openai-whisper family: takes --model <name> and writes <outdir>/*.txt directly.
    for binary in ("whisper-ctranslate2", "whisper"):
        cmd = _which(binary)
        if cmd:
            if not ffmpeg:
                return "__WHISPER_NO_FFMPEG__"
            pr = shlex.quote(_voicemail_prompt())
            return (f"{cmd} {{audio}} --model {{model}} --language en --initial_prompt {pr} "
                    f"--output_format txt --output_dir {{outdir}}")
    # whisper.cpp family: needs a ggml model file path plus a 16 kHz mono resample.
    for binary in ("whisper-cli", "whisper-cpp", "main"):
        cmd = _which(binary)
        if cmd:
            if not ffmpeg:
                return "__WHISPER_NO_FFMPEG__"
            try:
                model_name = getattr(get_settings(), "stt_model", "base.en") or "base.en"
            except Exception:
                model_name = "base.en"
            model_path = _resolve_ggml_model(model_name)
            if not model_path:
                return "__WHISPER_CPP_NO_MODEL__"
            mp = shlex.quote(model_path)
            pr = shlex.quote(_voicemail_prompt())
            return (
                f"{ffmpeg} -y -loglevel error -i {{audio}} -ar 16000 -ac 1 -f wav {{outdir}}/_16k.wav "
                f"&& {cmd} -f {{outdir}}/_16k.wav -m {mp} -otxt -nt --prompt {pr} -of {{outdir}}/transcript"
            )
    return None


def _transcribe_audio(payload_b64: str | None, filename: str | None) -> tuple[str | None, str, str | None, str | None]:
    """Transcribe voicemail audio locally. Uses Whisper if installed, with no manual configuration
    required: the binary is auto-detected. Set MONGOOSE_VOICEMAIL_TRANSCRIBE_CMD to override
    with a custom command, or MONGOOSE_STT_MODEL to choose a Whisper model (default: base.en).
    """
    if not payload_b64:
        return None, "pending_no_audio", None, None
    # Prefer an explicit override; fall back to auto-detection.
    cmd_template = os.getenv("MONGOOSE_VOICEMAIL_TRANSCRIBE_CMD", "").strip() or _auto_transcribe_cmd()
    if not cmd_template:
        return None, "pending_no_transcriber", None, None
    if cmd_template == "__WHISPER_NO_FFMPEG__":
        return None, "pending_no_ffmpeg", None, "Whisper is installed but ffmpeg is not. Run: brew install ffmpeg"
    if cmd_template == "__WHISPER_CPP_NO_MODEL__":
        return None, "pending_no_model", None, (
            "whisper.cpp is installed but no ggml model was found. Put ggml-base.en.bin in "
            "~/.mongoose/data/models (download from huggingface.co/ggerganov/whisper.cpp)."
        )
    try:
        audio = base64.b64decode(payload_b64)
    except Exception as exc:
        return None, "error", None, f"could not decode audio payload: {exc}"
    suffix = Path(filename or "voicemail.wav").suffix or ".wav"
    settings = get_settings()
    timeout = int(os.getenv("MONGOOSE_VOICEMAIL_TRANSCRIBE_TIMEOUT_S", "90") or "90")
    with tempfile.TemporaryDirectory(prefix="mongoose-vm-") as td:
        outdir = Path(td)
        audio_path = outdir / ("voicemail" + suffix)
        audio_path.write_bytes(audio)
        quoted = {
            "audio": shlex.quote(str(audio_path)),
            "outdir": shlex.quote(str(outdir)),
            "model": shlex.quote(getattr(settings, "stt_model", "base.en") or "base.en"),
        }
        cmd = cmd_template.format(**quoted)
        if "{audio}" not in cmd_template:
            cmd = f"{cmd} {quoted['audio']}"
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, "error", "local_command", f"transcription timed out after {timeout}s"
        except Exception as exc:
            return None, "error", "local_command", f"transcription failed: {exc}"
        transcript = (proc.stdout or "").strip()
        if not transcript:
            txts = sorted(outdir.glob("*.txt"))
            for txt in txts:
                try:
                    candidate = txt.read_text(encoding="utf-8", errors="replace").strip()
                except Exception:
                    candidate = ""
                if candidate:
                    transcript = candidate
                    break
        if proc.returncode != 0 and not transcript:
            err = (proc.stderr or "").strip()[:500]
            return None, "error", "local_command", err or f"transcriber exited {proc.returncode}"
        transcript = _clean_transcript(transcript)
        if transcript:
            return transcript, "done", "local_command", None
        return None, "pending_empty", "local_command", None


def _select_existing(con, *, identity_key: str, dedupe_key: str | None = None):
    row = None
    if identity_key:
        row = con.execute(
            "SELECT * FROM voicemail_messages WHERE identity_key = ? ORDER BY id LIMIT 1",
            (identity_key,),
        ).fetchone()
    if row is None and dedupe_key:
        row = con.execute(
            "SELECT * FROM voicemail_messages WHERE dedupe_key = ? ORDER BY id LIMIT 1",
            (dedupe_key,),
        ).fetchone()
    return row


def _choose(old: Any, new: Any, *, prefer_new: bool = False) -> Any:
    if prefer_new:
        return new if new not in (None, "", []) else old
    return old if old not in (None, "", []) else new


def _upsert_voicemail(*, identity_key: str, canonical_uid: str | None, dedupe_key: str, ref: dict[str, Any],
                      source: str, source_message_id: str | None, provider: str, mailbox: str | None,
                      mailbox_label: str | None, account_number: str | None, caller_number: str | None,
                      subject: str, notice_text: str | None, voicemail_at: str | None, email_date: str | None,
                      audio_attachment_id: int | None, audio_filename: str | None, audio_mime_type: str | None,
                      audio_size_bytes: int | None, remote_audio_path: str | None, manage_url: str | None,
                      delete_url: str | None, new_message_count: int | None, old_message_count: int | None,
                      transcript: str | None, transcript_status: str, transcript_source: str | None,
                      privacy: str, raw: dict[str, Any]) -> dict[str, Any]:
    now = utc_now_iso()
    with connect() as con:
        existing = _select_existing(con, identity_key=identity_key, dedupe_key=dedupe_key)
        if existing:
            rowid = existing["id"]
            _delete_fts(con, existing)
            merged_refs = _merge_refs(existing["source_refs"] if "source_refs" in existing.keys() else None, ref)
            merged_source = _source_label(merged_refs, source)
            old_transcript = existing["transcript"] or ""
            old_tsrc = existing["transcript_source"] or ""
            # Source quality: HA staging is OpenAI gpt-4o transcription, materially better than the
            # local base.en Whisper (local_command), so it outranks it. manual (the owner typed it) wins all.
            _quality = {"manual": 4, "ha_staging": 3, "local_command": 2, "existing": 1, None: 0, "": 0}
            new_quality = _quality.get(transcript_source, 1) if transcript else 0
            old_quality = _quality.get(old_tsrc, 1) if old_transcript else 0

            # When two real transcripts differ: if their SOURCE quality clearly differs (e.g. HA gpt-4o vs
            # local base.en), prefer the better source deterministically , the local LLM judge has picked
            # the weaker transcript before, so we don't trust it across tiers. Only when the two are the
            # SAME quality tier do we fall back to content-level LLM judgement + merge. Manual always wins.
            transcript_diff_stored = None
            if (transcript_source != "manual" and old_tsrc != "manual"
                    and new_quality > 0 and old_quality > 0
                    and transcript and old_transcript
                    and transcript.strip() != old_transcript.strip()
                    and new_quality != old_quality):
                # deterministic: better source wins, no judge
                transcript_diff_stored = _transcript_diff(old_transcript, transcript)
                if new_quality > old_quality:
                    final_transcript = transcript
                    final_status = transcript_status
                    final_tsrc = transcript_source
                else:
                    final_transcript = old_transcript
                    final_status = existing["transcript_status"]
                    final_tsrc = old_tsrc
            elif (transcript_source != "manual" and old_tsrc != "manual"
                    and new_quality > 0 and old_quality > 0
                    and transcript and old_transcript
                    and transcript.strip() != old_transcript.strip()):
                judgement = judge_transcripts(
                    old_transcript, old_tsrc, transcript, transcript_source or "new"
                )
                final_transcript = judgement["chosen"]
                transcript_diff_stored = judgement.get("diff")
                if judgement["winner"] == "b":
                    final_status = transcript_status
                    final_tsrc = (transcript_source or "new") + ":ai_chosen"
                elif judgement["winner"] == "merge":
                    final_status = "done"
                    final_tsrc = f"ai_merge:{old_tsrc}+{transcript_source or 'new'}"
                else:
                    final_status = "done"
                    final_tsrc = old_tsrc + ":ai_kept"
            elif transcript_source == "manual":
                # Manual always wins immediately.
                final_transcript = transcript
                final_status = transcript_status
                final_tsrc = transcript_source
            elif new_quality > old_quality and transcript:
                final_transcript = transcript
                final_status = transcript_status
                final_tsrc = transcript_source
            else:
                final_transcript = old_transcript or (transcript or None)
                final_status = existing["transcript_status"]
                final_tsrc = existing["transcript_source"]
                if transcript and not old_transcript:
                    final_status = transcript_status
                    final_tsrc = transcript_source
                elif old_transcript and final_status != "done":
                    final_status = "done"
                    final_tsrc = existing["transcript_source"] or "existing"
            _vp = _provider_token()  # the provider's own notice is authoritative for its fields
            con.execute(
                """
                UPDATE voicemail_messages SET email_id=?, source=?, source_message_id=?, provider=?, mailbox=?, mailbox_label=?,
                    account_number=?, caller_number=?, subject=?, notice_text=?, voicemail_at=?, email_date=?,
                    audio_attachment_id=?, audio_filename=?, audio_mime_type=?, audio_size_bytes=?, remote_audio_path=?,
                    manage_url=?, delete_url=?, new_message_count=?, old_message_count=?, transcript=?, transcript_status=?,
                    transcript_source=?, privacy=?, raw_json=?, identity_key=?, canonical_uid=?, source_refs=?, indexed_at=?
                WHERE id=?
                """,
                (_choose(existing["email_id"], raw.get("email_id")), merged_source,
                 _choose(existing["source_message_id"], source_message_id),
                 _choose(existing["provider"], provider, prefer_new=(provider == _vp)),
                 _choose(existing["mailbox"], mailbox, prefer_new=(provider == _vp)),
                 _choose(existing["mailbox_label"], mailbox_label, prefer_new=(provider == _vp)),
                 _choose(existing["account_number"], account_number, prefer_new=(provider == _vp)),
                 _choose(existing["caller_number"], caller_number, prefer_new=(provider == _vp)),
                 _choose(existing["subject"], subject, prefer_new=(provider == _vp)),
                 _choose(existing["notice_text"], notice_text, prefer_new=bool(notice_text and len(notice_text) > len(existing["notice_text"] or ""))),
                 _choose(existing["voicemail_at"], voicemail_at, prefer_new=(provider == _vp)),
                 _choose(existing["email_date"], email_date, prefer_new=(provider == _vp)),
                 _choose(existing["audio_attachment_id"], audio_attachment_id), _choose(existing["audio_filename"], audio_filename),
                 _choose(existing["audio_mime_type"], audio_mime_type), _choose(existing["audio_size_bytes"], audio_size_bytes),
                 _choose(existing["remote_audio_path"] if "remote_audio_path" in existing.keys() else None, remote_audio_path),
                 _choose(existing["manage_url"], manage_url), _choose(existing["delete_url"], delete_url),
                 _choose(existing["new_message_count"], new_message_count), _choose(existing["old_message_count"], old_message_count),
                 final_transcript, final_status, final_tsrc, _choose(existing["privacy"], privacy),
                 json.dumps({"latest": raw, "merged": True, **({"transcript_diff": transcript_diff_stored} if transcript_diff_stored else {})}), identity_key,
                 _choose(existing["canonical_uid"] if "canonical_uid" in existing.keys() else None, canonical_uid),
                 merged_refs, now, rowid),
            )
            # Reload for FTS with final values.
            row = con.execute("SELECT * FROM voicemail_messages WHERE id = ?", (rowid,)).fetchone()
            _insert_fts(con, rowid, provider=row["provider"], mailbox_label=row["mailbox_label"],
                        account_number=row["account_number"], caller_number=row["caller_number"],
                        subject=row["subject"], notice_text=row["notice_text"], transcript=row["transcript"],
                        audio_filename=row["audio_filename"])
            action = "merge"
        else:
            source_refs = _merge_refs("[]", ref)
            cur = con.execute(
                """
                INSERT INTO voicemail_messages(email_id, source, source_message_id, provider, mailbox, mailbox_label,
                    account_number, caller_number, subject, notice_text, voicemail_at, email_date, audio_attachment_id,
                    audio_filename, audio_mime_type, audio_size_bytes, manage_url, delete_url, new_message_count,
                    old_message_count, transcript, transcript_status, transcript_source, privacy, raw_json, dedupe_key,
                    identity_key, canonical_uid, source_refs, remote_audio_path, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (raw.get("email_id"), source, source_message_id, provider, mailbox, mailbox_label,
                 account_number, caller_number, subject, notice_text, voicemail_at, email_date, audio_attachment_id,
                 audio_filename, audio_mime_type, audio_size_bytes, manage_url, delete_url, new_message_count,
                 old_message_count, transcript, transcript_status, transcript_source, privacy, json.dumps(raw), dedupe_key,
                 identity_key, canonical_uid, source_refs, remote_audio_path, now),
            )
            rowid = cur.lastrowid
            _insert_fts(con, rowid, provider=provider, mailbox_label=mailbox_label, account_number=account_number,
                        caller_number=caller_number, subject=subject, notice_text=notice_text,
                        transcript=transcript, audio_filename=audio_filename)
            action = "insert"
        # Reload the row within the same connection (while it's still open) to return the
        # ACTUAL stored transcript_status and transcript_source rather than the incoming values.
        stored = con.execute(
            "SELECT transcript_status, transcript_source FROM voicemail_messages WHERE id=?", (int(rowid),)
        ).fetchone()
    final_ts = stored["transcript_status"] if stored else transcript_status
    final_tsrc = stored["transcript_source"] if stored else None
    return {"ok": True, "id": int(rowid), "identity_key": identity_key, "canonical_uid": canonical_uid,
            "transcript_status": final_ts, "transcript_source": final_tsrc, "source": source, "action": action}



def _already_processed_imap_audio(identity_key: str, dedupe_key: str, audio_filename: str | None) -> bool:
    """Return True when this voicemail already has an IMAP/audio observation and a completed
    transcript. Scheduled IMAP overlap re-fetches the same voicemail notices many times; without this
    guard a working Whisper install re-transcribes the same WAV on every overlap, making day-two mail
    sync feel slow and hot even though nothing changed.
    """
    if not audio_filename:
        return False
    try:
        with connect() as con:
            existing = _select_existing(con, identity_key=identity_key, dedupe_key=dedupe_key)
            if not existing:
                return False
            if (existing["transcript_status"] or "") != "done" or not (existing["transcript"] or ""):
                return False
            refs = _loads_refs(existing["source_refs"] if "source_refs" in existing.keys() else None)
            for ref in refs:
                if ref.get("kind") == "imap" and str(ref.get("audio_filename") or "") == str(audio_filename):
                    return True
    except Exception:
        return False
    return False

def index_voicemail_from_email(*, msg: Message, body: str, source: str, source_message_id: str | None,
                               email_id: int | None, mailbox: str | None,
                               attachments: list[dict[str, Any]], privacy: str = "medium") -> dict[str, Any]:
    """Detect and index a voicemail notice plus its audio attachment.

    `attachments` should be the indexed attachment dicts returned by attachments.index_attachment, enriched
    with filename/mime/size/payload_b64.
    """
    detection = detect_voicemail_message(msg, body)
    if not detection:
        return {"ok": False, "reason": "not_voicemail"}
    audio = _pick_audio_attachment(attachments)
    # Audio is desirable but not required: the Pi/HA staging path may supply the audio later, and the
    # voicemail record is still useful for caller ID, timestamp and transcript without it. A missing
    # attachment is common when IMAP fetches arrive before the full attachment is downloaded.
    audio_filename = (audio.get("filename") if audio else None) or None
    uid = _extract_uid_from_text(detection.provider_uid, audio_filename, detection.delete_url)
    identity_key, canonical_uid = canonical_identity_key(
        provider_uid=uid,
        account_number=detection.account_number,
        caller_number=detection.caller_number,
        voicemail_at=detection.voicemail_at or msg.get("Date"),
        audio_filename=audio_filename,
        source_message_id=source_message_id,
    )
    dedupe_key = _source_specific_dedupe_key(source, source_message_id, audio_filename, detection.caller_number, detection.voicemail_at)
    if _already_processed_imap_audio(identity_key, dedupe_key, audio_filename):
        transcript, transcript_status, transcript_source, transcript_error = None, "done", None, None
    else:
        transcript, transcript_status, transcript_source, transcript_error = _transcribe_audio(
            audio.get("payload_b64") if audio else None, audio_filename)
    raw = {
        "email_id": email_id,
        "provider": detection.provider,
        "transcript_error": transcript_error,
        "audio_attachment": ({k: audio.get(k) for k in ("id", "filename", "mime_type", "size_bytes")}
                             if audio else None),
    }
    ref = {
        "kind": "imap",
        "source": source,
        "source_message_id": source_message_id,
        "email_id": email_id,
        "audio_attachment_id": audio.get("id") if audio else None,
        "audio_filename": audio_filename,
        "provider_uid": canonical_uid,
        "seen_at": utc_now_iso(),
    }
    res = _upsert_voicemail(
        identity_key=identity_key, canonical_uid=canonical_uid, dedupe_key=dedupe_key, ref=ref,
        source=source, source_message_id=source_message_id, provider=detection.provider, mailbox=mailbox,
        mailbox_label=detection.mailbox_label, account_number=detection.account_number,
        caller_number=detection.caller_number, subject=msg.get("Subject") or "(no subject)",
        notice_text=detection.notice_text, voicemail_at=detection.voicemail_at, email_date=msg.get("Date"),
        audio_attachment_id=audio.get("id") if audio else None,
        audio_filename=audio_filename,
        audio_mime_type=audio.get("mime_type") if audio else None,
        audio_size_bytes=audio.get("size_bytes") if audio else None,
        remote_audio_path=None, manage_url=detection.manage_url,
        delete_url=detection.delete_url, new_message_count=detection.new_message_count,
        old_message_count=detection.old_message_count, transcript=transcript, transcript_status=transcript_status,
        transcript_source=transcript_source, privacy=privacy, raw=raw,
    )
    audit("voicemail", "index", True, privacy=privacy,
          payload={"provider": detection.provider, "caller_number": detection.caller_number,
                   "transcript_status": res.get("transcript_status"), "identity_key": identity_key,
                   "action": res.get("action")})
    return {"ok": True, "id": res["id"], "provider": detection.provider,
            "transcript_status": res.get("transcript_status"), "identity_key": identity_key,
            "action": res.get("action")}


def index_voicemail_from_protocol_staging(envelope: dict[str, Any]) -> dict[str, Any]:
    """Index a site RTM voicemail drained from the Pi v3 staging JSONL file.

    This complements IMAP/VoIP provider detection. The HA add-on has already extracted the transcript/path, so
    we store it as an observation of the same canonical voicemail identity, not as a separate duplicate.
    Audio lives on the Pi and is stored as a remote path reference unless an IMAP audio attachment later
    supplies local bytes.
    """
    msg_id = str(envelope.get("msg_id") or "").strip()
    if not msg_id:
        return {"ok": False, "reason": "missing_msg_id"}
    caller = _normalise_number(str(envelope.get("caller") or "").strip())
    transcript = str(envelope.get("transcript") or "").strip() or None
    wav_rel_path = str(envelope.get("wav_rel_path") or "").strip() or None
    uid = str(envelope.get("uid") or msg_id.replace("vm_", "")).strip()
    source = str(envelope.get("from") or _ha_source())
    tag = str(envelope.get("tag") or "").strip() or None
    # Accept both "timestamp" (old v3 envelopes) and "voicemail_at" (new canonical field) for the call
    # time, and "account_number" for the VoIP provider mailbox, so the fingerprint matches the IMAP identity.
    timestamp = (str(envelope.get("voicemail_at") or "").strip()
                 or str(envelope.get("timestamp") or "").strip() or None)
    account_number = str(envelope.get("account_number") or "").strip() or None
    provider_uid = _extract_uid_from_text(uid, msg_id, wav_rel_path)
    identity_key, canonical_uid = canonical_identity_key(
        provider_uid=provider_uid,
        account_number=account_number,
        caller_number=caller,
        voicemail_at=timestamp,
        remote_audio_path=wav_rel_path,
        source_message_id=msg_id,
    )
    provider = "home_assistant"
    rtm_name = _profile.rtm_name()
    mailbox_label = f"{rtm_name} RTM"
    subject = f"{rtm_name} voicemail from {caller or 'unknown caller'}"
    notice_text = transcript or f"Voicemail staged by Home Assistant{(' from ' + caller) if caller else ''}."
    status = "done" if transcript else "pending_ha_transcript"
    transcript_source = "ha_staging" if transcript else None
    audio_filename = Path(wav_rel_path).name if wav_rel_path else f"{uid}.wav"
    raw = {
        "source": source,
        "source_message_id": msg_id,
        "protocol_msg_id": msg_id,
        "uid": uid,
        "tag": tag,
        "wav_rel_path": wav_rel_path,
        "m_approval_status": envelope.get("m_approval_status"),
        "data_not_instructions": True,
    }
    ref = {
        "kind": "ha_protocol",
        "source": source,
        "source_message_id": msg_id,
        "uid": uid,
        "provider_uid": canonical_uid,
        "remote_audio_path": wav_rel_path,
        "tag": tag,
        "seen_at": utc_now_iso(),
    }
    dedupe_key = _source_specific_dedupe_key(source, msg_id, audio_filename, caller, timestamp)
    res = _upsert_voicemail(
        identity_key=identity_key, canonical_uid=canonical_uid, dedupe_key=dedupe_key, ref=ref,
        source=source, source_message_id=msg_id, provider=provider, mailbox="pi_staging",
        mailbox_label=mailbox_label, account_number=account_number, caller_number=caller, subject=subject,
        notice_text=notice_text, voicemail_at=timestamp, email_date=timestamp,
        audio_attachment_id=None, audio_filename=audio_filename,
        audio_mime_type="audio/x-wav" if str(audio_filename).lower().endswith(".wav") else None,
        audio_size_bytes=None, remote_audio_path=wav_rel_path, manage_url=None, delete_url=None,
        new_message_count=None, old_message_count=None, transcript=transcript, transcript_status=status,
        transcript_source=transcript_source, privacy="medium", raw=raw,
    )
    audit("voicemail", "index_protocol_staging", True, privacy="medium",
          payload={"msg_id": msg_id, "caller_number": caller, "transcript_status": status,
                   "identity_key": identity_key, "action": res.get("action")})
    return {"ok": True, "id": res["id"], "provider": provider,
            "transcript_status": res.get("transcript_status", status),
            "transcript_source": res.get("transcript_source", transcript_source),
            "source": source, "identity_key": identity_key, "action": res.get("action")}


def search_voicemails(q: str, limit: int = 20, *, include_email_fallback: bool = True) -> list[dict[str, Any]]:
    """Search detected voicemails, with phone-number normalisation and an email coverage fallback.

    Primary source is the canonical voicemail table (rich rows with audio + transcript). Users often
    type caller numbers with spaces and the indexed number is compact, so we also try an all-digits
    variant of the query (e.g. "07700 900123" also matches "07700900123"). As a coverage fallback we
    ALSO surface voicemail-like emails that were never promoted into the canonical table (a different
    provider, a forward, an unrecognised notice), clearly marked (origin='email',
    transcript_status='email_only', id=None) and deduped against emails already promoted."""
    limit = max(1, min(int(limit or 20), 200))
    # Query variants: the raw text plus a compact-digits form so a spaced phone number still matches.
    queries: list[str] = []
    raw = str(q or "").strip()
    if raw:
        queries.append(raw)
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 6 and digits not in queries:
        queries.append(digits)
    matches = [m for m in (safe_fts_query(query) for query in queries) if m is not None]
    if not matches:
        # Generic ask with no usable search term ("any voicemails?"): list the most RECENT voicemails
        # (canonical rows + voicemail-like emails) rather than returning nothing, which would read as
        # "you're all clear" without ever looking at forwarded voicemail emails.
        return _recent_voicemails(limit, include_email_fallback=include_email_fallback)

    canon_sql = """
        SELECT v.id, v.email_id, v.source, v.source_refs, v.identity_key, v.canonical_uid,
               v.provider, v.mailbox, v.mailbox_label, v.account_number, v.caller_number,
               v.subject, v.notice_text, v.voicemail_at, v.email_date, v.audio_attachment_id,
               v.audio_filename, v.audio_mime_type, v.audio_size_bytes, v.remote_audio_path,
               v.transcript, v.transcript_status, v.transcript_source, v.privacy, v.indexed_at
        FROM voicemail_fts f
        JOIN voicemail_messages v ON v.id = f.rowid
        WHERE voicemail_fts MATCH ?
        ORDER BY bm25(voicemail_fts), COALESCE(v.email_date, v.indexed_at) DESC
        LIMIT ?
        """
    seen_vid: set[int] = set()
    out: list[dict[str, Any]] = []
    with connect() as con:
        for match in matches:
            for row in con.execute(canon_sql, (match, limit)).fetchall():
                vid = int(row["id"])
                if vid in seen_vid:
                    continue
                seen_vid.add(vid)
                out.append(dict(row))
                if len(out) >= limit:
                    return out[:limit]
        if include_email_fallback and len(out) < limit:
            out.extend(_voicemail_like_email_hits(con, matches, limit - len(out)))
    return out[:limit]


def _promoted_email_ids(con) -> set:
    """email_ids already promoted into the canonical voicemail table (so we never double-count)."""
    return {
        r[0] for r in con.execute(
            "SELECT DISTINCT email_id FROM voicemail_messages WHERE email_id IS NOT NULL"
        ).fetchall()
    }


def _email_row_to_voicemail(em: dict[str, Any]) -> dict[str, Any]:
    """Shape a voicemail-like email row into the standard voicemail result dict (origin='email')."""
    when = em.get("received_at") or em.get("sent_at")
    return {
        "id": None,
        "email_id": em["id"],
        "source": "email",
        "source_refs": "[]",
        "identity_key": f"email:{em['id']}",
        "canonical_uid": None,
        "provider": "email",
        "mailbox": em.get("mailbox"),
        "mailbox_label": None,
        "account_number": None,
        "caller_number": em.get("sender"),
        "subject": em.get("subject"),
        "notice_text": em.get("snippet"),
        "voicemail_at": when,
        "email_date": when,
        "audio_attachment_id": None,
        "audio_filename": None,
        "audio_mime_type": None,
        "audio_size_bytes": None,
        "remote_audio_path": None,
        "transcript": None,
        "transcript_status": "email_only",
        "transcript_source": None,
        "privacy": em.get("privacy") or "medium",
        "indexed_at": None,
        "origin": "email",
        "match_kind": "voicemail_like_email",
    }


def _voicemail_like_email_hits(con, matches: list[str], limit: int) -> list[dict[str, Any]]:
    """Emails matching any query variant that look like voicemail notices but are not in the canonical
    table. `con` is an open connection; `matches` are already-sanitised FTS query strings."""
    if limit <= 0:
        return []
    promoted = _promoted_email_ids(con)
    email_sql = """
        SELECT m.id, m.source, m.mailbox, m.sender, m.subject,
               substr(m.body, 1, 800) AS snippet, m.sent_at, m.received_at, m.privacy
        FROM email_fts f
        JOIN email_messages m ON m.id = f.rowid
        WHERE email_fts MATCH ?
        ORDER BY bm25(email_fts), m.indexed_at DESC
        LIMIT ?
        """
    seen_email: set[int] = set()
    out: list[dict[str, Any]] = []
    sender_re = _vm_email_sender_re()
    for match in matches:
        # Pull a few extra candidates so filtering to voicemail-like ones still fills the limit.
        rows = con.execute(email_sql, (match, max(limit * 4, 20))).fetchall()
        for r in rows:
            em = dict(r)
            if em["id"] in promoted or em["id"] in seen_email:
                continue
            if not _email_looks_like_voicemail(em.get("sender"), em.get("subject"), em.get("snippet"),
                                               sender_re):
                continue
            seen_email.add(em["id"])
            out.append(_email_row_to_voicemail(em))
            if len(out) >= limit:
                return out
    return out


def _recent_voicemail_like_emails(con, limit: int, *, exclude_ids: set | None = None) -> list[dict[str, Any]]:
    """The most RECENT emails that look like voicemail notices, WITHOUT requiring a search term. This is
    what makes a generic 'any voicemails?' actually surface forwarded voicemail emails that simply do not
    contain the literal word the user typed."""
    if limit <= 0:
        return []
    promoted = _promoted_email_ids(con)
    skip = set(exclude_ids or set())
    rows = con.execute(
        """
        SELECT m.id, m.source, m.mailbox, m.sender, m.subject,
               substr(m.body, 1, 800) AS snippet, m.sent_at, m.received_at, m.privacy
        FROM email_messages m
        ORDER BY COALESCE(m.received_at, m.sent_at, m.indexed_at) DESC
        LIMIT ?
        """,
        (max(limit * 25, 400),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    sender_re = _vm_email_sender_re()
    for r in rows:
        em = dict(r)
        if em["id"] in promoted or em["id"] in skip:
            continue
        if not _email_looks_like_voicemail(em.get("sender"), em.get("subject"), em.get("snippet"), sender_re):
            continue
        out.append(_email_row_to_voicemail(em))
        if len(out) >= limit:
            break
    return out


def _recent_voicemails(limit: int, *, include_email_fallback: bool = True) -> list[dict[str, Any]]:
    """Most recent voicemails overall: canonical rows first, then recent voicemail-like emails to fill.
    Used when the ask is generic ('any voicemails?') so there is no search term to match on."""
    out = list_voicemails(limit)
    if include_email_fallback and len(out) < limit:
        seen = {r.get("email_id") for r in out if r.get("email_id")}
        with connect() as con:
            out.extend(_recent_voicemail_like_emails(con, limit - len(out), exclude_ids=seen))
    return out[:limit]


def list_voicemails(limit: int = 20, *, transcript_status: str | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 20), 200))
    sql = """
        SELECT id, email_id, source, source_refs, identity_key, canonical_uid, provider, mailbox,
               mailbox_label, account_number, caller_number, subject, voicemail_at, email_date,
               audio_attachment_id, audio_filename, audio_mime_type, audio_size_bytes, remote_audio_path,
               transcript, transcript_status, transcript_source, privacy, indexed_at
        FROM voicemail_messages
    """
    params: list[Any] = []
    if transcript_status:
        sql += " WHERE transcript_status = ?"
        params.append(transcript_status)
    # NOTE: email_date is an RFC-2822 STRING ("Fri, 17 Jul ..."), so a SQL ORDER BY on it sorts by
    # weekday name alphabetically and scrambles chronology (the newest voicemail sinks down the list).
    # Fetch all, then sort in Python by a REAL parsed timestamp so newest is always first.
    with connect() as con:
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]

    def _sort_key(r: dict[str, Any]) -> float:
        # Real instant (epoch seconds) so newest is always first, regardless of string format.
        val = r.get("email_date")
        if val:
            try:
                return parsedate_to_datetime(val).timestamp()
            except Exception:
                pass
        ix = r.get("indexed_at")
        if ix:
            try:
                return datetime.fromisoformat(ix).timestamp()
            except Exception:
                pass
        return 0.0

    rows.sort(key=_sort_key, reverse=True)
    return rows[:limit]


def get_voicemail(voicemail_id: int) -> dict[str, Any] | None:
    with connect() as con:
        row = con.execute("SELECT * FROM voicemail_messages WHERE id = ?", (int(voicemail_id),)).fetchone()
    return dict(row) if row else None


def set_voicemail_transcript(voicemail_id: int, transcript: str, *, source: str = "manual") -> dict[str, Any]:
    transcript = (transcript or "").strip()
    with connect() as con:
        existing = con.execute(
            "SELECT * FROM voicemail_messages WHERE id = ?",
            (int(voicemail_id),),
        ).fetchone()
        if not existing:
            return {"ok": False, "reason": "not_found"}
        _delete_fts(con, existing)
        status = "done" if transcript else "pending_manual"
        refs = _merge_refs(existing["source_refs"] if "source_refs" in existing.keys() else None,
                           {"kind": "manual_transcript", "source": source, "seen_at": utc_now_iso()})
        con.execute(
            "UPDATE voicemail_messages SET transcript=?, transcript_status=?, transcript_source=?, source_refs=?, indexed_at=? WHERE id=?",
            (transcript or None, status, source, refs, utc_now_iso(), existing["id"]),
        )
        _insert_fts(con, existing["id"], provider=existing["provider"], mailbox_label=existing["mailbox_label"],
                    account_number=existing["account_number"], caller_number=existing["caller_number"],
                    subject=existing["subject"], notice_text=existing["notice_text"],
                    transcript=transcript, audio_filename=existing["audio_filename"])
    audit("voicemail", "transcript_update", True, privacy="medium", payload={"id": int(voicemail_id), "source": source})
    return {"ok": True, "id": int(voicemail_id), "transcript_status": status}


def stage_transcript(*, transcript: str, provider_uid: str | None = None, audio_filename: str | None = None,
                     caller_number: str | None = None, account_number: str | None = None,
                     voicemail_at: str | None = None, source_message_id: str | None = None,
                     remote_audio_path: str | None = None, source: str = "ha_staging",
                     voicemail_id: int | None = None) -> dict[str, Any]:
    """Ingest a transcript produced by an EXTERNAL transcriber (the Home Assistant Example Court
    add-on) and merge it with any local Whisper transcript for the same voicemail.

    This is the long-designed 'ha_staging' source that the AI judge/merge in _upsert_voicemail
    references: when HA's transcript and Mongoose's local Whisper transcript differ, judge_transcripts
    picks the better one or merges them. Matching is by canonical identity (provider UID, else
    caller+minute, else audio fingerprint), the same key IMAP ingest uses, so the two observations
    of one voicemail collapse into a single row. If no row exists yet (HA transcribed before IMAP
    indexed the notice), a new row is created that a later IMAP observation merges into.

    HA should send `provider_uid` (the voip_provider UUID from the notice / delete URL / .wav filename)
    for the most reliable match, or failing that `caller_number` + `voicemail_at`.
    """
    text = _clean_transcript((transcript or "").strip())
    if not text:
        return {"ok": False, "reason": "empty_transcript"}
    if voicemail_id is not None:
        existing = get_voicemail(int(voicemail_id))
        if not existing:
            return {"ok": False, "reason": "not_found"}
        caller_number = caller_number or existing.get("caller_number")
        account_number = account_number or existing.get("account_number")
        voicemail_at = voicemail_at or existing.get("voicemail_at")
        audio_filename = audio_filename or existing.get("audio_filename")
        source_message_id = source_message_id or existing.get("source_message_id")
    identity_key, canonical_uid = canonical_identity_key(
        provider_uid=provider_uid, account_number=account_number, caller_number=caller_number,
        voicemail_at=voicemail_at, audio_filename=audio_filename, remote_audio_path=remote_audio_path,
        source_message_id=source_message_id)
    dedupe_key = _source_specific_dedupe_key(source, source_message_id, audio_filename, caller_number, voicemail_at)
    ref = {"kind": "ha_staging", "source": source, "seen_at": utc_now_iso(),
           "provider_uid": provider_uid, "audio_filename": audio_filename}
    # subject/notice_text are NOT NULL columns (IMAP always supplies them). For a staged-only row
    # that arrives before the IMAP notice, synthesise placeholders; _upsert_voicemail's _choose keeps
    # the richer IMAP values when that observation later merges in.
    staged_subject = f"Voicemail from {caller_number}" if caller_number else "Voicemail"
    res = _upsert_voicemail(
        identity_key=identity_key, canonical_uid=canonical_uid, dedupe_key=dedupe_key, ref=ref,
        source=source, source_message_id=source_message_id, provider=_provider_token(), mailbox=None,
        mailbox_label=None, account_number=account_number, caller_number=caller_number,
        subject=staged_subject, notice_text="", voicemail_at=voicemail_at, email_date=None,
        audio_attachment_id=None, audio_filename=audio_filename, audio_mime_type=None,
        audio_size_bytes=None, remote_audio_path=remote_audio_path, manage_url=None, delete_url=None,
        new_message_count=None, old_message_count=None, transcript=text, transcript_status="done",
        transcript_source=source, privacy="medium", raw={"source": source, "staged": True})
    audit("voicemail", "stage_transcript", True, privacy="medium",
          payload={"id": res.get("id"), "source": source, "action": res.get("action"),
                   "final_source": res.get("transcript_source")})
    return res


def reconcile_voicemail_identities(limit: int = 500) -> dict[str, Any]:
    """Best-effort maintenance pass for older DBs that already had duplicate IMAP and HA rows.

    New writes merge at upsert time. This pass only collapses existing rows that share a non-empty
    identity_key, preserving transcripts, IMAP audio attachment ids, remote audio paths and source refs.
    """
    merged = 0
    _vp = _provider_token()
    with connect() as con:
        groups = con.execute(
            """
            SELECT identity_key, GROUP_CONCAT(id) ids, COUNT(*) c
            FROM voicemail_messages
            WHERE identity_key IS NOT NULL AND identity_key != ''
            GROUP BY identity_key HAVING c > 1
            LIMIT ?
            """,
            (max(1, min(int(limit or 500), 5000)),),
        ).fetchall()
        for g in groups:
            ids = [int(x) for x in str(g["ids"]).split(",") if x]
            if len(ids) < 2:
                continue
            rows = [con.execute("SELECT * FROM voicemail_messages WHERE id=?", (i,)).fetchone() for i in ids]
            rows = [r for r in rows if r]
            if not rows:
                continue
            keep = rows[0]
            for r in rows[1:]:
                _delete_fts(con, keep)
                _delete_fts(con, r)
                refs = _merge_refs(keep["source_refs"], {"kind": "merged_duplicate", "source_id": r["id"], "seen_at": utc_now_iso()})
                for old in _loads_refs(r["source_refs"]):
                    refs = _merge_refs(refs, old)
                transcript = keep["transcript"] or r["transcript"]
                status = "done" if transcript else (keep["transcript_status"] or r["transcript_status"])
                con.execute(
                    """
                    UPDATE voicemail_messages SET email_id=?, source=?, provider=?, mailbox=?, mailbox_label=?,
                        account_number=?, caller_number=?, subject=?, notice_text=?, voicemail_at=?, email_date=?,
                        audio_attachment_id=?, audio_filename=?, audio_mime_type=?, audio_size_bytes=?, remote_audio_path=?,
                        manage_url=?, delete_url=?, new_message_count=?, old_message_count=?, transcript=?,
                        transcript_status=?, transcript_source=?, source_refs=?, indexed_at=?
                    WHERE id=?
                    """,
                    (_choose(keep["email_id"], r["email_id"]), _source_label(refs, keep["source"]),
                     _choose(keep["provider"], r["provider"], prefer_new=(r["provider"] == _vp)),
                     _choose(keep["mailbox"], r["mailbox"]), _choose(keep["mailbox_label"], r["mailbox_label"]),
                     _choose(keep["account_number"], r["account_number"]), _choose(keep["caller_number"], r["caller_number"]),
                     _choose(keep["subject"], r["subject"], prefer_new=(r["provider"] == _vp)),
                     _choose(keep["notice_text"], r["notice_text"]), _choose(keep["voicemail_at"], r["voicemail_at"]),
                     _choose(keep["email_date"], r["email_date"]), _choose(keep["audio_attachment_id"], r["audio_attachment_id"]),
                     _choose(keep["audio_filename"], r["audio_filename"]), _choose(keep["audio_mime_type"], r["audio_mime_type"]),
                     _choose(keep["audio_size_bytes"], r["audio_size_bytes"]), _choose(keep["remote_audio_path"], r["remote_audio_path"]),
                     _choose(keep["manage_url"], r["manage_url"]), _choose(keep["delete_url"], r["delete_url"]),
                     _choose(keep["new_message_count"], r["new_message_count"]), _choose(keep["old_message_count"], r["old_message_count"]),
                     transcript, status, _choose(keep["transcript_source"], r["transcript_source"]), refs,
                     utc_now_iso(), keep["id"]),
                )
                con.execute("DELETE FROM voicemail_messages WHERE id=?", (r["id"],))
                keep = con.execute("SELECT * FROM voicemail_messages WHERE id=?", (keep["id"],)).fetchone()
                _insert_fts(con, keep["id"], provider=keep["provider"], mailbox_label=keep["mailbox_label"],
                            account_number=keep["account_number"], caller_number=keep["caller_number"],
                            subject=keep["subject"], notice_text=keep["notice_text"],
                            transcript=keep["transcript"], audio_filename=keep["audio_filename"])
                merged += 1
    audit("voicemail", "reconcile_identities", True, privacy="medium", payload={"merged": merged})
    return {"ok": True, "merged": merged}


def voicemail_stats() -> dict[str, Any]:
    with connect() as con:
        total = con.execute("SELECT COUNT(*) FROM voicemail_messages").fetchone()[0]
        done = con.execute("SELECT COUNT(*) FROM voicemail_messages WHERE transcript_status = 'done'").fetchone()[0]
        pending = con.execute("SELECT COUNT(*) FROM voicemail_messages WHERE transcript_status != 'done'").fetchone()[0]
        latest = con.execute("SELECT MAX(COALESCE(email_date, indexed_at)) FROM voicemail_messages").fetchone()[0]
        identities = con.execute("SELECT COUNT(DISTINCT identity_key) FROM voicemail_messages WHERE identity_key IS NOT NULL").fetchone()[0]
    return {"total": total, "canonical_identities": identities, "transcribed": done,
            "pending_transcript": pending, "latest": latest}


# --- AI transcript quality judgement + diff --------------------------------------------------------

def _transcript_diff(a: str, b: str) -> str:
    """Unified diff between two transcripts as a compact string. Used so the stored record shows
    exactly what changed between the HA and Whisper versions, or between any two sources."""
    import difflib
    a_lines = a.splitlines(keepends=True)
    b_lines = b.splitlines(keepends=True)
    diff = list(difflib.unified_diff(a_lines, b_lines, fromfile="transcript_a", tofile="transcript_b", lineterm=""))
    return "\n".join(diff) if diff else "(identical)"


async def judge_transcripts_async(transcript_a: str, source_a: str,
                                   transcript_b: str, source_b: str) -> dict[str, Any]:
    """Use the local LLM to decide which of two voicemail transcripts is better quality,
    or produce a merged version when both have unique information. Returns:
      {winner: 'a'|'b'|'merge', chosen: str, reason: str, merged: str|None, diff: str}
    Never raises: on failure returns winner='a' (keep existing) with the error noted."""
    import json as _json
    prompt = (
        "You are comparing two automatic transcriptions of the same voicemail. "
        "Decide which is higher quality, or produce a merged version if each has unique information.\n\n"
        f"Transcript A (source: {source_a}):\n{transcript_a}\n\n"
        f"Transcript B (source: {source_b}):\n{transcript_b}\n\n"
        "Respond with ONLY a JSON object, no prose:\n"
        "{\"winner\": \"a\" | \"b\" | \"merge\", "
        "\"chosen\": \"<the best single transcript>\", "
        "\"reason\": \"<one sentence: why this is better or what was merged>\", "
        "\"merged\": \"<only if winner=merge: the combined transcript, else null>\"}\n\n"
        "Quality signals: completeness, correct proper nouns, natural punctuation, coherence. "
        "Prefer the transcript that captures more of what was said accurately. "
        "If both are equally good, prefer the one with better punctuation. "
        "If one clearly has an error (repeated words, garbled text, missing content) the other lacks, pick the other."
    )
    diff = _transcript_diff(transcript_a, transcript_b)
    try:
        from ..llm import ask_ollama
        raw = await ask_ollama(prompt, role="chat")
        # strip markdown fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        data = _json.loads(raw)
        winner = data.get("winner", "a")
        chosen = (data.get("merged") or data.get("chosen") or
                  (transcript_a if winner != "b" else transcript_b))
        return {
            "winner": winner,
            "chosen": chosen.strip() if chosen else transcript_a,
            "reason": data.get("reason", ""),
            "diff": diff,
        }
    except Exception as exc:
        return {
            "winner": "a",
            "chosen": transcript_a,
            "reason": f"judgement failed ({exc}); kept existing",
            "diff": diff,
        }


def judge_transcripts(transcript_a: str, source_a: str,
                      transcript_b: str, source_b: str) -> dict[str, Any]:
    """Synchronous wrapper for use from the upsert path (which may be called from sync code)."""
    import asyncio
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            # Called from within an async context: schedule and return a placeholder;
            # the caller should use the async version directly when possible.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(asyncio.run, judge_transcripts_async(transcript_a, source_a, transcript_b, source_b))
                return fut.result(timeout=30)
        return asyncio.run(judge_transcripts_async(transcript_a, source_a, transcript_b, source_b))
    except Exception as exc:
        return {
            "winner": "a",
            "chosen": transcript_a,
            "reason": f"sync judgement failed ({exc}); kept existing",
            "diff": _transcript_diff(transcript_a, transcript_b),
        }
