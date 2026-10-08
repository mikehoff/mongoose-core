"""Seller-Pack Responder, the OUTPUT side (Stage 1 of a two-stage response).

The spec's response is deliberately two-stage, matching how a careful RTM director actually replies to an
LPE1 request BEFORE any money has changed hands:

  Stage 1 (this module), issued on receipt of the request, BEFORE the fee:
    - a branded cover letter that names the fee, the bank details, what will be released on payment, the
      notice-of-assignment / deed-of-covenant position, and membership; and
    - the statutory Administration Charges summary of rights (Schedule 11 to the Commonhold and Leasehold
      Reform Act 2002, prescribed by the Administration Charges (Summary of Rights and Obligations)
      (England) Regulations 2007), which BY LAW must accompany any demand for an administration charge.

  Stage 2 (rtm_seller_pack.build_draft + the official-PDF fill, a later slice): the completed Form LPE1
    and the enclosures, released only once `stage == 'paid'`.

Design rules carried from the field engine: nothing is invented; the money/identity facts are read from
the single company source (rtm_templates.BRANDING / company_profile) and the company/lease registers, so
the letter can never disagree with a demand; the buildings-insurance position is never warranted here (it
is a Stage-2 reply); and issue is gated on the director (human sign-off is mandatory before anything goes
out). The cover letter renders through the ONE branded template (branded_letter.docx, real letterhead in
the data dir); the statutory summary renders as its own branded enclosure.
"""
from __future__ import annotations

import datetime
from typing import Any

from .. import profile as _profile
from ..db import connect, utc_now_iso
from . import rtm_templates as TPL
from .rtm_seller_pack import FEE_GBP, FORM

# The statutory management-scope split (director-confirmed, 13 Sep 2026): the RTM manages the building and
# undercroft only; estate services remain with the former manager. Read from the company register so a
# change of arrangement updates every future letter.
RTM_ACQUISITION_HUMAN = "16 July 2026"


