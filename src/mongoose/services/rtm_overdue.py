"""Overdue service charge correspondence: one stage decision, rendered three ways.

The .docx, the WordPress portal text and the chaser email all render from `overdue_stage()`. Three
surfaces deciding independently what "overdue" means is how this system has drifted before (three phone
tile lists, three CC reads, three flat counts), and here the cost is a leaseholder getting a red banner on
the portal and a friendly nudge by email on the same morning.

WHAT THESE LETTERS MAY NOT SAY. Mongoose holds no lease wording: no interest clause, no recovery-costs
clause, no forfeiture provision. So nothing here asserts a right to interest, administration charges or
costs. Claiming a contractual right that cannot be evidenced is the expensive mistake in this area. If the
lease does provide for them, put the clause in config and the wording can then state it accurately.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .. import profile as _profile
from . import rtm_management as MGMT
from . import rtm_templates as TPL

# Day thresholds. Deliberately gentle: this is the first charging cycle since RTM acquisition on
# 16 July 2026 and the directors are the leaseholders' neighbours.
STAGES = (
    ("reminder", 1, 14),
    ("formal", 15, 45),
    ("final", 46, 90),
)
# Below this a letter costs more than the debt.
DE_MINIMIS_PENCE = 2500


def _today() -> date:
    return date.today()


def overdue_stage(flat_id: str, *, today: str = "", force: bool = False) -> dict[str, Any]:
    """Where this flat sits on the chasing ladder, and every fact the three renderings need.

    Returns stage='none' with a reason whenever the flat must NOT be chased, so the exclusions live here
    once rather than being re-implemented (and re-forgotten) in each template.
    """
    fid = (flat_id or "").strip().upper().replace("-", "").replace(" ", "")
    fl = MGMT._get_flat(fid) or {}
    if not fl:
        return {"stage": "none", "reason": f"unknown flat {fid}", "flat_id": fid}
    if int(fl.get("excluded") or 0):
        return {"stage": "none", "reason": "test/excluded flat", "flat_id": fid}

    # A flat in NET CREDIT is never chased. Auto-apply already marks a prepaid demand Paid, but a flat may
    # also hold standing on-account credit against a demand not yet raised; on balance it owes nothing, so
    # a chaser would demand money already received. Belt-and-braces on top of the per-demand status.
    try:
        if MGMT.flat_statement(fid).get("position") == "credit":
            return {"stage": "none", "flat_id": fid, "reason": "flat is in credit / paid on account"}
    except Exception:
        pass

    # An ON-TRACK payment plan is never chased. The balance is real but it is owed by an agreement the
    # directors made, and chasing it contradicts the letter the leaseholder was sent. A BROKEN plan is a
    # different matter: the agreement was what excused the balance, so it re-enters at 'formal', never at
    # the gentle 'reminder' rung.
    plan_broken = False
    try:
        from .rtm_payment_plans import plan_progress
        prog = plan_progress(fid)
    except Exception:
        prog = None
    if prog:
        if prog.get("on_track"):
            return {"stage": "none", "flat_id": fid,
                    "reason": (f"payment plan agreed and on track "
                               f"({prog['instalments_paid']} of {prog['months']} paid)")}
        plan_broken = True

    day = date.fromisoformat(today) if today else _today()
    worst = None
    for d in MGMT.list_demands(flat=fid, limit=200):
        out = int(d.get("outstanding_pence") or 0)
        settled = str(d.get("status", "")).lower() in ("paid", "cancelled", "superseded")
        # force=True is the TEST path: it bypasses the outstanding and due-date filters so a chaser can be
        # previewed against a real flat that owes nothing, exactly as send_test does for demands. It uses
        # the demand's FULL amount, because a settled demand has no outstanding balance to quote.
        if not force and (out <= 0 or settled):
            continue
        try:
            due = date.fromisoformat(str(d.get("due_date"))[:10])
        except Exception:
            continue
        if due >= day and not force:
            continue                       # not yet overdue
        overdue_days = max(1, (day - due).days)
        amount = out if out > 0 else int(d.get("amount_pence") or 0)
        if amount <= 0:
            continue
        if worst is None or overdue_days > worst["days_overdue"]:
            worst = {"document_number": d.get("document_number"), "due_date": due.isoformat(),
                     "days_overdue": overdue_days, "outstanding_pence": amount,
                     "outstanding": MGMT._money(amount), "period": d.get("period", ""),
                     "illustrative": bool(force and (out <= 0 or settled))}
    if worst is None:
        return {"stage": "none", "reason": "nothing overdue", "flat_id": fid}
    if worst["outstanding_pence"] < DE_MINIMIS_PENCE and not force:
        return {"stage": "none", "flat_id": fid,
                "reason": f"below the {MGMT._money(DE_MINIMIS_PENCE)} de-minimis floor"}

    stage = "recovery"
    for name, lo, hi in STAGES:
        if lo <= worst["days_overdue"] <= hi:
            stage = name
            break
    if plan_broken and stage == "reminder":
        stage = "formal"

    # Recovery has NO template on purpose. Forfeiture and county court each carry statutory preconditions
    # (s.168 CLRA 2002 among them) and a wrong step is far worse than sending nothing, so the system stops.
    return {
        "stage": stage, "flat_id": fid, "plan_broken": plan_broken,
        "leaseholder": fl.get("leaseholder_name", ""),
        # greeting_name is the salutation the DIRECTOR wrote ("Dr Morgan", "Alex and Sam
        # Barnes") and is used VERBATIM. Deriving from it gave "Dear Morgan"; deriving from the full
        # name gave "Dear Dr". A first-name guess is the fallback only when no salutation is on file.
        "greeting_name": (str(fl.get("greeting_name") or "").strip()
                          or TPL.first_name_from(fl.get("leaseholder_name") or "")),
        "unit": f"{fl.get('block', fid[:3])} F{fl.get('flat_number', '')}",
        "to": fl.get("leaseholder_email", ""),
        "cc": MGMT.correspondence_cc(fid),
        "payment_reference": fl.get("payment_reference", ""),
        "deadline_date": (day + timedelta(days=14)).isoformat(),
        "sendable": stage in ("reminder", "formal", "final"),
        **worst,
    }


def _human(iso: str) -> str:
    from .rtm_demand_mailing import _human_date
    return _human_date(iso)


def render_chaser(flat_id: str, *, today: str = "", test: bool = False) -> dict[str, Any]:
    """The chaser email for a flat, or a refusal explaining why it must not be chased."""
    st = overdue_stage(flat_id, today=today, force=test)
    if not st.get("sendable") and not test:
        return {"ok": False, "stage": st.get("stage"), "reason": st.get("reason"), "flat_id": st["flat_id"]}

    stage = st.get("stage") if st.get("sendable") else "reminder"
    who = st.get("greeting_name") or ""
    bank = TPL._BANK
    pay = (f"Payment is by bank transfer to {TPL.BRANDING['company']}, {bank.get('name', '')}, "
           f"sort code {bank.get('sort_code', '')}, account {bank.get('account_number', '')}, using "
           f"payment reference {st.get('payment_reference', '')}.")

    if stage == "reminder":
        d = int(st["days_overdue"])
        subject = f"Service charge for {st['unit']}, {d} day{'' if d == 1 else 's'} overdue"
        # Register matters. The first draft opened "this is very likely an oversight, so this is just a
        # nudge", which reads as written by a chatbot rather than by a company that is owed money. A
        # service charge demand is a contractual obligation, and the letter should be courteous and plain
        # without apologising for asking. Firm, short sentences; no hedging; no filler.
        body = (
            f"Dear {who},\n\n"
            f"{st['outstanding']} remains outstanding on your service charge account. Payment was due on "
            f"{_human(st['due_date'])} and has not been received.\n\n"
            f"Please arrange payment without further delay. {pay}\n\n"
            "If payment has been made in the last few days it may have crossed with this letter, in which "
            "case please disregard it. If you are unable to pay in full at present, contact us and we will "
            "discuss an instalment arrangement.\n")
    else:
        opening = ("The instalment arrangement agreed with you has not been kept to, so the full balance "
                   "is again payable."
                   if st.get("plan_broken") else
                   "A reminder has already been sent.")
        subject = f"Formal notice. Service charge arrears for {st['unit']}"
        body = (
            f"Dear {who},\n\n"
            f"The service charge of {st['outstanding']} fell due on {_human(st['due_date'])} and remains "
            f"unpaid after {st['days_overdue']} days. {opening}\n\n"
            "This sum is payable under the terms of your lease. The building's costs, including insurance, "
            "cleaning and maintenance, are met from the service charge and are shared between all "
            "leaseholders, so an unpaid account is met by your neighbours until it is settled.\n\n"
            f"Payment of {st['outstanding']} is required by {_human(st['deadline_date'])}. {pay}\n\n"
            f"If you are unable to pay in full, you must contact us before {_human(st['deadline_date'])} "
            "so that an instalment arrangement can be agreed. Arrangements have been agreed with other "
            "leaseholders and we would prefer to reach one with you.\n\n"
            "If we do not hear from you by that date, the directors will consider what further steps are "
            "open to the company to recover the debt.\n")

    body += (f"\nAny questions, just reply to this email or call {TPL.BRANDING['tel']}.\n\n"
             f"Best regards,\n\n{TPL.BRANDING['sign_name']}\n\n{TPL.BRANDING['director']}\n"
             f"{TPL.BRANDING['director_title']}, {TPL.BRANDING['company']}\n"
             f"Tel {TPL.BRANDING['tel']} · {TPL.BRANDING['email']}\n")

    if test:
        subject = "[TEST, not a real chase] " + subject
        body = ("[ TEST COPY. This is a template preview sent to the director. It is not a demand for "
                "payment and no action is required. ]\n\n") + body

    from .rtm_demand_mailing import _DASH_RE   # generated copy stays dash-free (house rule)
    return {"ok": True, "stage": stage, "flat_id": st["flat_id"], "to": st["to"], "cc": st.get("cc", []),
            "subject": _DASH_RE.sub(" ", subject), "body": _DASH_RE.sub(" ", body),
            "days_overdue": st.get("days_overdue"), "outstanding": st.get("outstanding"),
            "test": bool(test)}


def portal_text(flat_id: str, *, today: str = "") -> dict[str, Any]:
    """What the leaseholder sees on the portal. Never states more than the letter actually sent."""
    st = overdue_stage(flat_id, today=today)
    if not st.get("sendable"):
        return {"show": False, "reason": st.get("reason")}
    if st["stage"] == "reminder":
        return {"show": True, "tone": "amber",
                "heading": "Payment overdue",
                "text": (f"{st['outstanding']} was due on {_human(st['due_date'])}. If you have paid in "
                         "the last few days, thank you, it may not have reached us yet. If paying now is "
                         "difficult, contact us and we will discuss an instalment plan.")}
    return {"show": True, "tone": "red",
            "heading": "Formal notice issued",
            "text": (f"{st['outstanding']} has been outstanding since {_human(st['due_date'])}. Please pay "
                     f"by {_human(st['deadline_date'])}, or contact us to agree a plan.")}


def chaser_paragraphs(flat_id: str, *, today: str = "", test: bool = False) -> list[str]:
    """The chaser body as paragraphs for the branded letterhead. Same words as the email, because a
    leaseholder who gets both must not find them saying different things."""
    r = render_chaser(flat_id, today=today, test=test)
    if not r.get("ok"):
        return []
    body = r["body"]
    # Drop the salutation and the signature block: branded_letter supplies both from the letterhead.
    lines = body.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("Dear ")), -1) + 1
    end = next((i for i, ln in enumerate(lines) if ln.startswith("Best regards")), len(lines))
    paras = [p.strip() for p in "\n".join(lines[start:end]).split("\n\n") if p.strip()]
    # A POSTED letter cannot be replied to. The email says "just reply to this email"; on paper that reads
    # as though the reader has missed something, so the contact line is rewritten for the channel.
    # A POSTED letter cannot be replied to. Every phrase that assumes an inbox is rewritten for paper,
    # not just the sign-off: "please reply and tell us" reads as a missing attachment on a printed page.
    out = []
    for p in paras:
        if p.lower().startswith("any questions"):
            p = ("Any questions, please call " + TPL.BRANDING["tel"] + " or email "
                 + TPL.BRANDING["email"] + ".")
        p = p.replace("please reply and tell us",
                      "please call or email us and tell us")
        p = p.replace("If you have already paid in the last few days, thank you, and please ignore this.",
                      "If you have already paid in the last few days, thank you, and please ignore this "
                      "letter, which will have crossed with your payment.")
        out.append(p)
    return out


def render_chaser_docx(flat_id: str, out_path: str, *, today: str = "",
                       deliver_to_premises: bool = False, test: bool = False) -> dict[str, Any]:
    """The chaser as a branded .docx, ready for PDF conversion and posting."""
    from datetime import date as _d
    from .rtm_templates import render_docx, split_address, premises as _prem
    st = overdue_stage(flat_id, today=today, force=test)
    fl = MGMT._get_flat(flat_id) or {}
    paras = chaser_paragraphs(flat_id, today=today, test=test)
    if not paras:
        return {"ok": False, "error": st.get("reason") or "nothing to chase"}
    block = str(fl.get("block") or flat_id[:3])
    num = fl.get("flat_number")
    if deliver_to_premises:
        a1, a2 = _prem(block, num), ""
    else:
        a1, a2 = split_address(str(fl.get("correspondence_address") or ""))
    ctx = {
        "letter_date": _d.today().strftime("%d %B %Y").lstrip("0"),
        "leaseholder_name": fl.get("leaseholder_name", ""),
        "addr_line_1": a1 or _prem(block, num), "addr_line_2": a2,
        "greeting_name": (str(fl.get("greeting_name") or "").strip()
                          or TPL.first_name_from(fl.get("leaseholder_name") or "")),
        "sign_off": "Best regards",
        # PLAIN STRINGS. The template renders each item directly, so wrapping them in a dict
        # printed "{'text': ...}" into the body of a letter meant for a leaseholder.
        # RED BOLD on the two facts that carry the letter: the sum owed and the date by which it must be
        # paid. Deliberately narrow. Colouring whole paragraphs red makes a letter look like a scam and
        # makes nothing stand out, because emphasis everywhere is emphasis nowhere.
        "paragraphs": _emphasise(paras, [str(st.get("outstanding") or ""),
                                         _human(str(st.get("deadline_date") or ""))]),
    }
    return render_docx("overdue_notice", ctx, out_path=out_path, validate=False)


# Body size for the chaser, in HALF-POINTS (docxtpl/OOXML convention). The letterhead body is 12pt
# (w:sz 24); an overdue notice is set 3pt larger so it reads as a more serious document than the routine
# demand it follows, and so it stays legible when a leaseholder photographs it rather than reading it.
CHASER_BODY_HALF_POINTS = 30   # 15pt


def _emphasise(paragraphs: list[str], terms: list[str], *,
               size: int = CHASER_BODY_HALF_POINTS) -> list:
    """Return paragraphs with `terms` rendered red and bold, as docxtpl RichText.

    Plain strings render as plain text, so a paragraph containing no term is left as a string and only the
    ones that matter become RichText. Falls back to the plain strings entirely if docxtpl is unavailable,
    because a letter with no emphasis is still a letter, whereas a crash is not.
    """
    try:
        from docxtpl import RichText
    except Exception:
        return list(paragraphs)
    terms = [t for t in terms if t and len(t) > 2]
    out: list = []
    for para in paragraphs:
        hits = [t for t in terms if t in para]
        if not hits:
            # EVERY paragraph must be RichText: the overdue template renders {{r para }}, and a bare
            # string there is inserted as raw XML, so an "&" in a name would corrupt the document.
            out.append(RichText(para, size=size))
            continue
        rt = RichText()
        rest = para
        while rest:
            # Take the earliest occurrence of any term, so overlapping terms cannot double-render.
            nxt = min(((rest.find(t), t) for t in terms if t in rest), default=None)
            if nxt is None:
                rt.add(rest, size=size)
                break
            i, term = nxt
            if i > 0:
                rt.add(rest[:i], size=size)
            rt.add(term, bold=True, color="B3403A", size=size)
            rest = rest[i + len(term):]
        out.append(rt)
    return out


def _addresses_differ(flat_id: str) -> bool:
    """Does the leaseholder receive post somewhere other than the flat? Half the building does, so a
    second demand addressed to the premises is a real need, not a nicety: the owner posts one copy to the
    correspondence address and puts the other through the flat's own door."""
    import re
    from .rtm_templates import premises as _prem
    fl = MGMT._get_flat(flat_id) or {}
    corr = str(fl.get("correspondence_address") or "").strip()
    if not corr:
        return False
    prem = _prem(str(fl.get("block") or flat_id[:3]), fl.get("flat_number"))
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())   # noqa: E731
    return norm(corr) != norm(prem)


