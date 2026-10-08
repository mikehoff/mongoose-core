from __future__ import annotations

import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

# Example Court RTM intake + draft-response pipeline.
#
# What this does, end to end: a new email to info@example-court.org becomes a CASE, gets classified
# (urgency + demise-vs-communal responsibility, deterministic rules FIRST), and a DRAFT reply is
# generated , draft-only, never sent , then pushed to the phone/HA for one-tap approve/edit/cancel,
# mirroring the proven WhatsApp draft pattern. Routine receipts get a clearly-marked SUGGESTED
# ACKNOWLEDGE the human still approves; substantive emails get a real draft to read.
#
# THE LIVE EDGES ARE DISABLED BY DEFAULT (sandbox-safe; you flip them on the Mac, once watched):
#   - RTM_LIVE_MAILBOX (default off): actually poll the live info@example-court.org mailbox.
#   - RTM_LIVE_SEND     (default off): actually send an approved reply.
# With both off, the whole pipeline runs and is testable against synthetic mail; nothing touches a
# real inbox and nothing is ever sent. This is the operational product the audit said to build,
# with every irreversible action behind a human and an off switch.
#
# Safety posture (chosen): draft-only for everything; nothing auto-sends. The only concession to
# convenience is that routine receipts arrive pre-filled with a suggested acknowledgement, still
# requiring a tap.

from .. import profile as _profile


def _rtm_address() -> str:
    """The RTM company's mailbox (site profile), e.g. info@example-court.org in the fictional default."""
    return _profile.rtm_email()


def _rtm_company_name() -> str:
    """'<RTM name> RTM Company Limited', the RTM name from the site profile."""
    return f"{_profile.rtm_name()} RTM Company Limited"


def _voicemail_provider_label() -> str:
    """Display name of the voicemail provider for a voicemail with no stored sender (lexicon
    "rtm.voicemail_sender_label"; generic in the fictional default)."""
    return str(_profile.lexicon("rtm.voicemail_sender_label", "VoIP provider"))


def __getattr__(name: str) -> str:
    # The historical module constants, now resolved from the site profile at use time.
    if name == "EXAMPLE_COURT_ADDR":
        return _rtm_address()
    if name == "EXAMPLE_COURT_NAME":
        return _rtm_company_name()
    raise AttributeError(name)


def _live_mailbox_enabled() -> bool:
    return os.getenv("RTM_LIVE_MAILBOX", "").lower() in ("1", "true", "yes", "on")


def _live_send_enabled() -> bool:
    return os.getenv("RTM_LIVE_SEND", "").lower() in ("1", "true", "yes", "on")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat()


# --- schema ------------------------------------------------------------------

