"""Unified RTM document templating: ONE source of truth for the Example Court branded documents.

M supplied a designed template pack (three branded .docx: a 4-page service-charge demand, a payment
receipt, a general letter; plus two cover-email texts and a welcome-letter content preset). Before this
module, receipt branding was previously hand-drawn a second time in reportlab (since removed) with a
DIFFERENT contact email, which is exactly the two-copies-that-drift problem. This module owns the pack:

  - BRANDING: the single canonical set of company/contact/branding facts. Everything that prints a
    Example Court document reads these, so the address and email can never disagree across surfaces.
  - FIELDS: the merge-field contract per template (mirrors the pack's fields.py). Tests assert the
    live .docx tags and these sets stay in lockstep, so a swapped template can't silently lose a field.
  - render_email(): pure-Python {{field}} substitution for the cover emails. No dependency, always
    works. Honours the pack rule that a bracketed/blank recipient means DRAFT, never send.
  - render_docx(): renders a branded .docx via docxtpl (Jinja-in-Word). Degrades honestly with an
    install hint if docxtpl is absent. Output is .docx; PDF export needs LibreOffice/soffice on the box
    (documented, not silently faked).
  - context builders: turn Mongoose's real data (a rtm_trust receipt row, a demand + budget) into the
    template context, owning all money/date formatting so the templates stay dumb.

The templates themselves live in assets/rtm_templates/ (docx/, email/, logo.png). The s.21B statutory
summary inside the demand is verbatim SI 2007/1257 text baked into the .docx: it is deliberately NOT a
field and must never be templated or edited.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import profile as _profile
from .rtm_example_court import company_profile as _company_profile

_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "rtm_templates"
_DOCX_DIR = _ASSETS / "docx"
_EMAIL_DIR = _ASSETS / "email"


def _docx_source(filename: str) -> Path:
    """Resolve a branded .docx template, preferring the per-user brain dir over the packaged asset,
    same precedence as property.json: <data_dir>/rtm_templates/docx/<file> if it exists, else the
    packaged template. The packaged .docx carry a FICTIONAL letterhead (no real address / director /
    phone / bank); the real branded letterhead lives only in the data dir on the live machine."""
    try:
        from ..settings import get_settings
        override = Path(get_settings().data_dir) / "rtm_templates" / "docx" / filename
        if override.exists():
            return override
    except Exception:
        pass
    return _DOCX_DIR / filename

# The ONE canonical branding. The company IDENTITY (director, phone, bank, block addresses) is no
# longer a source literal: it comes from the property.json `company` block via company_profile()
# (data_dir = real on the live machine, packaged asset = fictional), so this file carries no real
# name/address/phone/bank. Change identity in that config and every document follows. The bank block
# still matches the "How to pay" paragraph in the (data-dir) service_charge_demand.docx; a fidelity
# test asserts each string appears in the demand PDF.
_CO = _company_profile()
_BANK = _CO.get("bank", {})
BRANDING: dict[str, str] = {
    "company": _CO["name"],
    "director": _CO["director"],
    "director_title": _CO.get("director_title", "Director"),
    "sign_name": _CO.get("sign_name") or (_CO["director"].split() or [""])[0],
    "tel": _CO["tel"],
    "email": _profile.rtm_email(),
    "motto": _CO["motto"],
    "bank_name": _BANK.get("name", ""),
    "bank_sort_code": _BANK.get("sort_code", ""),
    "bank_account_number": _BANK.get("account_number", ""),
    "bank_account_name": _BANK.get("account_name", _CO["name"]),
}

# Block -> (road, postcode) for building the `premises` line, from the same company config. 3EC is the
# stack-swap of 1EC; the postcodes differ by building (see the pack's fields.py premises note).
_BLOCKS: dict[str, tuple[str, str]] = {
    b: (v.get("road", ""), v.get("postcode", "")) for b, v in _CO.get("blocks", {}).items()
}
_TOWN: str = _CO.get("town", "")

# Merge-field contract per template. `scalars` are {{snake_case}} fields; `loops` are the repeating
# structures ({%tr%} table rows / {%p%} paragraph loops) with the keys each row needs (None = a list of
# plain strings). Kept in lockstep with the live .docx by test_rtm_templates.py.
FIELDS: dict[str, dict[str, Any]] = {
    "service_charge_demand": {
        "file": "service_charge_demand.docx",
        "scalars": [
            "demand_date", "doc_number", "leaseholder_name", "addr_line_1", "addr_line_2", "premises",
            "greeting_name", "opening_sentence", "period", "sch1_pct", "sch1_annual", "sch1_half",
            "uc_pct", "uc_annual", "uc_half", "total_due", "due_date", "payment_ref", "budget_year_end",
            "sch1_subtotal", "sch2_subtotal", "total_annual", "half_year_all",
        ],
        "loops": {"sch1_items": ["label", "amount"], "sch2_items": ["label", "amount"]},
    },
    "payment_receipt": {
        "file": "payment_receipt.docx",
        "scalars": [
            "issue_date", "receipt_no", "leaseholder_name", "addr_line_1", "addr_line_2", "premises",
            "greeting_name", "payer_name", "payment_ref", "period", "doc_number", "amount_received",
            "received_date", "method", "outstanding",
        ],
        "loops": {},
    },
    # Same letterhead as branded_letter, but its paragraph loop is {{r para }} so paragraphs can carry
    # bold and colour. A SEPARATE template because branded_letter is used by the welcome and payment-plan
    # letters, which pass plain strings: switching that one to rich text would break them.
    "overdue_notice": {
        "file": "overdue_notice.docx",
        "scalars": ["letter_date", "leaseholder_name", "addr_line_1", "addr_line_2", "greeting_name",
                    "sign_off"],
        "loops": {"paragraphs": None},
    },
    "branded_letter": {
        "file": "branded_letter.docx",
        "scalars": ["letter_date", "leaseholder_name", "addr_line_1", "addr_line_2", "greeting_name",
                    "sign_off"],
        "loops": {"paragraphs": None},
    },
}

EMAIL_TEMPLATES: dict[str, dict[str, Any]] = {
    "cover_email_receipt": {
        "file": "cover_email_receipt.txt",
        "fields": ["recipient_email", "flat_number", "road", "first_name", "amount_received", "received_date"],
    },
    "cover_email_receipt_with_demand": {
        "file": "cover_email_receipt_with_demand.txt",
        "fields": ["recipient_email", "flat_number", "road", "first_name", "amount_received", "received_date"],
    },
    # Payment-now-due cover email that carries the attached demand PDF. Repeats the amount, bank block
    # and payment reference in the body so a leaseholder can pay without opening the attachment. The
    # authored copy is strictly dash-free (test_rtm_demand_mailing enforces it); the only permitted
    # dashes anywhere in this mailing are the verbatim s.21B dashes inside the demand PDF itself.
    # LPE1 stage 2 release email (the first live one: Flat 7, 23 September 2026). Sent with two PDFs: the
    # covering letter and the completed LPE1 pack (rtm_lpe1_stage2). Drafted, never sent automatically.
    "cover_email_lpe1_stage2": {
        "file": "cover_email_lpe1_stage2.txt",
        "fields": ["recipient_email", "email_salutation", "property_short", "their_ref"],
    },
    "cover_email_demand": {
        "file": "cover_email_demand.txt",
        "fields": ["recipient_email", "greeting_name", "flat_number", "road", "period_end",
                   "amount_due", "due_date", "payment_ref", "bank_name", "bank_sort_code",
                   "bank_account_number", "bank_account_name"],
    },
}

_TAG_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


# --------------------------------------------------------------------------- helpers

def premises(block: str, flat: int | str) -> str:
    """Build the unambiguous demised-premises line, e.g. 'Flat 9, <block road>, <town> <postcode>'."""
    b = (block or "").strip().upper()[:3]
    road, postcode = _BLOCKS.get(b, ("", ""))
    tail = f", {_TOWN} {postcode}".rstrip()
    return f"Flat {flat}, {road}{tail}"


def split_address(address: str) -> tuple[str, str]:
    """Best-effort split a one-line address into the two lines the templates print. Prefers an explicit
    newline, else the last comma (town + postcode on line 2), else everything on line 1."""
    a = (address or "").strip()
    if not a:
        return "", ""
    if "\n" in a:
        first, rest = a.split("\n", 1)
        return first.strip(), rest.replace("\n", ", ").strip()
    if "," in a:
        i = a.rfind(",")
        return a[:i].strip(), a[i + 1:].strip()
    return a, ""


def greeting_from(name: str) -> str:
    """A friendly salutation name from a full legal name. Joint holders ('A and B Smith') are kept;
    a single full name is used as-is (the caller can override for the 'Dr Morgan' style)."""
    return (name or "").strip()


_TITLES = {"dr", "mr", "mrs", "ms", "miss", "mx", "prof", "professor", "sir", "dame", "rev",
           "revd", "reverend", "lord", "lady", "capt", "captain", "major", "col"}


def first_name_from(name: str) -> str:
    """Informal first name for the warmer email salutation. Joint holders keep the 'A and B' head.

    TITLES ARE SKIPPED. Taking the first token produced "Dear Dr" for "Dr Alex James Morgan" and for
    "Dr Ivan Petrov", and "Dear Mr" for any "Mr & Mrs" holder. This helper feeds the demand letters, the
    payment-plan letters and the chasers alike, so one wrong line reached real leaseholders from every
    surface at once.
    """
    n = (name or "").strip()
    if not n:
        return ""
    if " and " in n.lower() or "&" in n:
        return n  # 'Brett and Karen' / 'Mr & Mrs Smith' style stays whole
    parts = [p for p in n.split() if p]
    while parts and parts[0].lower().strip(".") in _TITLES:
        parts.pop(0)
    return parts[0] if parts else n.split()[0]


# --------------------------------------------------------------------------- validation

def validate_context(template: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Check a context dict against a template's field contract. Returns {ok, missing, empty, unknown,
    loop_problems}. `missing` = required scalar absent; `empty` = present but blank (would print a hole);
    `unknown` = a key the template does not use (usually a typo). Loops must be lists with the right keys."""
    spec = FIELDS.get(template)
    if not spec:
        return {"ok": False, "missing": [], "empty": [], "unknown": [], "loop_problems": [f"unknown template {template!r}"]}
    scalars = set(spec["scalars"])
    loops = spec["loops"]
    allowed = scalars | set(loops)
    missing = [f for f in scalars if f not in ctx]
    empty = [f for f in scalars if f in ctx and str(ctx.get(f, "")).strip() == ""]
    unknown = [k for k in ctx if k not in allowed]
    loop_problems: list[str] = []
    for name, keys in loops.items():
        if name not in ctx:
            loop_problems.append(f"missing loop {name}")
            continue
        rows = ctx[name]
        if not isinstance(rows, list):
            loop_problems.append(f"{name} must be a list")
            continue
        if keys:
            for i, row in enumerate(rows):
                if not isinstance(row, dict) or any(k not in row for k in keys):
                    loop_problems.append(f"{name}[{i}] needs keys {keys}")
    ok = not (missing or empty or unknown or loop_problems)
    return {"ok": ok, "missing": missing, "empty": empty, "unknown": unknown, "loop_problems": loop_problems}