def _period_context(period: str, issue_date: str) -> dict[str, str]:
    """Turn a demand's period token (e.g. '2027-H1') and issue date into the wording the demand letter
    needs: the BUDGET year for the apportionment lookup, the half-year date range, and the demand date.
    Half-years: H1 = 1 January to 30 June of that year; H2 = 1 July to 31 December of that year. The budget
    year label is the financial year the half sits in (H2 of YYYY and H1 of YYYY+1 share budget year
    YYYY+1), matching how budget lines are stored."""
    import re as _re
    from datetime import date as _date
    m = _re.match(r"^(\d{4})-H([12])$", (period or "").strip())
    if not m:
        # Unknown shape: fall back to the current-budget wording rather than guessing.
        return {"year": "2027", "period_label": "1 July 2026 to 31 December 2026", "demand_date": "16 July 2026"}
    yr, half = int(m.group(1)), int(m.group(2))
    if half == 1:
        period_label = f"1 January {yr} to 30 June {yr}"
        budget_year = str(yr)
    else:
        period_label = f"1 July {yr} to 31 December {yr}"
        budget_year = str(yr + 1)
    demand_date = period  # sensible fallback if the issue date does not parse
    try:
        d = _date.fromisoformat((issue_date or "")[:10])
        demand_date = f"{d.day} {d.strftime('%B %Y')}"
    except Exception:
        pass
    return {"year": budget_year, "period_label": period_label, "demand_date": demand_date}