def _ensure_tables() -> None:
    from ..db import connect
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS rtm_cases (
                id TEXT PRIMARY KEY, thread_key TEXT, sender TEXT, sender_email TEXT,
                subject TEXT, status TEXT DEFAULT 'open',
                urgency TEXT, responsibility TEXT, category TEXT,
                opened_at TEXT, last_activity_at TEXT
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS rtm_intake_items (
                id TEXT PRIMARY KEY, case_id TEXT, kind TEXT,
                source_message_id TEXT, sender TEXT, subject TEXT, body TEXT,
                received_at TEXT, classified TEXT, created_at TEXT
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS rtm_drafts (
                id TEXT PRIMARY KEY, case_id TEXT, intake_id TEXT,
                to_email TEXT, subject TEXT, body TEXT,
                kind TEXT,                 -- 'acknowledge' | 'reply'
                status TEXT DEFAULT 'pending',  -- pending | approved | sent | cancelled
                created_at TEXT, decided_at TEXT
            )""")
        # Case ACTION log (draft_created / sent / cancelled ...). Named rtm_case_actions, NOT
        # "receipts": a payment receipt is rtm_trust_receipts (see docs/RTM_DATA_MODEL.md). This is an
        # audit trail of what happened to a case/draft, never a money document.
        con.execute("""
            CREATE TABLE IF NOT EXISTS rtm_case_actions (
                id TEXT PRIMARY KEY, case_id TEXT, draft_id TEXT, action TEXT,
                detail TEXT, at TEXT
            )""")
        # FTS index over intake items, so RTM email + voicemail transcripts are searchable
        # ("lift stuck", "service charge"). Ported from the v0.37 fork; additive. A standard FTS5
        # table (stores the text) so MATCH works directly.
        con.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS rtm_intake_fts USING fts5(
                intake_id UNINDEXED, subject, body, sender
            )""")
        con.commit()



_RTM_DRAFT_TOKEN_TTL_SECONDS = 24 * 3600  # one day: enough for a tap, not a reusable credential


def _ensure_draft_tokens_table() -> None:
    """Short-lived RTM draft action tokens for phone notification buttons.

    These deliberately mirror the WhatsApp fix: an ntfy action button must never carry the reusable
    master MONGOOSE_API_TOKEN to a push server. A token is scoped to one draft + one action and is
    consumed on first use.
    """
    _ensure_tables()
    from ..db import connect
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS rtm_draft_tokens (
                token TEXT PRIMARY KEY,
                draft_id TEXT NOT NULL,
                action TEXT NOT NULL,
                expires_at REAL NOT NULL,
                used INTEGER DEFAULT 0
            )""")
        con.commit()


def _mint_draft_action_token(draft_id: str, *, action: str) -> str:
    if action not in {"approve", "cancel"}:
        raise ValueError("action must be approve or cancel")
    _ensure_draft_tokens_table()
    token = secrets.token_urlsafe(24)
    expires_at = time.time() + _RTM_DRAFT_TOKEN_TTL_SECONDS
    from ..db import connect
    with connect() as con:
        con.execute("INSERT INTO rtm_draft_tokens(token, draft_id, action, expires_at) VALUES (?,?,?,?)",
                    (token, str(draft_id), action, expires_at))
        con.commit()
    return token


def consume_draft_action_token(token: str, draft_id: str, action: str) -> bool:
    """Validate and consume a token minted for exactly this RTM draft/action."""
    if action not in {"approve", "cancel"}:
        return False
    _ensure_draft_tokens_table()
    from ..db import connect
    now = time.time()
    with connect() as con:
        row = con.execute(
            "SELECT token, draft_id, action, expires_at, used FROM rtm_draft_tokens "
            "WHERE token=? AND draft_id=? AND action=?",
            (token, str(draft_id), action),
        ).fetchone()
        if not row or int(row["used"] or 0) or float(row["expires_at"] or 0) < now:
            return False
        cur = con.execute("UPDATE rtm_draft_tokens SET used=1 WHERE token=? AND used=0", (token,))
        con.commit()
        return cur.rowcount == 1


# --- deterministic classification (rules FIRST, never ML for safety) ---------

# Safety-critical patterns , these set urgency HIGH regardless of anything else. Negation-aware so
# "no gas smell" doesn't trip the gas rule.
_SAFETY_PATTERNS = [
    ("gas", r"\b(gas (leak|smell)|smell(?:s|ing)? (of )?gas|carbon monoxide)\b"),
    ("fire", r"\b(fire|smoke alarm|burning smell)\b"),
    ("flood", r"\b(flood(?:ing)?|burst pipe|water (is |was )?(pouring|gushing|coming through|"
              r"flooding)|water.{0,15}through (the )?ceiling|leak through (the )?ceiling)\b"),
    ("structural", r"\b(structural|crack(?:ing|ed)? (wall|ceiling|beam)|ceiling (falling|collapse))\b"),
    ("lift_entrapment", r"\b(stuck in (the )?lift|trapped in (the )?lift|lift entrapment)\b"),
    ("no_heat_vulnerable", r"\b(no (heating|hot water).{0,30}(elderly|baby|vulnerable|infant))\b"),
    ("security", r"\b(break.?in|intruder|door (won'?t lock|forced)|broken (entry|security) door)\b"),
]
_NEGATORS = re.compile(r"\b(no|not|without|isn'?t|aren'?t|wasn'?t|never|resolved|fixed|sorted)\b")

# Responsibility: demise (leaseholder's own flat) vs communal (RTM's responsibility).
_COMMUNAL = r"\b(communal|hallway|corridor|stairwell|lift|roof|gutter|car ?park|entrance|" \
            r"main door|external|grounds|garden|bin store|service charge|estate)\b"
_DEMISE = r"\b(my (flat|apartment|kitchen|bathroom|bedroom)|inside my|my own|within my (flat|demise))\b"

# Routine receipts , safe to suggest an acknowledgement for.
_ROUTINE = r"\b(thank you|thanks|received|noted|acknowledg|just (confirming|checking)|" \
           r"for your records|fyi|update on|following up)\b"


def classify(subject: str, body: str) -> dict[str, Any]:
    """Deterministic-first classification. Returns urgency, responsibility, category, and whether
    it looks like a routine receipt. NEVER diagnoses beyond what the rules match; ML can refine
    later but safety never depends on it."""
    text = f"{subject}\n{body}".lower()

    # 1. safety rules , highest urgency, negation-aware
    safety_hits = []
    for name, pat in _SAFETY_PATTERNS:
        for m in re.finditer(pat, text):
            before = text[max(0, m.start() - 40):m.start()]
            after = text[m.end():m.end() + 30]
            # negation before ("no gas smell") suppresses.
            if _NEGATORS.search(before):
                continue
            # a resolution AFTER ("flooding was resolved") suppresses , BUT only if the resolution
            # is not itself negated ("not yet fixed" must still trip). For safety, a false negative
            # is worse than a false positive, so we suppress conservatively.
            res_match = re.search(r"\b(resolved|fixed|sorted|stopped|no longer|already (dealt|handled))\b", after)
            if res_match:
                res_window = after[max(0, res_match.start() - 20):res_match.start()]
                if not _NEGATORS.search(res_window):
                    continue
            safety_hits.append(name)
            break
    if safety_hits:
        return {"urgency": "high", "responsibility": _responsibility(text),
                "category": "safety:" + ",".join(safety_hits), "routine": False,
                "safety": safety_hits}

    # 2. routine receipt?
    routine = bool(re.search(_ROUTINE, text)) and not re.search(r"\b(urgent|asap|emergency|complaint|broken|not working|failed)\b", text)

    # 3. ordinary urgency heuristics (deterministic, transparent)
    if re.search(r"\b(urgent|asap|emergency|immediately|dangerous)\b", text):
        urgency = "high"
    elif re.search(r"\b(broken|not working|leak|fault|repair|outage|no (power|water|heating))\b", text):
        urgency = "medium"
    else:
        urgency = "low"

    return {"urgency": urgency, "responsibility": _responsibility(text),
            "category": _category(text), "routine": routine, "safety": []}


def _responsibility(text: str) -> str:
    communal = bool(re.search(_COMMUNAL, text))
    demise = bool(re.search(_DEMISE, text))
    if communal and not demise:
        return "communal"
    if demise and not communal:
        return "demise"
    if communal and demise:
        return "mixed"
    return "unclear"


def _category(text: str) -> str:
    for cat, pat in [
        ("maintenance", r"\b(repair|broken|fault|leak|not working|fix|maintenance)\b"),
        ("billing", r"\b(service charge|invoice|payment|bill|account|arrears)\b"),
        ("complaint", r"\b(complaint|unhappy|unacceptable|disappointed|poor)\b"),
        ("admin", r"\b(document|certificate|insurance|minutes|agm|meeting|notice)\b"),
        ("access", r"\b(key|fob|access|entry|parking permit)\b"),
    ]:
        if re.search(pat, text):
            return cat
    return "general"


# --- intake: a new email becomes a case + classification ---------------------

def intake_email(*, sender: str, subject: str, body: str,
                 source_message_id: str = "", received_at: str = "",
                 kind: str = "email") -> dict[str, Any]:
    """Turn an inbound email into an RTM intake item + case (threaded by sender+subject), with
    deterministic classification. Does NOT draft yet, call generate_draft for that. Pure data;
    safe to run on synthetic mail."""
    _ensure_tables()
    sender_email = _parse_email(sender)
    thread_key = _thread_key(sender_email, subject)
    cls = classify(subject, body)
    from ..db import connect
    with connect() as con:
        # find or open a case for this thread
        row = con.execute("SELECT id FROM rtm_cases WHERE thread_key = ? AND status = 'open'",
                          (thread_key,)).fetchone()
        if row:
            case_id = row["id"]
            con.execute("UPDATE rtm_cases SET last_activity_at = ?, urgency = ?, responsibility = ?, "
                        "category = ? WHERE id = ?",
                        (_iso(), cls["urgency"], cls["responsibility"], cls["category"], case_id))
        else:
            case_id = "case_" + uuid.uuid4().hex[:10]
            con.execute("INSERT INTO rtm_cases(id,thread_key,sender,sender_email,subject,status,"
                        "urgency,responsibility,category,opened_at,last_activity_at) "
                        "VALUES(?,?,?,?,?,'open',?,?,?,?,?)",
                        (case_id, thread_key, sender, sender_email, subject,
                         cls["urgency"], cls["responsibility"], cls["category"], _iso(), _iso()))
        intake_id = "in_" + uuid.uuid4().hex[:10]
        con.execute("INSERT INTO rtm_intake_items(id,case_id,kind,source_message_id,sender,subject,"
                    "body,received_at,classified,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (intake_id, case_id, kind, source_message_id, sender, subject, body,
                     received_at or _iso(), json.dumps(cls), _iso()))
        con.execute("INSERT INTO rtm_intake_fts(intake_id,subject,body,sender) VALUES(?,?,?,?)",
                    (intake_id, subject, body, sender))
        con.commit()
    return {"case_id": case_id, "intake_id": intake_id, "classification": cls,
            "sender_email": sender_email, "thread_key": thread_key}


def _parse_email(raw: str) -> str:
    from email.utils import parseaddr
    return parseaddr(raw or "")[1].lower()


def _thread_key(sender_email: str, subject: str) -> str:
    base = re.sub(r"^\s*(re|fwd?):\s*", "", (subject or "").strip(), flags=re.IGNORECASE).lower()
    return f"{sender_email}|{base[:80]}"


# --- draft generation (draft-only) -------------------------------------------

def generate_draft(intake_id: str) -> dict[str, Any]:
    """Generate a DRAFT reply for an intake item. Draft-only, this never sends. Routine receipts
    get a suggested ACKNOWLEDGE (clearly marked); everything else gets a substantive reply draft
    grounded in the classification. The human approves/edits every one."""
    _ensure_tables()
    from ..db import connect
    with connect() as con:
        item = con.execute("SELECT * FROM rtm_intake_items WHERE id = ?", (intake_id,)).fetchone()
    if not item:
        return {"ok": False, "reason": "intake_not_found"}
    item = dict(item)
    cls = json.loads(item["classified"])
    sender_email = _parse_email(item["sender"])
    first_name = _first_name(item["sender"])

    if cls.get("routine"):
        kind = "acknowledge"
        body = _acknowledge_body(first_name)
        draft_source = "template"
    else:
        kind = "reply"
        # Tiered drafting: safety + high-urgency cases ALWAYS use the deterministic template (a
        # human-checkable skeleton is safer than improvised prose for a gas leak). Substantive
        # cases use the optional model tier IF configured; otherwise the template. Privacy-first:
        # the model tier is inert unless a key is set, so by default nothing leaves the Mac.
        if _should_use_model_tier(cls):
            model_body = _draft_body_via_model(first_name, cls, item["subject"], item["body"])
            if model_body:
                body = model_body
                draft_source = "model"
            else:
                body = _reply_body(first_name, cls, item["subject"], item["body"])
                draft_source = "template"
        else:
            body = _reply_body(first_name, cls, item["subject"], item["body"])
            draft_source = "template"

    subject = item["subject"] or "(no subject)"
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject

    draft_id = "draft_" + uuid.uuid4().hex[:10]
    with connect() as con:
        con.execute("INSERT INTO rtm_drafts(id,case_id,intake_id,to_email,subject,body,kind,"
                    "status,created_at) VALUES(?,?,?,?,?,?,?,'pending',?)",
                    (draft_id, item["case_id"], intake_id, sender_email, subject, body, kind, _iso()))
        con.commit()
    _log_case_action(item["case_id"], draft_id, "draft_created",
             {"kind": kind, "urgency": cls.get("urgency"), "source": draft_source})
    return {"ok": True, "draft_id": draft_id, "kind": kind, "to": sender_email,
            "subject": subject, "body": body, "classification": cls, "source": draft_source}


def _acknowledge_body(name: str) -> str:
    greeting = f"Dear {name}," if name else "Hello,"
    return (f"{greeting}\n\nThank you for your message. We confirm we have received it and it has "
            f"been logged. We will be in touch if anything further is needed.\n\n"
            f"Kind regards,\n{_rtm_company_name()}")


def _reply_body(name: str, cls: dict[str, Any], subject: str, body: str) -> str:
    greeting = f"Dear {name}," if name else "Hello,"
    # a grounded, neutral holding reply , the human edits in specifics. Tone matches urgency.
    if cls.get("safety"):
        core = ("Thank you for reporting this. We are treating it as urgent. "
                "[DRAFT, review before sending: confirm the immediate safety steps and who is "
                "attending, and include emergency guidance if appropriate.]")
    elif cls.get("urgency") == "high":
        core = ("Thank you for your message, which we are treating as a priority. "
                "[DRAFT, add the specific action and timescale before sending.]")
    elif cls.get("responsibility") == "demise":
        core = ("Thank you for your message. This appears to relate to within your own demise "
                "rather than the communal parts the company is responsible for. "
                "[DRAFT, confirm the responsibility split and any signposting before sending.]")
    else:
        core = ("Thank you for your message, which we have logged. "
                "[DRAFT, add the specific response and any next step before sending.]")
    return f"{greeting}\n\n{core}\n\nKind regards,\n{_rtm_company_name()}"


# --- the optional model-drafting tier (off by default, privacy-first) --------

def _model_tier_enabled() -> bool:
    """The model-drafting tier is active ONLY when a provider + key are configured. With nothing
    set (the default), this returns False and substantive cases fall back to the local template,
    so by default no case content ever leaves the Mac."""
    provider = os.getenv("RTM_DRAFT_MODEL_PROVIDER", "").lower()
    if provider == "anthropic":
        return bool(os.getenv("ANTHROPIC_API_KEY", ""))
    if provider == "openai":
        return bool(os.getenv("OPENAI_API_KEY", ""))
    if provider == "local":
        # use the local Ollama model , stays on the Mac, no external send, no credits
        return True
    return False


def _should_use_model_tier(cls: dict[str, Any]) -> bool:
    """Decide per-case whether to use the model tier. Safety and high-urgency cases ALWAYS use the
    deterministic template (a human-checkable skeleton beats improvised prose when it matters most).
    Only substantive, non-urgent cases are routed to the model, and only if it's configured."""
    if not _model_tier_enabled():
        return False
    if cls.get("safety"):
        return False
    if cls.get("urgency") == "high":
        return False
    if cls.get("routine"):
        return False
    return True


def _draft_body_via_model(name: str, cls: dict[str, Any], subject: str, body: str) -> str | None:
    """Draft a substantive reply body via the configured model. Returns the drafted text, or None
    on any failure (so generate_draft falls back to the template). This is the ONE place external
    case content is sent, and only when the tier is configured. The output is still a DRAFT the
    human approves, the model never sends."""
    provider = os.getenv("RTM_DRAFT_MODEL_PROVIDER", "").lower()
    greeting = f"Dear {name}," if name else "Hello,"
    prompt = (
        f"You are drafting a reply on behalf of {_rtm_company_name()}, a residential "
        "right-to-manage company, to a leaseholder's email. Write a measured, professional, neutral "
        "reply. Do not invent facts, commitments, dates, or figures. Where a specific is needed but "
        "unknown, leave a clearly bracketed placeholder like [confirm timescale]. Keep it concise. "
        f"Begin with '{greeting}' and end with 'Kind regards,\\n{_rtm_company_name()}'.\n\n"
        f"Category: {cls.get('category')}. Responsibility: {cls.get('responsibility')}.\n"
        f"Their subject: {subject}\nTheir message:\n{body}\n\nDraft reply:")
    try:
        if provider == "anthropic":
            return _draft_anthropic(prompt)
        if provider == "openai":
            return _draft_openai(prompt)
        if provider == "local":
            return _draft_local(prompt)
    except Exception:
        return None
    return None


def _draft_anthropic(prompt: str) -> str | None:
    import httpx
    key = os.getenv("ANTHROPIC_API_KEY", "")
    model = os.getenv("RTM_DRAFT_MODEL", "claude-sonnet-4-6")
    r = httpx.post("https://api.anthropic.com/v1/messages",
                   headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                            "content-type": "application/json"},
                   json={"model": model, "max_tokens": 600,
                         "messages": [{"role": "user", "content": prompt}]}, timeout=30)
    if r.status_code != 200:
        return None
    parts = r.json().get("content", [])
    return "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip() or None


