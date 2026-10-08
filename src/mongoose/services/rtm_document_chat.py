"""Chat bridge for RTM document generation: "generate a receipt for 3ECF9" (default-profile code), "make Susan's welcome
letter", "draft the cover email for flat 5". Rule-based (no magic prompt): it parses the template and
the flat, calls the ONE shared generator (rtm_templates.generate_document), and hands back a reply with
a downloadable capability link for docx, or the rendered draft text for a cover email. It never sends
anything: it produces the document/draft only.
"""
from __future__ import annotations

import re
from typing import Any

from .. import profile as _profile

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_STOP = {"generate", "create", "make", "produce", "draft", "prepare", "issue", "send", "email",
         "print", "download", "receipt", "welcome", "letter", "cover", "covering", "the", "for",
         "please", "mongoose", "demand", "service", "charge", "flat", "with", "and", "get", "me"}


def _block_parts() -> tuple[str, str, str]:
    """(block numbers, block-code letters, street first words) as regex alternations, from the site
    profile. With the fictional default: ('1|3', 'ec', 'example')."""
    bl = _profile.blocks()
    nums = sorted({b["number"] for b in bl}, key=len, reverse=True)
    letters = sorted({re.sub(r"^[0-9]+", "", b["code"]).lower() for b in bl}, key=len, reverse=True)
    streets = sorted({(b["street"].split() or [""])[0].lower() for b in bl}, key=len, reverse=True)
    esc = lambda xs: "|".join(re.escape(x) for x in xs if x)  # noqa: E731
    return esc(nums), esc(letters), esc(streets)


def _code_for(number: str) -> str:
    """Block code for a spoken block number ('3' -> '3EC' with the default profile)."""
    return _profile.block_for(number) or number


def _parse_flat(text: str) -> str:
    nums, letters, streets = _block_parts()
    # <code>F9 / <code> F9 / '<n> <letters> f 9', e.g. 3ECF9 / 3EC F9 / 3 ec f 9 with the default profile
    m = re.search(rf"\b({nums})\s?(?:{letters})\s?f?\s?(\d)\b", text or "", re.I)
    if m:
        return f"{_code_for(m.group(1))}F{m.group(2)}"
    mf = re.search(r"\bflat\s+(\d)\b", text or "", re.I)
    mb = re.search(rf"\b({nums})\s*(?:{streets})\b|\b({nums})\s?(?:{letters})\b", text or "", re.I)
    if mf and mb:
        blk = mb.group(1) or mb.group(2)
        return f"{_code_for(blk)}F{mf.group(1)}"
    return ""


def _match_by_name(text: str) -> str:
    """No flat given: match a capitalised name in the message against a leaseholder on a receipt."""
    try:
        from .rtm_trust import list_receipts
        recs = list_receipts()
    except Exception:
        return ""
    for w in re.findall(r"\b([A-Z][a-z]{2,})\b", text or ""):
        if w.lower() in _STOP:
            continue
        for r in recs:
            if w.lower() in (r.get("leaseholder", "") or "").lower():
                return r.get("flat_id", "") or ""
    return ""


def _abs(url: str) -> str:
    """Make a capability link absolute when a public base is known (so it is clickable in phone chat),
    else return it relative (the PWA resolves it against its own origin)."""
    if not url:
        return url
    try:
        from .remote_access import public_base_url
        base = public_base_url()
        if base:
            return base.rstrip("/") + url
    except Exception:
        pass
    return url


_EXPORT_FMT_WORDS = [
    ("quickbooks", "quickbooks"), ("quickbook", "quickbooks"), ("qbo", "quickbooks"), ("qb", "quickbooks"),
    ("gnucash", "gnucash"), ("gnu cash", "gnucash"), ("xero", "xero"), ("qif", "qif"), ("ofx", "ofx"),
    ("xlsx", "xlsx"), ("excel", "xlsx"), ("spreadsheet", "xlsx"), ("workbook", "xlsx"),
    ("pdf", "pdf"), ("json", "json"),
]


def _is_export_request(low: str) -> bool:
    """True for 'export the accounts', 'accountant pack', 'export the books as xero', etc. Kept tight so
    it does not hijack ordinary chat that merely mentions 'export'."""
    if "export" in low and any(k in low for k in ("account", "book", "financ", "pack", "ledger", "recon")):
        return True
    if "accountant" in low and any(k in low for k in ("export", "pack", "send", "give", "for")):
        return True
    if ("accounts" in low
            and re.search(r"\b(export|give|make|produce|send|want|need|prepare|generate|download|get)\b", low)
            and any(re.search(r"\b" + re.escape(w) + r"\b", low) for w, _ in _EXPORT_FMT_WORDS)):
        return True
    return False