def document_pack(flat_id: str, *, out_dir: str, today: str = "", test: bool = False,
                  period: str = "") -> dict[str, Any]:
    """Every postable document for one flat, as PDFs, ready for Docmail.

    Always: the demand as addressed on file. Additionally, when the leaseholder lives elsewhere, the SAME
    demand re-addressed to the flat itself, so a copy can go through the door without printing the
    leaseholder's home address on something left in a shared mailbox area. Plus the chaser, when one is due.
    """
    from pathlib import Path
    from .rtm_apportionment_docs import render_demand_docx
    from .rtm_templates import to_pdf
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fl = MGMT._get_flat(flat_id) or {}
    if not fl:
        return {"ok": False, "error": f"unknown flat {flat_id}"}
    _dems = MGMT.list_demands(flat=flat_id, limit=20)
    if period:
        dem = next((d for d in _dems if str(d.get("period", "")) == period), None)
    else:
        dem = next((d for d in _dems), None)   # newest (list_demands is newest-first)
    docs: list[dict[str, Any]] = []

    def _emit(kind: str, label: str, docx_path: Path, res: dict) -> None:
        if not res.get("ok"):
            docs.append({"kind": kind, "label": label, "ok": False, "error": res.get("error")})
            return
        pdf = to_pdf(str(docx_path))
        ok = bool(pdf.get("ok"))
        docs.append({"kind": kind, "label": label, "ok": ok,
                     "path": pdf.get("path") if ok else str(docx_path),
                     "error": None if ok else pdf.get("error")})

    if dem:
        _pc = _period_context(str(dem.get("period", "")), str(dem.get("issue_date", "")))
        common = dict(flat_id=flat_id, year=_pc["year"], period_label=_pc["period_label"],
                      demand_date=_pc["demand_date"], due_date=str(dem.get("due_date") or ""),
                      doc_number=str(dem.get("document_number") or ""))
        p1 = out / f"{flat_id}_demand_posted.docx"
        _emit("demand_posted", "Demand, as addressed on file",
              p1, render_demand_docx(out_path=str(p1), **common))
        if _addresses_differ(flat_id):
            p2 = out / f"{flat_id}_demand_premises.docx"
            _emit("demand_premises", "Demand, re-addressed to the flat",
                  p2, render_demand_docx(out_path=str(p2), deliver_to_premises=True, **common))

    st = overdue_stage(flat_id, today=today, force=test)
    if st.get("sendable") or test:
        stage = st.get("stage", "reminder")
        p3 = out / f"{flat_id}_chaser.docx"
        _emit("chaser", f"Chaser letter ({stage})",
              p3, render_chaser_docx(flat_id, str(p3), today=today, test=test))
        # A chaser needs BOTH copies for the same reason a demand does: the leaseholder is chased at the
        # address they actually read post at, and a second copy goes through the flat's own door in case
        # they are not reading it. Only generated when the two addresses genuinely differ.
        if _addresses_differ(flat_id):
            p4 = out / f"{flat_id}_chaser_premises.docx"
            _emit("chaser_premises", f"Chaser letter, re-addressed to the flat ({stage})",
                  p4, render_chaser_docx(flat_id, str(p4), today=today, test=test,
                                         deliver_to_premises=True))
    return {"ok": True, "flat_id": flat_id, "addresses_differ": _addresses_differ(flat_id),
            "documents": docs}