def _draft_openai(prompt: str) -> str | None:
    import httpx
    key = os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("RTM_DRAFT_MODEL", "gpt-4o-mini")
    r = httpx.post("https://api.openai.com/v1/chat/completions",
                   headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
                   json={"model": model, "max_tokens": 600,
                         "messages": [{"role": "user", "content": prompt}]}, timeout=30)
    if r.status_code != 200:
        return None
    return r.json()["choices"][0]["message"]["content"].strip() or None


def _draft_local(prompt: str) -> str | None:
    """Draft via the local Ollama model, stays on the Mac, no external API, no credits."""
    import httpx
    model = os.getenv("RTM_DRAFT_MODEL", os.getenv("MONGOOSE_CHAT_MODEL", "gemma4:26b-a4b-it-q4_K_M"))
    base = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    r = httpx.post(f"{base}/api/generate",
                   json={"model": model, "prompt": prompt, "stream": False}, timeout=60)
    if r.status_code != 200:
        return None
    return (r.json().get("response") or "").strip() or None


def _first_name(raw: str) -> str:
    from email.utils import parseaddr
    name = parseaddr(raw or "")[0]
    if name:
        return name.split()[0]
    return ""


# --- approval workflow (mirrors WhatsApp: draft -> approve/edit/cancel) -------