# --------------------------------------------------------------------------- email rendering

def _site_copy(raw: str) -> str:
    """Put the active site's identity into authored email copy. Copy may use {{site.*}} profile
    placeholders (dotted, so the single-word {{field}} merge below never sees them). The shipped
    templates are authored against the fictional default profile and carry its RTM address verbatim
    (test_rtm_templates pins that text), so that address is swapped for the active profile's here; with
    the default profile this is a no-op."""
    import json
    raw = _profile.render(raw)
    try:
        default_email = json.loads(_profile.PACKAGED_PROFILE.read_text(encoding="utf-8"))["site"]["rtm_email"]
    except Exception:
        return raw
    active = _profile.rtm_email()
    return raw.replace(default_email, active) if default_email and default_email != active else raw


def render_email(template: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """Render a cover email by pure {{field}} substitution (no dependency). Returns {ok, text,
    ready_to_send, missing}. Honours the pack rule: a blank/bracketed recipient means the email is a
    DRAFT and ready_to_send is False, so nothing half-filled is ever sent."""
    spec = EMAIL_TEMPLATES.get(template)
    if not spec:
        return {"ok": False, "reason": f"unknown email template {template!r}"}
    raw = _site_copy((_EMAIL_DIR / spec["file"]).read_text(encoding="utf-8"))
    # Seed the company identity so signature placeholders ({{director}}, {{tel}}, {{email}}) always
    # resolve from config, never a source literal. Caller ctx overrides if it supplies these.
    base = {"director": BRANDING["director"], "director_title": BRANDING["director_title"],
            "sign_name": BRANDING["sign_name"], "tel": BRANDING["tel"], "email": BRANDING["email"],
            "company": BRANDING["company"]}
    merged = {**base, **ctx}
    used = set(_TAG_RE.findall(raw))
    missing = [f for f in used if not str(merged.get(f, "")).strip()]

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        val = merged.get(key, "")
        return str(val) if str(val).strip() else f"[{key}]"

    text = _TAG_RE.sub(_sub, raw)
    ctx = merged
    recipient = str(ctx.get("recipient_email", "")).strip()
    ready = bool(recipient) and "[" not in text
    return {"ok": True, "text": text, "ready_to_send": ready, "missing": missing}


# --------------------------------------------------------------------------- docx rendering

def docx_available() -> bool:
    try:
        import docxtpl
        return bool(docxtpl)
    except Exception:
        return False


def render_docx(template: str, ctx: dict[str, Any], out_path: str | Path | None = None,
                *, validate: bool = True) -> dict[str, Any]:
    """Render a branded .docx from its template + context. Validates the context first (unless
    validate=False), then renders with docxtpl. Returns {ok, path, ...} or an honest failure with a
    reason. Output is .docx; converting to PDF needs LibreOffice/soffice on the machine (see to_pdf)."""
    spec = FIELDS.get(template)
    if not spec:
        return {"ok": False, "reason": f"unknown template {template!r}"}
    if validate:
        v = validate_context(template, ctx)
        if not v["ok"]:
            return {"ok": False, "reason": "context_invalid", "validation": v}
    if not docx_available():
        return {"ok": False, "reason": "docxtpl_missing",
                "hint": "Install the document engine once: pip install docxtpl (adds jinja2). Cover "
                        "emails render without it; only the branded .docx needs it."}
    from docxtpl import DocxTemplate
    src = _docx_source(spec["file"])
    if out_path is None:
        out_path = _output_dir() / f"{template}_{_safe(ctx.get('doc_number') or ctx.get('receipt_no') or template)}.docx"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tpl = DocxTemplate(str(src))
    tpl.render(dict(ctx))
    tpl.save(str(out_path))
    return {"ok": True, "path": str(out_path), "template": template, "bytes": out_path.stat().st_size}


def to_pdf(docx_path: str | Path) -> dict[str, Any]:
    """Convert a rendered .docx to PDF using LibreOffice headless if it is installed. Honest no-op with
    a hint otherwise (this Mac has no soffice); the .docx is still a valid, printable deliverable."""
    import shutil
    import subprocess
    docx_path = Path(docx_path)
    soffice = (shutil.which("soffice") or shutil.which("libreoffice")
               or ("/Applications/LibreOffice.app/Contents/MacOS/soffice"
                   if Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").exists() else None))
    if not soffice:
        return {"ok": False, "reason": "no_pdf_engine",
                "hint": "PDF export needs LibreOffice (brew install --cask libreoffice), then soffice "
                        "converts the .docx headless. The .docx itself is a valid deliverable."}
    try:
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir",
                        str(docx_path.parent), str(docx_path)], capture_output=True, timeout=120, check=True)
    except Exception as exc:
        return {"ok": False, "reason": f"convert_failed: {exc}"}
    pdf = docx_path.with_suffix(".pdf")
    return {"ok": pdf.exists(), "path": str(pdf)} if pdf.exists() else {"ok": False, "reason": "no_output"}


def _output_dir() -> Path:
    from ..settings import get_settings
    d = get_settings().db_path.parent / "rtm" / "documents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(s: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s or "doc")).strip("_") or "doc"


# --------------------------------------------------------------------------- context builders

def receipt_context(receipt: dict[str, Any]) -> dict[str, Any]:
    """Map a rtm_trust receipt row (build_receipt / list_receipts output) onto payment_receipt fields.
    The generator owns display formatting; the template stays dumb."""
    name = receipt.get("leaseholder", "") or ""
    a1, a2 = split_address(receipt.get("address", ""))
    return {
        "issue_date": receipt.get("issue_date", ""),
        "receipt_no": receipt.get("receipt_number", ""),
        "leaseholder_name": name,
        "addr_line_1": a1,
        "addr_line_2": a2,
        "premises": receipt.get("premises", "") or receipt.get("address", ""),
        "greeting_name": greeting_from(name),
        "payer_name": name,
        "payment_ref": receipt.get("payment_ref", ""),
        "period": receipt.get("period", ""),
        "doc_number": receipt.get("document_number", ""),
        "amount_received": receipt.get("amount", ""),
        "received_date": receipt.get("received_date", ""),
        "method": receipt.get("method", "") or "Bank transfer",
        "outstanding": receipt.get("outstanding", "") or "£0.00",
    }


def receipt_email_context(receipt: dict[str, Any], recipient_email: str = "",
                          flat_number: int | str = "", road: str = "") -> dict[str, Any]:
    """Cover-email context for a receipt. recipient_email blank => the email renders as a DRAFT."""
    return {
        "recipient_email": recipient_email or "",
        "flat_number": flat_number or "",
        "road": road or "",
        "first_name": first_name_from(receipt.get("leaseholder", "")),
        "amount_received": receipt.get("amount", ""),
        "received_date": receipt.get("received_date", ""),
    }


def welcome_context(letter_date: str, leaseholder_name: str, addr_line_1: str, addr_line_2: str,
                    flat_number: int | str, road: str, greeting_name: str = "") -> dict[str, Any]:
    """Welcome letter (branded_letter) for a leaseholder's FIRST payment. The paragraphs are the pack's
    approved wording (optional electronic-delivery invitation, no-obligation tone)."""
    return {
        "letter_date": letter_date,
        "leaseholder_name": leaseholder_name,
        "addr_line_1": addr_line_1,
        "addr_line_2": addr_line_2,
        "greeting_name": greeting_name or greeting_from(leaseholder_name),
        "sign_off": "With best wishes,",
        "paragraphs": [
            (f"I am delighted to confirm that we have received your first service charge payment for "
             f"Flat {flat_number}, {road}. Thank you. A formal receipt is enclosed, and your account is "
             f"fully up to date with nothing outstanding."),
            ("As the right to manage company now runs the buildings, I want to make sure our records for "
             "you are complete and that you can reach us easily. If you would like to, please email us at "
             f"{BRANDING['email']} to confirm your email address and a contact telephone number."),
            ("Confirming your email address also gives you the option of receiving documents "
             "electronically, such as receipts, demands and notices, alongside or instead of post, "
             "whichever you prefer. There is no obligation, and you can continue to receive everything by "
             "post if that suits you better."),
            ("If you have any questions about your account or the buildings, you are very welcome to get "
             "in touch at any time."),
        ],
    }


def letter_context(letter_date: str, leaseholder_name: str, addr_line_1: str, addr_line_2: str,
                    paragraphs: list[str], sign_off: str = "With best wishes,",
                    greeting_name: str = "") -> dict[str, Any]:
    """Generic branded-letter context for any correspondence composed elsewhere."""
    return {
        "letter_date": letter_date,
        "leaseholder_name": leaseholder_name,
        "addr_line_1": addr_line_1,
        "addr_line_2": addr_line_2,
        "greeting_name": greeting_name or greeting_from(leaseholder_name),
        "sign_off": sign_off,
        "paragraphs": list(paragraphs or []),
    }


# --------------------------------------------------------------------------- download tokens

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def mint_download(path: str | Path, filename: str, ttl_s: int = 3600,
                  media_type: str = _DOCX_MIME) -> str:
    """Mint a short-lived capability token for a generated file so it can be handed out as a plain link
    (a chat message, a cockpit button) without threading a nonce through a browser navigation. The token
    is high-entropy and expires; the file itself stays owner-private on disk. Persisted in app_state so
    a link survives a server restart within its TTL. media_type lets the same mechanism serve docx, pdf,
    etc. (the serving route reads it back)."""
    import json
    import secrets
    import time
    from ..db import set_app_state
    token = secrets.token_urlsafe(24)
    set_app_state(f"rtmdl:{token}",
                  json.dumps({"path": str(path), "filename": filename, "at": time.time(),
                              "ttl": ttl_s, "media_type": media_type}))
    return token


def resolve_download(token: str) -> dict[str, Any] | None:
    """Resolve a download token to {path, filename} if it is valid, unexpired, and the file still exists."""
    import json
    import time
    from ..db import get_app_state
    raw = get_app_state(f"rtmdl:{(token or '').strip()}")
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    if time.time() - float(d.get("at", 0)) > float(d.get("ttl", 3600)):
        return None
    p = Path(str(d.get("path") or ""))
    if not p.exists():
        return None
    return {"path": str(p), "filename": d.get("filename") or p.name,
            "media_type": d.get("media_type") or _DOCX_MIME}


def _today() -> str:
    import datetime
    try:
        return datetime.date.today().strftime("%-d %B %Y")
    except ValueError:  # non-GNU strftime fallback
        return datetime.date.today().strftime("%d %B %Y").lstrip("0")


def _finish_docx(r: dict[str, Any], filename: str, ttl_s: int) -> dict[str, Any]:
    if not r.get("ok"):
        return r
    token = mint_download(r["path"], filename, ttl_s)
    r.update({"kind": "docx", "filename": filename,
              "download_url": f"/rtm/doc/{token}", "download_token": token})
    return r


def generate_document(template: str, *, flat_id: str = "", document_number: str = "",
                      recipient_email: str = "", leaseholder_name: str = "", address: str = "",
                      letter_date: str = "", download_ttl_s: int = 3600) -> dict[str, Any]:
    """ONE entry point for producing a document from real data, shared by the cockpit button, the chat
    action, and the API. Locates the receipt (for receipt/cover-email) by flat + optional demand number.
    docx results carry a `download_url` (an /rtm/doc/<token> capability link); email results carry the
    rendered text and ready_to_send. Never sends anything: it only produces the document/draft."""
    from . import rtm_trust as _RT
    fid = (flat_id or "").strip().upper().replace("-", "").replace(" ", "")
    block, _, flatn = fid.partition("F")
    road = _BLOCKS.get(block[:3], ("", ""))[0]

    def _find() -> dict | None:
        for r in _RT.list_receipts(fid or None):
            if not document_number or r.get("document_number") == document_number:
                return r
        return None

    if template in ("cover_email_receipt", "cover_email_receipt_with_demand"):
        rec = _find()
        if not rec:
            return {"ok": False, "reason": "no_receipt", "flat_id": fid,
                    "hint": "No matching receipt for that flat/demand yet (a receipt is issued when a "
                            "demand reaches zero outstanding)."}
        res = render_email(template, receipt_email_context(rec, recipient_email=recipient_email,
                                                           flat_number=flatn, road=road))
        res.update({"kind": "email", "flat_id": fid})
        return res

    if template == "payment_receipt":
        rec = _find()
        if not rec:
            return {"ok": False, "reason": "no_receipt", "flat_id": fid,
                    "hint": "No matching receipt for that flat/demand yet."}
        r = render_docx("payment_receipt", receipt_context(rec))
        return _finish_docx(r, f"Receipt_{_safe(fid)}.docx", download_ttl_s)

    if template in ("welcome", "branded_letter"):
        rec = _find() or {}
        name = leaseholder_name or rec.get("leaseholder", "")
        a1, a2 = split_address(address or rec.get("address", ""))
        ctx = welcome_context(letter_date=letter_date or _today(), leaseholder_name=name,
                              addr_line_1=a1, addr_line_2=a2, flat_number=flatn or "", road=road)
        r = render_docx("branded_letter", ctx)
        return _finish_docx(r, f"Welcome_{_safe(fid)}.docx", download_ttl_s)

    return {"ok": False, "reason": "unknown_template",
            "available": list(FIELDS) + list(EMAIL_TEMPLATES)}


def catalogue() -> dict[str, Any]:
    """Machine-readable summary of what the pack can produce, for a cockpit/plugin surface."""
    return {
        "branding": dict(BRANDING),
        "docx": {name: {"file": spec["file"], "scalars": spec["scalars"], "loops": list(spec["loops"])}
                 for name, spec in FIELDS.items()},
        "email": {name: dict(spec) for name, spec in EMAIL_TEMPLATES.items()},
        # LPE1 stage 2 release (covering letter PDF, completed LPE1 pack PDF, cover email): see rtm_lpe1_stage2.
        "lpe1_stage2": {"letter": "Stage2_1_Cover_Letter_<flat>_LPE1.pdf",
                        "pack": "Stage2_2_LPE1_Pack_<flat>_SIGN_PAGE_11.pdf",
                        "email": "cover_email_lpe1_stage2", "render": "rtm_lpe1_stage2.render_job(<job file>)"},
        "docx_engine": docx_available(),
    }