# Bumped whenever the LETTER changes: wording, layout, sizing, or which variants are produced. Combined
# with the template file hashes below so an edit to the .docx invalidates the cache without anyone
# remembering to touch this number.
COPY_VERSION = "2026-08-03.chaser-formal-15pt-both-addresses"


def _document_build_id() -> str:
    """A short id for the CURRENT document recipe: the copy version plus the templates it renders from."""
    import hashlib as _h
    from .rtm_templates import _docx_source
    hasher = _h.sha256(COPY_VERSION.encode())
    for name in ("overdue_notice.docx", "service_charge_demand.docx"):
        try:
            hasher.update(_docx_source(name).read_bytes())
        except Exception:
            hasher.update(b"missing")
    return hasher.hexdigest()[:12]


def pack_fingerprint(flat_id: str, *, today: str = "") -> str:
    """What a flat's postal pack DEPENDS on. If this is unchanged, regenerating the PDFs would
    produce identical bytes, so the nightly job can skip the LibreOffice conversion entirely.
    Covers the balance, the demand, the overdue stage and the addressing, because a change in any
    of those changes what the letter says or who it is addressed to."""
    import hashlib as _h
    # The fingerprint MUST cover what the letter SAYS, not only who it is about. Without this, rewriting
    # the copy, changing the body size and adding the premises variant all left the fingerprint identical,
    # so the nightly job skipped every flat and the plugin kept serving the previous letter. A caller
    # downloaded a chaser that had been replaced hours earlier and had no way to tell.
    fl = MGMT._get_flat(flat_id) or {}
    st = overdue_stage(flat_id, today=today)
    dem = next((d for d in MGMT.list_demands(flat=flat_id, limit=5)), {}) or {}
    parts = [flat_id, str(fl.get("leaseholder_name")), str(fl.get("greeting_name")),
             str(fl.get("correspondence_address")), str(fl.get("payment_reference")),
             str(dem.get("document_number")), str(dem.get("outstanding_pence")),
             str(dem.get("status")), str(st.get("stage")), str(st.get("days_overdue")),
             _document_build_id()]
    return _h.sha256("|".join(parts).encode()).hexdigest()[:24]