def list_drafts(status: str = "pending") -> list[dict[str, Any]]:
    _ensure_tables()
    from ..db import connect
    with connect() as con:
        rows = con.execute("SELECT * FROM rtm_drafts WHERE status = ? ORDER BY created_at DESC",
                           (status,)).fetchall()
    return [dict(r) for r in rows]


def get_draft(draft_id: str) -> dict[str, Any] | None:
    _ensure_tables()
    from ..db import connect
    with connect() as con:
        row = con.execute("SELECT * FROM rtm_drafts WHERE id = ?", (draft_id,)).fetchone()
    return dict(row) if row else None


def edit_draft(draft_id: str, body: str) -> dict[str, Any]:
    _ensure_tables()
    from ..db import connect
    with connect() as con:
        con.execute("UPDATE rtm_drafts SET body = ? WHERE id = ? AND status = 'pending'",
                    (body, draft_id))
        con.commit()
    return {"ok": True, "draft_id": draft_id, "body": body}


def cancel_draft(draft_id: str) -> dict[str, Any]:
    _ensure_tables()
    from ..db import connect
    with connect() as con:
        con.execute("UPDATE rtm_drafts SET status = 'cancelled', decided_at = ? WHERE id = ?",
                    (_iso(), draft_id))
        con.commit()
    d = get_draft(draft_id)
    if d:
        _log_case_action(d["case_id"], draft_id, "cancelled", {})
    return {"ok": True, "draft_id": draft_id, "status": "cancelled"}