# --------------------------------------------------------------------------- job store
def _ensure(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS rtm_seller_pack_jobs (
        job_ref TEXT PRIMARY KEY, flat_id TEXT, seller_name TEXT, conveyancer_firm TEXT,
        conveyancer_contact TEXT, conveyancer_email TEXT, conveyancer_address TEXT, their_ref TEXT,
        request_date TEXT, fee_pence INTEGER, stage TEXT, notes TEXT, created_at TEXT, updated_at TEXT)""")


def our_ref(flat_id: str) -> str:
    """Deterministic Company reference for the matter, e.g. 1ECF7 -> EC/1EC/F7/LPE1 (the prefix is the
    site profile's payment reference prefix)."""
    fid = (flat_id or "").strip().upper().replace("-", "").replace(" ", "")
    block, _, flatn = fid.partition("F")
    return f"{_profile.payment_ref_prefix()}/{block[:3]}/F{flatn}/LPE1"


def _company_name() -> str:
    """'<RTM name> RTM Company Limited', the RTM name from the site profile."""
    return f"{_profile.rtm_name()} RTM Company Limited"


def _blocks_phrase() -> str:
    """The buildings the RTM manages, e.g. '1 and 3 Example Court': the block numbers joined, then the
    street when every block shares it (else each block's full address)."""
    bl = _profile.blocks()
    streets = {b["street"] for b in bl}
    if len(streets) == 1:
        nums = [b["number"] for b in bl]
        head = ", ".join(nums[:-1]) + " and " + nums[-1] if len(nums) > 1 else nums[0]
        return f"{head} {bl[0]['street']}"
    addrs = [_profile.block_address(b["code"]) for b in bl]
    return ", ".join(addrs[:-1]) + " and " + addrs[-1] if len(addrs) > 1 else addrs[0]


def _estate_clause() -> str:
    """Who keeps the estate services after RTM (lexicon "rtm.estate_manager_clause"; fictional default)."""
    return str(_profile.lexicon("rtm.estate_manager_clause",
                                "Example Estate Management Limited, acting through its agent Example Agents Ltd"))


def set_job(flat_id: str, **fields: Any) -> dict[str, Any]:
    """Create or update the seller-pack job for a flat. job_ref defaults to our_ref(flat_id). Stage is one
    of new | awaiting_fee | paid | released. Only the fields passed are changed."""
    fid = (flat_id or "").strip().upper().replace("-", "").replace(" ", "")
    if not fid:
        return {"ok": False, "error": "flat_id required"}
    ref = fields.get("job_ref") or our_ref(fid)
    cols = ("seller_name", "conveyancer_firm", "conveyancer_contact", "conveyancer_email",
            "conveyancer_address", "their_ref", "request_date", "fee_pence", "stage", "notes")
    with connect() as con:
        _ensure(con)
        row = con.execute("SELECT * FROM rtm_seller_pack_jobs WHERE job_ref=?", (ref,)).fetchone()
        cur = dict(row) if row else {}
        cur["flat_id"] = fid
        for k in cols:
            if k in fields:
                cur[k] = fields[k]
        if not cur.get("stage"):
            cur["stage"] = "new"
        if cur.get("fee_pence") is None:
            cur["fee_pence"] = FEE_GBP * 100
        con.execute(
            """INSERT OR REPLACE INTO rtm_seller_pack_jobs
               (job_ref,flat_id,seller_name,conveyancer_firm,conveyancer_contact,conveyancer_email,
                conveyancer_address,their_ref,request_date,fee_pence,stage,notes,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ref, fid, cur.get("seller_name"), cur.get("conveyancer_firm"), cur.get("conveyancer_contact"),
             cur.get("conveyancer_email"), cur.get("conveyancer_address"), cur.get("their_ref"),
             cur.get("request_date"), cur.get("fee_pence"), cur.get("stage"), cur.get("notes"),
             (dict(row).get("created_at") if row else utc_now_iso()), utc_now_iso()))
    return {"ok": True, "job_ref": ref, **{k: cur.get(k) for k in cols}, "flat_id": fid}


def get_job(flat_or_ref: str) -> dict[str, Any] | None:
    key = (flat_or_ref or "").strip()
    fid = key.upper().replace("-", "").replace(" ", "")
    with connect() as con:
        _ensure(con)
        row = con.execute("SELECT * FROM rtm_seller_pack_jobs WHERE job_ref=? OR flat_id=?",
                          (key, fid)).fetchone()
        return dict(row) if row else None


def list_jobs() -> list[dict[str, Any]]:
    with connect() as con:
        _ensure(con)
        return [dict(r) for r in con.execute(
            "SELECT * FROM rtm_seller_pack_jobs ORDER BY updated_at DESC").fetchall()]


# --------------------------------------------------------------------------- statutory summary (verbatim)
# Prescribed by the Administration Charges (Summary of Rights and Obligations) (England) Regulations 2007
# (Schedule 11, Commonhold and Leasehold Reform Act 2002). This is VERBATIM statutory text and must never
# be edited or reworded, exactly like the s.21B service-charge summary baked into the demand. The dashes
# and bullet glyphs below are the SI's own and are the one permitted place for such characters here.
SCHEDULE_11_TITLE = "Administration Charges: Summary of tenants' rights and obligations"

SCHEDULE_11_PARAS: list[dict[str, Any]] = [
    {"kind": "intro",
     "text": "The wording below is the summary prescribed by the Administration Charges (Summary of "
             "Rights and Obligations) (England) Regulations 2007, which by law must accompany a demand "
             "for an administration charge."},
    {"kind": "num", "text": "(1) This summary, which briefly sets out your rights and obligations in "
        "relation to administration charges, must by law accompany a demand for administration charges. "
        "Unless a summary is sent to you with a demand, you may withhold the administration charge. The "
        "summary does not give a full interpretation of the law and if you are in any doubt about your "
        "rights and obligations you should seek independent advice."},
    {"kind": "num", "text": "(2) An administration charge is an amount which may be payable by you as part "
        "of or in addition to the rent directly or indirectly—"},
    {"kind": "bullet", "text": "for or in connection with the grant of an approval under your lease, or an "
        "application for such approval;"},
    {"kind": "bullet", "text": "for or in connection with the provision of information or documents;"},
    {"kind": "bullet", "text": "in respect of your failure to make any payment due under your lease; or"},
    {"kind": "bullet", "text": "in connection with a breach of a covenant or condition of your lease."},
    {"kind": "cont", "text": "If you are liable to pay an administration charge, it is payable only to the "
        "extent that the amount is reasonable."},
    {"kind": "num", "text": "(3) Any provision contained in a grant of a lease under the right to buy under "
        "the Housing Act 1985, which claims to allow the landlord to charge a sum for consent or approval, "
        "is void."},
    {"kind": "num", "text": "(4) You have the right to ask a leasehold valuation tribunal whether an "
        "administration charge is payable. You may make a request before or after you have paid the "
        "administration charge. If the tribunal determines the charge is payable, the tribunal may also "
        "determine—"},
    {"kind": "bullet", "text": "who should pay the administration charge and who it should be paid to;"},
    {"kind": "bullet", "text": "the amount;"},
    {"kind": "bullet", "text": "the date it should be paid by; and"},
    {"kind": "bullet", "text": "how it should be paid."},
    {"kind": "cont", "text": "However, you do not have this right where—"},
    {"kind": "bullet", "text": "a matter has been agreed to or admitted by you;"},
    {"kind": "bullet", "text": "a matter has been, or is to be, referred to arbitration or has been "
        "determined by arbitration and you agreed to go to arbitration after the disagreement about the "
        "administration charge arose; or"},
    {"kind": "bullet", "text": "a matter has been decided by a court."},
    {"kind": "num", "text": "(5) You have the right to apply to a leasehold valuation tribunal for an order "
        "varying the lease on the grounds that any administration charge specified in the lease, or any "
        "formula specified in the lease for calculating an administration charge is unreasonable."},
    {"kind": "num", "text": "(6) Where you seek a determination or order from a leasehold valuation "
        "tribunal, you will have to pay an application fee and, where the matter proceeds to a hearing, a "
        "hearing fee, unless you qualify for a waiver or reduction. The total fees payable to the tribunal "
        "will not exceed £500, but making an application may incur additional costs, such as "
        "professional fees, which you may have to pay."},
    {"kind": "num", "text": "(7) A leasehold valuation tribunal has the power to award costs, not exceeding "
        "£500, against a party to any proceedings where—"},
    {"kind": "bullet", "text": "it dismisses a matter because it is frivolous, vexatious or an abuse of "
        "process; or"},
    {"kind": "bullet", "text": "it considers that a party has acted frivolously, vexatiously, abusively, "
        "disruptively or unreasonably."},
    {"kind": "cont", "text": "The Lands Tribunal has similar powers when hearing an appeal against a "
        "decision of a leasehold valuation tribunal."},
    {"kind": "num", "text": "(8) Your lease may give your landlord a right of re-entry or forfeiture where "
        "you have failed to pay charges which are properly due under the lease. However, to exercise this "
        "right, the landlord must meet all the legal requirements and obtain a court order. A court order "
        "will only be granted if you have admitted you are liable to pay the amount or it is finally "
        "determined by a court, a tribunal or by arbitration that the amount is due. The court has a wide "
        "discretion in granting such an order and it will take into account all the circumstances of the "
        "case."},
    {"kind": "note", "text": "Note: since 1 July 2013 the functions of the leasehold valuation tribunal "
        "have been exercised by the First-tier Tribunal (Property Chamber), and those of the Lands Tribunal "
        "by the Upper Tribunal (Lands Chamber). References above to the leasehold valuation tribunal and "
        "the Lands Tribunal are to be read accordingly."},
]


# --------------------------------------------------------------------------- context
def _fmt_date(raw: str | None) -> str:
    """Format an ISO or free date as '9 September 2026'; pass through anything already human."""
    s = (raw or "").strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            d = datetime.datetime.strptime(s, fmt).date()
            return d.strftime("%d %B %Y").lstrip("0")
        except ValueError:
            continue
    return s


def _sc_year_end(today: datetime.date | None = None) -> str:
    """The current service-charge year end (30 June), e.g. 'year to 30 June 2027'. The RTM year runs
    1 July to 30 June (director-confirmed); July onwards is in the year ending the following 30 June."""
    today = today or datetime.date.today()
    yr = today.year + 1 if today.month >= 7 else today.year
    return f"30 June {yr}"


def _company_number() -> str:
    try:
        from . import rtm_registers as REG
        return str(REG.get_company_register().get("company_number") or "").strip()
    except Exception:
        return ""


def first_stage_context(flat_or_ref: str, *, today: datetime.date | None = None,
                        **overrides: Any) -> dict[str, Any]:
    """Assemble every field the Stage-1 letter + statutory summary need, from the job record + the single
    company/branding source. Overrides win over the stored job (so a fresh request can be answered before
    it is saved). Returns the context plus a flag list for the director to verify before issue."""
    from .rtm_management import resolve_flat as _resolve_flat
    fid, cands, err = _resolve_flat(flat_or_ref)
    if not fid:
        if cands:
            return {"ok": False, "error": "ambiguous", "candidates": [c.get("flat_id") for c in cands]}
        # allow a bare job_ref lookup
        j = get_job(flat_or_ref)
        if not j:
            return {"ok": False, "error": err or f"no flat or job matching {flat_or_ref!r}"}
        fid = j["flat_id"]

    job = {**(get_job(fid) or {}), **{k: v for k, v in overrides.items() if v is not None}}
    block, _, flatn = fid.partition("F")
    road = TPL._BLOCKS.get(block[:3], ("", ""))[0]
    premises = TPL.premises(block, flatn)
    B = TPL.BRANDING
    fee_pence = int(job.get("fee_pence") or FEE_GBP * 100)
    fee_txt = f"£{fee_pence // 100:,}" + ("" if fee_pence % 100 == 0 else f".{fee_pence % 100:02d}")
    payment_ref = f"Flat {flatn}, {road}"
    letter_date = _fmt_date(job.get("letter_date")) or datetime.date.today().strftime("%d %B %Y").lstrip("0") \
        if not today else today.strftime("%d %B %Y").lstrip("0")
    sc_year = _sc_year_end(today)

    flags: list[str] = []
    seller = (job.get("seller_name") or "").strip()
    if not seller:
        seller = "the seller"
        flags.append("Seller name not on the job record: add it (from the LPE1) before issue.")
    firm = (job.get("conveyancer_firm") or "").strip()
    contact = (job.get("conveyancer_contact") or "").strip()
    c_email = (job.get("conveyancer_email") or "").strip()
    their_ref = (job.get("their_ref") or "").strip()
    req_date = _fmt_date(job.get("request_date"))
    if not (firm and c_email):
        flags.append("Conveyancer firm/email missing: the letter needs a real addressee before issue.")
    if not their_ref:
        flags.append("Their reference missing: quote it so their file matches.")
    if not req_date:
        flags.append("Date of the LPE1 request missing: confirm it for the opening sentence.")

    ctx = {
        "ok": True, "flat_id": fid, "premises": premises, "seller": seller, "form": FORM,
        "letter_date": letter_date, "sc_year_end": sc_year,
        "our_ref": job.get("job_ref") or our_ref(fid), "their_ref": their_ref,
        "conveyancer_firm": firm, "conveyancer_contact": contact, "conveyancer_email": c_email,
        "conveyancer_address": (job.get("conveyancer_address") or "").strip(),
        "request_date": req_date, "fee_txt": fee_txt, "fee_pence": fee_pence, "payment_ref": payment_ref,
        "stage": job.get("stage") or "new", "company_number": _company_number(),
        "bank": {"name": B["bank_name"], "sort_code": B["bank_sort_code"],
                 "account_number": B["bank_account_number"], "account_name": B["bank_account_name"]},
        "flags": flags,
    }
    ctx["subject"] = f"{premises}"
    ctx["body_paragraphs"] = _cover_paragraphs(ctx)
    return ctx


def _cover_paragraphs(c: dict[str, Any]) -> list[str]:
    """The Stage-1 cover-letter body, generated from the context. Dash-free house style (statutory dashes
    live only in the accompanying summary). Mirrors the director's own reference letter of 13 Sep 2026."""
    bk = c["bank"]
    seller = c["seller"]
    req = c["request_date"] or "the above date"
    return [
        f"Your ref: {c['their_ref'] or '(not stated)'}    Our ref: {c['our_ref']}",
        f"Re: {c['premises']}",
        f"Proposed sale by {seller}. Form LPE1.",
        f"Thank you for your letter of {req} enclosing Form LPE1 in connection with the proposed sale of "
        "the above property.",
        f"{_company_name()} is the Right to Manage company for {_blocks_phrase()} and, "
        f"since acquiring the right to manage on {RTM_ACQUISITION_HUMAN}, has been the management company "
        f"in respect of the building and undercroft only; estate services remain with {_estate_clause()}. "
        "We complete Form LPE1 in that capacity.",
        "We have completed Form LPE1. We will release the following on receipt of our fee:",
        "1. the completed Form LPE1, together with the Company's attached replies, which expand on the "
        "answers given on the form; and",
        f"2. the current service-charge budget (year to {c['sc_year_end']}); the Company's Memorandum and "
        "Articles of Association; and the form of deed of covenant.",
        f"Fee. Our fee for providing the management information and completing Form LPE1 is {c['fee_txt']}. "
        "This is a reasonable administration charge; the Company is not registered for VAT, so no VAT is "
        f"chargeable. Payment is by bank transfer only, to {bk['account_name']} ({bk['name']}), sort code "
        f"{bk['sort_code']}, account number {bk['account_number']}, quoting reference "
        f"'{c['payment_ref']}'. We do not accept cheques.",
        "The fee is payable by the seller. Please arrange payment at your earliest convenience so that we "
        "can release the completed form and enclosures without delay.",
        "A summary of tenants' rights and obligations in respect of administration charges accompanies "
        "this letter.",
        "Notice of Assignment and Charge. On completion, the lease (Leasebook clause 10.7) requires the "
        "incoming tenant, within one month, to give notice of the assignment to both the Company and the "
        "freeholder and to enter a deed of covenant with the Company. The Company's fee for registering "
        "the interest and dealing with the deed of covenant is £65 (the Company is not registered "
        "for VAT, so no VAT is added); the freeholder separately charges £65 plus VAT. On a charge "
        "or mortgage the fee is £25 to each. The Company will provide its form of deed of covenant "
        "and, on completion of the deed and payment, will issue the certificate of compliance required by "
        "the Land Registry restriction on the title. Notice to the Company may be given by email to "
        f"{TPL.BRANDING['email']}.",
        "Membership. As the property is managed by a Right to Manage company, the buyer will be entitled "
        f"to become a member of {_company_name()} on completion. The procedure is set out "
        "in our reply to enquiry 2.5.",
        "When released, the completed form will include our replies on buildings insurance (Section 5) "
        "and on disputes (Section 6), which you and the buyer's solicitors will wish to note.",
        "Please do not hesitate to contact us if you require anything further.",
    ]


# --------------------------------------------------------------------------- rendering
def cover_letter_text(c: dict[str, Any]) -> str:
    """Plain-text rendering of the cover letter (for review / an email body), letterhead omitted."""
    lines = [c["letter_date"], ""]
    if c["conveyancer_firm"]:
        lines.append(c["conveyancer_firm"])
    if c["conveyancer_contact"]:
        lines.append(f"FAO: {c['conveyancer_contact']}")
    if c["conveyancer_email"]:
        lines.append(c["conveyancer_email"])
    lines += ["", "Dear Sirs", ""]
    lines += c["body_paragraphs"]
    lines += ["", "Yours faithfully", TPL.BRANDING["director"],
              f"Director, {_company_name()}",
              "Management company in respect of the building and undercroft only"]
    return "\n\n".join(lines)


def schedule_11_text(c: dict[str, Any]) -> str:
    """Plain-text rendering of the statutory administration-charge summary bound to this demand."""
    out = [SCHEDULE_11_TITLE, "",
           f"This summary accompanies the Company's demand for its fee for providing management "
           f"information (Form LPE1) in respect of {c['premises']}.", ""]
    for p in SCHEDULE_11_PARAS:
        prefix = "    • " if p["kind"] == "bullet" else ""
        out.append(prefix + p["text"])
    return "\n\n".join(out)


def _render_cover_letter_pdf(c: dict[str, Any]) -> dict[str, Any]:
    """Render the cover letter through the ONE branded template (branded_letter.docx) and convert to PDF."""
    addr1 = (f"FAO: {c['conveyancer_contact']}" if c["conveyancer_contact"] else c["conveyancer_address"])
    ctx = TPL.letter_context(
        letter_date=c["letter_date"], leaseholder_name=c["conveyancer_firm"] or "The Conveyancer",
        addr_line_1=addr1, addr_line_2=c["conveyancer_email"], paragraphs=c["body_paragraphs"],
        sign_off="Yours faithfully", greeting_name="Sirs")
    r = TPL.render_docx("branded_letter", ctx, validate=False)
    if not r.get("ok"):
        return r
    p = TPL.to_pdf(r["path"])
    return {"ok": True, "docx": r["path"], "pdf": p.get("path") if p.get("ok") else None,
            "pdf_reason": None if p.get("ok") else p.get("reason")}


def _render_summary_pdf(c: dict[str, Any], out_path: str | None = None) -> dict[str, Any]:
    """Render the statutory administration-charge summary as its own branded enclosure (reportlab). Every
    identity fact is read from the single company source, so it cannot drift from the demand/letterhead."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    except Exception as exc:
        return {"ok": False, "reason": f"reportlab_missing: {exc}"}
    from pathlib import Path
    B = TPL.BRANDING
    first = _profile.block_codes()[0]
    block6 = TPL._BLOCKS.get(first) or (_profile.block_address(first), "")
    reg_office = f"Flat 1, {block6[0]}, {TPL._TOWN} {block6[1]}"
    hdr3 = " · ".join(x for x in (
        f"Company No. {c['company_number']}" if c["company_number"] else "", B["email"], B["tel"]) if x)

    out = Path(out_path or (TPL._output_dir() / f"AdminChargeSummary_{c['flat_id']}.pdf"))
    styles = getSampleStyleSheet()
    st_hdr = ParagraphStyle("hdr", parent=styles["Normal"], alignment=TA_CENTER, fontSize=11, leading=15)
    st_hdr_sm = ParagraphStyle("hdrsm", parent=styles["Normal"], alignment=TA_CENTER, fontSize=8.5, leading=12,
                               textColor="#555555")
    st_title = ParagraphStyle("title", parent=styles["Normal"], fontSize=12, leading=16, spaceBefore=10,
                              spaceAfter=8)
    st_body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9.5, leading=13, spaceAfter=6)
    st_bullet = ParagraphStyle("bullet", parent=st_body, leftIndent=16, bulletIndent=4, spaceAfter=3)
    st_note = ParagraphStyle("note", parent=st_body, fontSize=8.5, leading=12, textColor="#555555",
                             spaceBefore=8)

    flow: list[Any] = [
        Paragraph(B["company"], st_hdr),
        Paragraph(reg_office, st_hdr_sm),
        Paragraph(hdr3, st_hdr_sm),
        Spacer(1, 6 * mm),
        Paragraph(SCHEDULE_11_TITLE, st_title),
        Paragraph(f"This summary accompanies the Company's demand for its fee for providing management "
                  f"information (Form LPE1) in respect of {c['premises']}.", st_body),
        Spacer(1, 2 * mm),
    ]
    for p in SCHEDULE_11_PARAS:
        if p["kind"] == "intro":
            flow.append(Paragraph(f"<i>{p['text']}</i>", st_body))
        elif p["kind"] == "bullet":
            flow.append(Paragraph(p["text"], st_bullet, bulletText="•"))
        elif p["kind"] == "note":
            flow.append(Paragraph(p["text"], st_note))
        else:
            flow.append(Paragraph(p["text"], st_body))
    doc = SimpleDocTemplate(str(out), pagesize=A4, topMargin=18 * mm, bottomMargin=18 * mm,
                            leftMargin=22 * mm, rightMargin=22 * mm, title=SCHEDULE_11_TITLE)
    doc.build(flow)
    return {"ok": True, "pdf": str(out)}


def first_stage_response(flat_or_ref: str, *, render: bool = True, download_ttl_s: int = 3600,
                         today: datetime.date | None = None, **overrides: Any) -> dict[str, Any]:
    """Produce the full Stage-1 response for a flat/job: the branded cover letter + the statutory
    administration-charge summary, plus the plain-text of both for review, the director flags, and (if
    render) capability download links. NEVER sends anything and NEVER releases the completed form (that is
    Stage 2, gated on stage == 'paid'). Human sign-off before issue is mandatory."""
    c = first_stage_context(flat_or_ref, today=today, **overrides)
    if not c.get("ok"):
        return c
    result: dict[str, Any] = {
        "ok": True, "flat_id": c["flat_id"], "our_ref": c["our_ref"], "stage": c["stage"],
        "premises": c["premises"], "seller": c["seller"], "fee": c["fee_txt"],
        "cover_letter_text": cover_letter_text(c),
        "schedule_11_text": schedule_11_text(c),
        "flags": c["flags"],
        "before_issue": [
            "Human sign-off is mandatory before this leaves the building (spec rule).",
            "This is Stage 1: cover letter + statutory summary only. The completed Form LPE1 and the "
            "enclosures are released ONLY on receipt of the fee (Stage 2).",
            "Do not warrant the currency of buildings insurance here; that is a Stage-2 reply.",
        ],
    }
    if render:
        cl = _render_cover_letter_pdf(c)
        if cl.get("ok"):
            path = cl.get("pdf") or cl.get("docx")
            fname = f"LPE1_CoverLetter_{c['flat_id']}.pdf" if cl.get("pdf") else f"LPE1_CoverLetter_{c['flat_id']}.docx"
            mt = "application/pdf" if cl.get("pdf") else TPL._DOCX_MIME
            tok = TPL.mint_download(path, fname, download_ttl_s, media_type=mt)
            result["cover_letter"] = {"download_url": f"/rtm/doc/{tok}", "filename": fname,
                                      "path": path, "pdf_reason": cl.get("pdf_reason")}
        else:
            result["cover_letter"] = {"ok": False, "reason": cl.get("reason"), "validation": cl.get("validation")}
        sm = _render_summary_pdf(c)
        if sm.get("ok"):
            fname = f"LPE1_AdminChargeSummary_{c['flat_id']}.pdf"
            tok = TPL.mint_download(sm["pdf"], fname, download_ttl_s, media_type="application/pdf")
            result["admin_charge_summary"] = {"download_url": f"/rtm/doc/{tok}", "filename": fname,
                                              "path": sm["pdf"]}
        else:
            result["admin_charge_summary"] = {"ok": False, "reason": sm.get("reason")}
    return result