def publish_packs(*, out_dir: str = "", today: str = "", limit: int = 50,
                  only_changed: bool = False) -> dict[str, Any]:
    """Generate every flat's postal pack and push the PDFs into the WordPress store.

    WordPress cannot pull from Mongoose (CGNAT), so the buttons on the admin screen can only be instant
    downloads if the bytes are already there. Each variant gets its own document_number suffix so the
    store's (pdf_key, document_number, flat_id) key stays unique and one variant can never serve another's
    bytes:  <doc>#posted, <doc>#premises, <flat>#chaser
    """
    import base64 as _b64
    import hashlib as _hash
    from pathlib import Path
    from .rtm_portal import _wp_call
    out = Path(out_dir or (Path.home() / ".mongoose/data/portal/packs"))
    _document_build_id()
    pushed, skipped, errors = 0, 0, []
    manifest: list[dict[str, Any]] = []
    from ..db import get_app_state, set_app_state
    unchanged = 0
    for f in MGMT.list_flats()[:max(1, limit)]:
        fid = f["flat_id"]
        fp = pack_fingerprint(fid, today=today)
        key = f"rtm_pack_fp:{fid}"
        if only_changed and get_app_state(key, "") == fp:
            # Nothing this pack depends on has moved, so the PDFs would be byte-identical. Skipping saves
            # a LibreOffice conversion per document, which is the expensive part of this job.
            unchanged += 1
            continue
        pack = document_pack(fid, out_dir=str(out / fid), today=today)
        for d in pack.get("documents", []):
            if not d.get("ok") or not str(d.get("path", "")).endswith(".pdf"):
                skipped += 1
                continue
            raw = Path(d["path"]).read_bytes()
            sha = _hash.sha256(raw).hexdigest()
            dem = next((x for x in MGMT.list_demands(flat=fid, limit=5)), {}) or {}
            base = str(dem.get("document_number") or fid)
            # Each variant needs its OWN document_number, or the store's unique key collapses two
            # different letters onto one row and the wrong one is served.
            # A PLAIN, STABLE key. I briefly stamped the recipe id into it (<name>#<variant>@<id>) so a
            # stale document would be unfindable. That broke every download button in the installed
            # plugin, which parses the plain form, and it was solving the wrong problem anyway: freshness
            # comes from the store returning the NEWEST row, not from hiding older ones. Content changes
            # produce a new sha, hence a new row with a higher id, hence a newer answer. Changing a key
            # format that another system parses is a breaking change and must not be done casually.
            variant = d["kind"].replace("demand_", "") if d["kind"].startswith("demand") else d["kind"]
            stem = base if d["kind"].startswith("demand") else fid
            docno = f"{stem}#{variant}"
            res = _wp_call("POST", _profile.wp_route("sync/demand-pdf"), timeout=60.0, json_body={
                "pdf_key": sha[:43], "document_number": docno, "flat_id": fid,
                "content_b64": _b64.b64encode(raw).decode("ascii"), "sha256": sha})
            if res.get("ok"):
                pushed += 1
                manifest.append({"flat_id": fid, "kind": d["kind"], "label": d["label"],
                                 "pdf_key": sha[:43], "document_number": docno})
            else:
                errors.append(f"{fid}/{d['kind']}: {res.get('error')}")
        if not errors:
            set_app_state(key, fp)
    return {"ok": not errors, "pushed": pushed, "skipped": skipped, "unchanged": unchanged,
            "errors": errors[:6], "manifest": manifest}