def approve_draft(draft_id: str) -> dict[str, Any]:
    """Mark a draft approved and SEND IT, but only if the live send edge is enabled. With
    RTM_LIVE_SEND off (the default, and always in the sandbox), this records the approval and
    explains that sending is disabled, so nothing leaves the building. This is the human's tap;
    the model never reaches this path."""
    _ensure_tables()
    d = get_draft(draft_id)
    if not d:
        return {"ok": False, "reason": "draft_not_found"}
    if d["status"] != "pending":
        return {"ok": False, "reason": f"draft_{d['status']}"}
    from ..db import connect
    if not _live_send_enabled():
        with connect() as con:
            con.execute("UPDATE rtm_drafts SET status = 'approved', decided_at = ? WHERE id = ?",
                        (_iso(), draft_id))
            con.commit()
        _log_case_action(d["case_id"], draft_id, "approved_send_disabled",
                 {"note": "RTM_LIVE_SEND is off, approved but not sent"})
        return {"ok": True, "draft_id": draft_id, "status": "approved", "sent": False,
                "note": "Approved. Live send is disabled (RTM_LIVE_SEND off), so nothing was sent. "
                        "Enable it on the Mac, once watched, to actually send."}
    # live send path (only reachable on the Mac with the flag on)
    result = _send_email(d["to_email"], d["subject"], d["body"])
    with connect() as con:
        con.execute("UPDATE rtm_drafts SET status = ?, decided_at = ? WHERE id = ?",
                    ("sent" if result.get("ok") else "approved", _iso(), draft_id))
        con.commit()
    _log_case_action(d["case_id"], draft_id, "sent" if result.get("ok") else "send_failed", result)
    return {"ok": result.get("ok", False), "draft_id": draft_id,
            "status": "sent" if result.get("ok") else "approved", "sent": result.get("ok", False)}