def _parse_export_formats(low: str):
    if re.search(r"\b(everything|all formats|every format|all the formats|full pack|the lot)\b", low):
        return None  # None => every format
    picked = []
    for word, fmt in _EXPORT_FMT_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", low) and fmt not in picked:
            picked.append(fmt)
    return picked or None


def _maybe_export(t: str, low: str):
    if not _is_export_request(low):
        return None
    from .rtm_export import build_export
    from .rtm_templates import mint_download
    formats = _parse_export_formats(low)
    include_evidence = not any(p in low for p in
                               ("no evidence", "without evidence", "no docs", "without docs", "no documents"))
    res = build_export(year="2027", formats=formats, include_evidence=include_evidence)
    if not res.get("ok"):
        return {"reply": f"I couldn't build the export: {res.get('error', 'unknown')}.", "source": "rtm_export"}
    path = res.get("path")
    if not path:
        return {"reply": "I built the pack but couldn't stage it for download; try the Cockpit Export card.",
                "source": "rtm_export"}
    token = mint_download(path, res["filename"], ttl_s=3600, media_type="application/zip")
    link = _abs(f"/rtm/doc/{token}")
    man = res.get("manifest", {})
    ev = man.get("evidence_files", 0)
    parts = ", ".join(man.get("formats", []))
    size_mb = res.get("size", 0) / 1024 / 1024
    return {"reply": (f"Built the accountant export ({parts}"
                      + (f"; {ev} evidence files" if ev else "; no evidence bundle")
                      + f"; {size_mb:.1f} MB). Download (link valid about an hour):\n{link}\n\n"
                      f"File: {res['filename']}. Bookkeeper-ready pack for your accountant, "
                      "not a signed statutory account."),
            "source": "rtm_export"}


def maybe_handle_rtm_document(prompt: str, conversation_id: str = "") -> dict[str, Any] | None:
    """Return a chat result for an RTM document request, or None to fall through to normal chat."""
    t = (prompt or "").strip()
    if not t:
        return None
    low = t.lower()
    exported = _maybe_export(t, low)
    if exported is not None:
        return exported
    if "welcome" in low:
        template, label = "welcome", "welcome letter"
    elif "cover" in low or "e-mail" in low or "email" in low or low.startswith("send") or " send " in low:
        template, label = "cover_email_receipt_with_demand", "cover email"
    else:
        template, label = "payment_receipt", "receipt"

    flat = _parse_flat(t) or _match_by_name(t)
    if not flat:
        return {"reply": (f"Which flat is that for? Give me the flat (e.g. {_profile.block_codes()[-1]}F9) or the leaseholder's "
                          "name and I'll generate the " + label + "."), "source": "rtm_document"}

    recipient = ""
    em = _EMAIL_RE.search(t)
    if em:
        recipient = em.group(0)

    from .rtm_templates import generate_document
    res = generate_document(template, flat_id=flat, recipient_email=recipient)

    if not res.get("ok"):
        reason = res.get("reason", "unknown")
        hint = res.get("hint", "")
        if reason == "no_receipt":
            msg = (f"I couldn't find a settled demand for {flat} to base a {label} on yet. "
                   "A receipt exists once a demand reaches zero outstanding.")
        elif reason == "docxtpl_missing":
            msg = "The document engine isn't installed on this machine yet (pip install docxtpl)."
        else:
            msg = f"I couldn't generate the {label} for {flat}: {reason}."
        if hint:
            msg += " " + hint
        return {"reply": msg, "source": "rtm_document"}

    if res.get("kind") == "email":
        head = (f"Here is the {label} for {flat}, ready to review and copy."
                if res.get("ready_to_send")
                else f"Here is the {label} for {flat} as a DRAFT (no email address on file yet, so nothing "
                     "is sent). Fill in the address before sending:")
        return {"reply": head + "\n\n" + res.get("text", ""), "source": "rtm_document"}

    link = _abs(res.get("download_url", ""))
    return {"reply": (f"Generated the branded {label} for {flat}. Download it here (link valid about an "
                      f"hour):\n{link}\n\nFile: {res.get('filename')}. Nothing has been sent."),
            "source": "rtm_document"}