# ── Docmail postal batch ─────────────────────────────────────────────────────
# Docmail prints from ONE multi-page PDF plus an address list, matching them IN ORDER: letter 1 goes to
# row 1. So the merge and the CSV must be produced together, from the same list, or every letter after a
# mismatch goes to the wrong person. That is the failure this function exists to make impossible.

def _address_lines(flat_id: str, *, premises: bool) -> list[str]:
    """The recipient's postal address as separate lines, for the address file."""
    from .rtm_templates import premises as _prem
    fl = MGMT._get_flat(flat_id) or {}
    if premises:
        whole = _prem(str(fl.get("block") or flat_id[:3]), fl.get("flat_number"))
    else:
        whole = str(fl.get("correspondence_address") or "") or _prem(
            str(fl.get("block") or flat_id[:3]), fl.get("flat_number"))
    parts = [p.strip() for p in str(whole).split(",") if p.strip()]
    return parts


def _split_postcode(parts: list[str]) -> tuple[list[str], str]:
    """Separate a UK postcode off the last address line; Docmail wants it in its own column."""
    import re as _re
    if not parts:
        return [], ""
    pc = _re.search(r"([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\s*$", parts[-1].upper())
    if not pc:
        return parts, ""
    code = pc.group(1)
    tail = parts[-1][: pc.start()].strip().rstrip(",")
    return ([*parts[:-1], tail] if tail else parts[:-1]), code


def postal_batch(flats: "list[str]", *, out_dir: str, today: str = "", kind: str = "chaser",
                 both_addresses: bool = True) -> dict[str, Any]:
    """One merged PDF plus a matching address CSV, ready to upload to Docmail.

    `both_addresses` adds the premises copy for any leaseholder who lives elsewhere, so the same letter
    goes to where they read post AND through the flat's own door. Each copy is its own letter and its own
    row, in the same order as the pages.
    """
    import csv as _csv
    from pathlib import Path
    from pypdf import PdfWriter, PdfReader
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    for fid in flats:
        pack = document_pack(fid, out_dir=str(out / "_src" / fid), today=today)
        by_kind = {d["kind"]: d for d in pack.get("documents", []) if d.get("ok")}
        wanted = [kind] + ([f"{kind}_premises"] if both_addresses else [])
        for k in wanted:
            doc = by_kind.get(k)
            if not doc:
                if k == kind:
                    skipped.append(f"{fid}: no {kind}")
                continue
            reader = PdfReader(doc["path"])
            pages = len(reader.pages)
            for pg in reader.pages:
                writer.add_page(pg)
            fl = MGMT._get_flat(fid) or {}
            lines, postcode = _split_postcode(_address_lines(fid, premises=k.endswith("_premises")))
            lines = (lines + ["", "", "", "", ""])[:5]
            rows.append({
                "Name": fl.get("leaseholder_name", ""),
                "Address 1": lines[0], "Address 2": lines[1], "Address 3": lines[2],
                "Address 4": lines[3], "Address 5": lines[4], "Postcode": postcode,
                "Pages": pages, "Reference": f"{fid}-{k}",
            })
    pdf_path = out / f"docmail_{kind}_batch.pdf"
    with pdf_path.open("wb") as fh:
        writer.write(fh)
    csv_path = out / f"docmail_{kind}_addresses.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=["Name", "Address 1", "Address 2", "Address 3", "Address 4",
                                            "Address 5", "Postcode", "Pages", "Reference"])
        w.writeheader()
        w.writerows(rows)
    # Every letter must have exactly one address row, or Docmail silently posts to the wrong people.
    total_pages = sum(int(r["Pages"]) for r in rows)
    return {"ok": not skipped, "pdf": str(pdf_path), "csv": str(csv_path),
            "letters": len(rows), "pages": total_pages, "skipped": skipped,
            "uniform_pages": len({int(r["Pages"]) for r in rows}) <= 1}


# ── Postal runs ──────────────────────────────────────────────────────────────
# A demand, a receipt and a chaser are the same object: a document, for a flat, at an address. So there is
# ONE postal run rather than three mailing features, and a single letter is a run of one. That matters
# beyond tidiness: a separate "quick send" path is a path that skips the checks the batch does.

RUN_KINDS = ("demand", "chaser", "receipt")