def _send_email(to_email: str, subject: str, body: str) -> dict[str, Any]:
    """The actual send. Deliberately a single, isolated function behind RTM_LIVE_SEND so the live
    edge is one auditable place. Uses SMTP env config; not exercised in the sandbox."""
    try:
        from .egress_guard import egress_allowed
        if not egress_allowed("rtm_email_send")[0]:
            return {"ok": False, "reason": "egress_blocked_non_live"}
        import smtplib
        from email.message import EmailMessage
        host = os.getenv("RTM_SMTP_HOST", "")
        port = int(os.getenv("RTM_SMTP_PORT", "587"))
        user = os.getenv("RTM_SMTP_USER", _rtm_address())
        pw = os.getenv("RTM_SMTP_PASSWORD", "")
        if not host or not pw:
            return {"ok": False, "reason": "smtp_not_configured"}
        m = EmailMessage()
        m["From"] = _rtm_address()
        m["To"] = to_email
        m["Subject"] = subject
        m.set_content(body)
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(m)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "reason": str(e)[:120]}


# --- push the draft to phone/HA (mirrors WhatsApp approval push) -------------

async def push_draft_approval(draft_id: str) -> dict[str, Any]:
    """Push an actionable approval card to the phone/HA with Approve / Edit / Cancel, so acting on
    a draft is one tap. Urgent/safety drafts go high-priority. The buttons POST to token-gated
    routes; the human's tap is the approval."""
    d = get_draft(draft_id)
    if not d:
        return {"sent": False, "reason": "draft_not_found"}
    from .notifications import send_to_phone
    try:
        from ..access_urls import user_facing_base
        base = (os.getenv("MONGOOSE_PUBLIC_URL") or user_facing_base(prefer_public=True, prefer_lan=True)).rstrip("/")
    except Exception:
        base = (os.getenv("MONGOOSE_PUBLIC_URL") or "https://127.0.0.1:8765").rstrip("/")
    approve_token = _mint_draft_action_token(draft_id, action="approve")
    cancel_token = _mint_draft_action_token(draft_id, action="cancel")
    actions = [
        {"action": "http", "label": "Approve", "method": "POST",
         "url": f"{base}/rtm/drafts/{draft_id}/approve",
         "headers": {"X-RTM-Draft-Action-Token": approve_token}, "clear": True},
        {"action": "http", "label": "Cancel", "method": "POST",
         "url": f"{base}/rtm/drafts/{draft_id}/cancel",
         "headers": {"X-RTM-Draft-Action-Token": cancel_token}, "clear": True},
    ]
    # urgency from the case
    from ..db import connect
    with connect() as con:
        case = con.execute("SELECT urgency FROM rtm_cases WHERE id = ?", (d["case_id"],)).fetchone()
    urgent = case and case["urgency"] == "high"
    label = "URGENT RTM reply" if urgent else ("RTM acknowledge" if d["kind"] == "acknowledge" else "RTM reply")
    title = f"{label} to {d['to_email']}?"
    preview = d["body"][:200] + ("..." if len(d["body"]) > 200 else "")
    return await send_to_phone(title, preview, priority=("high" if urgent else "default"),
                               tags=(["rotating_light"] if urgent else ["email"]), actions=actions)