def _ensure_run_tables() -> None:
    from ..db import connect
    with connect() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS rtm_postal_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                flat_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                address_variant TEXT NOT NULL,
                pdf_sha256 TEXT NOT NULL DEFAULT '',
                pages INTEGER NOT NULL DEFAULT 1,
                built_at TEXT NOT NULL,
                posted_at TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT ''
            )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_rtm_postal_flat "
                    "ON rtm_postal_runs(flat_id, kind, built_at)")
        con.commit()


def _receipt_pdf_for(flat_id: str) -> "tuple[str, str] | None":
    """The newest receipt PDF for a flat, as (path, sha). Receipts already exist on disk from the
    receipt pack, so a postal run reuses them rather than re-rendering and risking a different document
    from the one already emailed."""
    from ..db import connect
    from pathlib import Path
    with connect() as con:
        r = con.execute(
            "SELECT pdf_storage_path, pdf_sha256 FROM portal_receipt_pdfs WHERE flat_id=? "
            "ORDER BY created_at DESC LIMIT 1", (flat_id,)).fetchone()
    if not r or not r["pdf_storage_path"]:
        return None
    p = Path(str(r["pdf_storage_path"])).expanduser()
    return (str(p), str(r["pdf_sha256"] or "")) if p.exists() else None


def run_candidates(kind: str, flats: "list[str] | None" = None, *, today: str = "", period: str = "") -> dict[str, Any]:
    """Who a run of this kind WOULD go to, and who is excluded and why, WITHOUT building anything.

    The exclusions are shown before you commit, using the same resolvers the rest of the system uses.
    Re-running this at build time is what stops a basket assembled this morning posting a chaser to
    someone who paid at lunchtime.
    """
    kind = (kind or "").strip().lower()
    if kind not in RUN_KINDS:
        return {"ok": False, "error": f"kind must be one of {', '.join(RUN_KINDS)}"}
    only = {f.strip().upper() for f in (flats or [])} or None
    include: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for f in MGMT.list_flats():
        fid = f["flat_id"]
        if only is not None and fid not in only:
            continue
        fl = MGMT._get_flat(fid) or {}
        name = str(fl.get("leaseholder_name") or "")
        if not str(fl.get("correspondence_address") or "").strip() and not _addresses_differ(fid):
            pass  # a blank correspondence address falls back to the premises, which is still postable
        if kind == "chaser":
            st = overdue_stage(fid, today=today)
            if not st.get("sendable"):
                excluded.append({"flat_id": fid, "name": name, "reason": st.get("reason", "not overdue")})
                continue
            include.append({"flat_id": fid, "name": name, "detail": f"{st['days_overdue']}d, {st['outstanding']}"})
        elif kind == "demand":
            _dl = MGMT.list_demands(flat=fid, limit=20)
            if period:
                dem = next((d for d in _dl if str(d.get("period", "")) == period), None)
                if not dem:
                    excluded.append({"flat_id": fid, "name": name, "reason": f"no {period} demand"})
                    continue
            else:
                dem = next((d for d in _dl), None)
                if not dem:
                    excluded.append({"flat_id": fid, "name": name, "reason": "no demand raised"})
                    continue
            include.append({"flat_id": fid, "name": name, "detail": str(dem.get("document_number"))})
        else:
            rp = _receipt_pdf_for(fid)
            if not rp:
                excluded.append({"flat_id": fid, "name": name, "reason": "no receipt on file"})
                continue
            include.append({"flat_id": fid, "name": name, "detail": "latest receipt"})
    return {"ok": True, "kind": kind, "include": include, "excluded": excluded,
            "count": len(include), "excluded_count": len(excluded)}