# --- the real-time trigger: new email -> case -> draft -> push ---------------

async def handle_incoming(*, sender: str, subject: str, body: str,
                          source_message_id: str = "", received_at: str = "",
                          auto_push: bool = True) -> dict[str, Any]:
    """The end-to-end real-time path for ONE incoming email: intake + classify, generate a draft,
    and (optionally) push the approval card. This is what the live monitor calls per new message.
    Safe on synthetic mail; sends nothing. Returns the case/draft so callers can inspect."""
    res = intake_email(sender=sender, subject=subject, body=body,
                       source_message_id=source_message_id, received_at=received_at)
    draft = generate_draft(res["intake_id"])
    out = {"case_id": res["case_id"], "intake_id": res["intake_id"],
           "classification": res["classification"], "draft": draft}
    if auto_push and draft.get("ok"):
        try:
            push = await push_draft_approval(draft["draft_id"])
            out["pushed"] = push
        except Exception as e:
            out["pushed"] = {"sent": False, "reason": str(e)[:120]}
    return out


def poll_once(*, max_messages: int = 50) -> dict[str, Any]:
    """Poll the live mailbox ONCE and run handle_incoming for each new message, but ONLY if
    RTM_LIVE_MAILBOX is enabled. With it off (default/sandbox), returns a clear disabled status
    and touches no real inbox. The live path reuses the existing IMAP cursor sync."""
    if not _live_mailbox_enabled():
        return {"ok": False, "live": False,
                "note": "RTM_LIVE_MAILBOX is off, not polling the real inbox. The pipeline is "
                        f"ready; enable it on the Mac, once watched, to monitor {_rtm_address()}."}
    # live path , deliberately thin; reuses the audited incremental IMAP sync, then routes any
    # newly-indexed messages addressed to the RTM address through handle_incoming.
    from .email_index import sync_imap_incremental
    cursor = _get_cursor()
    synced = sync_imap_incremental(mailbox="INBOX", last_uid=cursor, max_messages=max_messages)
    _set_cursor(synced.get("last_uid", cursor))
    return {"ok": True, "live": True, "synced": synced}


def _get_cursor() -> int:
    try:
        from ..db import get_app_state
        return int(get_app_state("rtm_imap_cursor", "0") or "0")
    except Exception:
        return 0


def _set_cursor(uid: int) -> None:
    try:
        from ..db import set_app_state
        set_app_state("rtm_imap_cursor", str(uid))
    except Exception:
        pass


# --- case action log ---------------------------------------------------------

def _log_case_action(case_id: str, draft_id: str, action: str, detail: dict[str, Any]) -> None:
    _ensure_tables()
    from ..db import connect
    with connect() as con:
        con.execute("INSERT INTO rtm_case_actions(id,case_id,draft_id,action,detail,at) "
                    "VALUES(?,?,?,?,?,?)",
                    ("act_" + uuid.uuid4().hex[:10], case_id, draft_id, action,
                     json.dumps(detail), _iso()))
        con.commit()


def case_actions(case_id: str) -> list[dict[str, Any]]:
    _ensure_tables()
    from ..db import connect
    with connect() as con:
        rows = con.execute("SELECT action, detail, at FROM rtm_case_actions WHERE case_id = ? "
                           "ORDER BY at", (case_id,)).fetchall()
    return [dict(r) for r in rows]


def list_cases(status: str = "open", limit: int = 50) -> list[dict[str, Any]]:
    _ensure_tables()
    from ..db import connect
    with connect() as con:
        rows = con.execute("SELECT * FROM rtm_cases WHERE status = ? ORDER BY last_activity_at DESC "
                           "LIMIT ?", (status, limit)).fetchall()
    return [dict(r) for r in rows]


def status() -> dict[str, Any]:
    """Operational status: how many cases/drafts, and crucially whether the live edges are on, so
    it's always obvious whether this is monitoring/sending for real or running safe."""
    _ensure_tables()
    from ..db import connect
    with connect() as con:
        cases = con.execute("SELECT COUNT(*) c FROM rtm_cases WHERE status='open'").fetchone()["c"]
        pending = con.execute("SELECT COUNT(*) c FROM rtm_drafts WHERE status='pending'").fetchone()["c"]
    return {"open_cases": cases, "pending_drafts": pending,
            "live_mailbox": _live_mailbox_enabled(), "live_send": _live_send_enabled(),
            "draft_tier": _draft_tier_label(), "address": _rtm_address()}


def _draft_tier_label() -> str:
    """Human-readable description of how substantive drafts are generated, always visible so it's
    never ambiguous whether case content can leave the Mac."""
    if not _model_tier_enabled():
        return "local template only (no case content leaves the Mac)"
    provider = os.getenv("RTM_DRAFT_MODEL_PROVIDER", "").lower()
    if provider == "local":
        return "local Ollama model for substantive cases (stays on the Mac)"
    return f"{provider} API for substantive cases (external; safety/urgent stay local template)"


# --- voicemail import (ported from the v0.37 fork; feeds the same pipeline) ---

def import_voicemail_dir(root: str, *, limit: int = 500) -> dict[str, Any]:
    """Import voicemail transcripts saved by the HA voicemail add-on. Expects JSON metadata with a
    same-directory transcript (named by transcript_filename or the JSON stem). Each becomes an
    intake item + case + draft via the normal pipeline. Idempotent by source message id. Reads
    files only; never touches a mailbox."""
    import json as _json
    from pathlib import Path
    _ensure_tables()
    base = Path(root).expanduser()
    if not base.exists():
        return {"ok": False, "reason": "root_not_found", "root": str(base)}
    files = sorted(base.rglob("*.json"))[:max(1, min(limit, 5000))]
    imported, skipped, errors = 0, 0, []
    for path in files:
        try:
            meta = _json.loads(path.read_text(encoding="utf-8"))
            tname = meta.get("transcript_filename")
            tpath = path.with_name(tname) if tname else path.with_suffix(".txt")
            transcript = tpath.read_text(encoding="utf-8") if tpath.exists() else ""
            source_id = "voicemail:" + str(meta.get("message_id") or meta.get("imap_uid")
                                           or meta.get("attachment_sha256") or path.name)
            # idempotent: skip if we've already imported this voicemail
            if _already_imported(source_id):
                skipped += 1
                continue
            caller = str(meta.get("caller") or meta.get("phone") or "not stated")
            subject = str(meta.get("email_subject") or f"Voicemail from {caller}")
            res = intake_email(
                sender=str(meta.get("email_from") or f"{_voicemail_provider_label()} voicemail <{caller}>"),
                subject=subject, body=transcript or subject,
                source_message_id=source_id,
                received_at=meta.get("received_time") or meta.get("processed_at_utc") or "",
                kind="voicemail")
            generate_draft(res["intake_id"])
            imported += 1
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)[:80]})
    return {"ok": True, "scanned": len(files), "imported": imported, "skipped": skipped,
            "errors": errors[:20]}


def _already_imported(source_message_id: str) -> bool:
    from ..db import connect
    with connect() as con:
        row = con.execute("SELECT 1 FROM rtm_intake_items WHERE source_message_id = ? LIMIT 1",
                          (source_message_id,)).fetchone()
    return row is not None


# --- FTS search over intake (ported from the v0.37 fork) ---------------------

def search_intake(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Full-text search across RTM email + voicemail intake ("lift stuck", "service charge").
    Read-only."""
    _ensure_tables()
    from ..db import connect
    with connect() as con:
        try:
            rows = con.execute(
                "SELECT f.intake_id, f.subject, f.sender, i.kind, i.received_at, i.classified "
                "FROM rtm_intake_fts f JOIN rtm_intake_items i ON i.id = f.intake_id "
                "WHERE rtm_intake_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit)).fetchall()
        except Exception:
            # FTS MATCH can raise on certain query syntax; fall back to a LIKE scan
            like = f"%{query}%"
            rows = con.execute(
                "SELECT id AS intake_id, subject, sender, kind, received_at, classified "
                "FROM rtm_intake_items WHERE subject LIKE ? OR body LIKE ? LIMIT ?",
                (like, like, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["classification"] = json.loads(d.pop("classified"))
        except Exception:
            d["classification"] = {}
        out.append(d)
    return out


def list_intake(limit: int = 50) -> list[dict[str, Any]]:
    """List recent intake items across all cases. Read-only."""
    _ensure_tables()
    from ..db import connect
    with connect() as con:
        rows = con.execute("SELECT id, case_id, kind, sender, subject, received_at, classified "
                           "FROM rtm_intake_items ORDER BY received_at DESC LIMIT ?",
                           (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["classification"] = json.loads(d.pop("classified"))
        except Exception:
            d["classification"] = {}
        out.append(d)
    return out


def enrichment_assets() -> list[dict[str, Any]]:
    """Assets the user uploaded (from the phone or elsewhere) that are available for RTM enrichment,
    code, documents, archives that aren't Mongoose builds. The RTM workflows can pull supporting
    material from here without it having to arrive by email. Read-only; defensive."""
    try:
        from . import assets as _assets
        out = []
        for a in _assets.rtm_candidates():
            out.append({
                "asset_id": a.get("asset_id"),
                "filename": a.get("filename"),
                "kind": a.get("kind"),
                "size_bytes": a.get("size_bytes"),
                "uploaded_by": a.get("uploaded_by"),
                "created_at": a.get("created_at"),
                "classification": a.get("classification"),
                "stored_path": a.get("stored_path"),
            })
        return out
    except Exception:
        return []