def postal_run(kind: str, flats: "list[str] | None" = None, *, out_dir: str = "", today: str = "",
               both_addresses: bool = True, run_id: str = "", dry_run: bool = True, period: str = "") -> dict[str, Any]:
    """Build one postal run: a merged PDF, a matching address CSV, and an audit row per letter.

    dry_run=True (the default) resolves and reports without writing files or logging, so the exclusions
    can be reviewed before anything is produced.

    THE ORDERING INVARIANT: Docmail matches letters to addresses BY POSITION, so page 1 goes to row 1.
    The PDF and the CSV are therefore produced by this one function from one list and never separately.
    Generating them apart is how a leaseholder receives someone else's debt letter.

    UNIFORM PAGE COUNT: Docmail splits a merged PDF by a fixed pages-per-letter. A 1-page chaser mixed
    with a 4-page demand cannot be one upload, so a mixed run is REFUSED rather than silently mis-split.
    """
    import csv as _csv
    from datetime import datetime as _dt
    from pathlib import Path
    from ..db import connect, utc_now_iso
    from pypdf import PdfReader, PdfWriter

    cand = run_candidates(kind, flats, today=today, period=period)
    if not cand.get("ok"):
        return cand
    kind = cand["kind"]
    targets = [c["flat_id"] for c in cand["include"]]
    if not targets:
        return {"ok": False, "error": f"nothing to post: no flat qualifies for a {kind} run",
                "excluded": cand["excluded"]}
    if dry_run:
        return {"ok": True, "dry_run": True, "kind": kind, "letters_estimate":
                len(targets) + (sum(1 for f in targets if _addresses_differ(f)) if both_addresses else 0),
                "include": cand["include"], "excluded": cand["excluded"]}

    run_id = run_id or f"{kind}-{_dt.now().strftime('%Y%m%d-%H%M%S')}"
    out = Path(out_dir or (Path.home() / ".mongoose/data/portal/postal" / run_id))
    out.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    rows: list[dict[str, Any]] = []
    page_counts: set[int] = set()
    problems: list[str] = []

    for fid in targets:
        variants: list[tuple[str, str]] = []      # (address_variant, pdf path)
        if kind == "receipt":
            rp = _receipt_pdf_for(fid)
            if rp:
                variants.append(("posted", rp[0]))
        else:
            pack = document_pack(fid, out_dir=str(out / "_src" / fid), today=today, period=period)
            by = {d["kind"]: d for d in pack.get("documents", []) if d.get("ok")}
            base = "demand_posted" if kind == "demand" else "chaser"
            prem = "demand_premises" if kind == "demand" else "chaser_premises"
            if by.get(base):
                variants.append(("posted", by[base]["path"]))
            if both_addresses and by.get(prem):
                variants.append(("premises", by[prem]["path"]))
        if not variants:
            problems.append(f"{fid}: {kind} could not be produced")
            continue
        fl = MGMT._get_flat(fid) or {}
        for variant, path in variants:
            reader = PdfReader(path)
            n = len(reader.pages)
            page_counts.add(n)
            for pg in reader.pages:
                writer.add_page(pg)
            lines, postcode = _split_postcode(_address_lines(fid, premises=(variant == "premises")))
            lines = (lines + ["", "", "", "", ""])[:5]
            rows.append({"Name": fl.get("leaseholder_name", ""),
                         "Address 1": lines[0], "Address 2": lines[1], "Address 3": lines[2],
                         "Address 4": lines[3], "Address 5": lines[4], "Postcode": postcode,
                         "Pages": n, "Reference": f"{fid}-{kind}-{variant}"})

    if len(page_counts) > 1:
        return {"ok": False, "error": (f"mixed page counts {sorted(page_counts)} in one run. Docmail "
                                       "splits by a fixed pages-per-letter, so this would mis-split. "
                                       "Build one run per document kind."), "letters": len(rows)}

    pdf_path = out / f"docmail_{kind}_batch.pdf"
    with pdf_path.open("wb") as fh:
        writer.write(fh)
    csv_path = out / f"docmail_{kind}_addresses.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=["Name", "Address 1", "Address 2", "Address 3", "Address 4",
                                            "Address 5", "Postcode", "Pages", "Reference"])
        w.writeheader()
        w.writerows(rows)

    _ensure_run_tables()
    now = utc_now_iso()
    with connect() as con:
        for r in rows:
            fid, _, variant = str(r["Reference"]).rsplit("-", 2)
            con.execute(
                "INSERT INTO rtm_postal_runs(run_id,flat_id,kind,address_variant,pages,built_at) "
                "VALUES(?,?,?,?,?,?)", (run_id, fid, kind, variant, int(r["Pages"]), now))
        con.commit()
    return {"ok": not problems, "run_id": run_id, "kind": kind,
            "pdf": str(pdf_path), "csv": str(csv_path),
            "letters": len(rows), "pages_per_letter": (page_counts.pop() if page_counts else 0),
            "excluded": cand["excluded"], "problems": problems}


def postal_history(flat_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """What has actually been posted, so a flat card can say "last posted: chaser, 3 Aug, to Compton".
    This is the evidence if a leaseholder says they never received a demand, and it is what stops the
    same chaser going out twice."""
    _ensure_run_tables()
    from ..db import connect
    q = ("SELECT run_id, flat_id, kind, address_variant, pages, built_at, posted_at "
         "FROM rtm_postal_runs {} ORDER BY built_at DESC LIMIT ?")
    with connect() as con:
        if flat_id:
            rows = con.execute(q.format("WHERE flat_id=?"), (flat_id.strip().upper(), limit)).fetchall()
        else:
            rows = con.execute(q.format(""), (limit,)).fetchall()
    return [dict(r) for r in rows]


_BUILD_ID_KEY = "rtm_documents_build_id"


def documents_stale() -> dict[str, Any]:
    """Do the published documents predate the current letter recipe? CHEAP: compares one id, renders
    nothing. Safe to call every few minutes.

    Staleness here is invisible and dangerous: the letters still open, still look right, and are simply
    the previous version. A director downloads what they believe is today's chaser and posts last week's
    wording. So it must self-heal rather than wait for someone to notice.
    """
    from ..db import get_app_state
    current = _document_build_id()
    published = get_app_state(_BUILD_ID_KEY, "") or ""
    return {"stale": published != current, "current": current, "published": published or "(never)"}


def refresh_documents_if_stale(*, force: bool = False) -> dict[str, Any]:
    """Republish every pack when the letter recipe has changed (or on demand). Idempotent and quiet when
    nothing has moved: the per-flat fingerprints still skip flats whose own data is unchanged, so a
    recipe bump rebuilds the documents without re-rendering anything else."""
    from ..db import set_app_state
    state = documents_stale()
    if not state["stale"] and not force:
        return {"ok": True, "rebuilt": False, "reason": "documents match the current recipe",
                "build_id": state["current"]}
    res = publish_packs(only_changed=not force)
    if res.get("ok"):
        set_app_state(_BUILD_ID_KEY, state["current"])
    return {"ok": bool(res.get("ok")), "rebuilt": True, "pushed": res.get("pushed", 0),
            "build_id": state["current"], "was": state["published"], "errors": res.get("errors", [])[:3]}
