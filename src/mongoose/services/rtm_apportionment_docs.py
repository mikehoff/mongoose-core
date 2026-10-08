"""Branded PDF documents for the two-schedule apportionment engine: credit notes (spec R26) and a
STAGED per-flat demand pack for auditing the engine's output against a hand-made gold standard.

reportlab is the PDF path (this box has no LibreOffice, so the .docx templates cannot export to PDF).
Documents are self-contained one-pagers with the RTM company's branding. Staged demand documents are
clearly watermarked STAGED and are NOT the same as issuing a demand: nothing here writes to the ledger.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import profile as _profile
from .rtm_templates import BRANDING, premises
from .rtm_management import _money


def _logo_path() -> Path | None:
    p = Path(__file__).resolve().parent.parent / "assets" / "rtm_templates" / "logo.png"
    return p if p.exists() else None


def _brand_header(c, width, y):
    """Draw the company header block, return the y below it."""
    from reportlab.lib.units import mm
    logo = _logo_path()
    if logo:
        try:
            c.drawImage(str(logo), 18 * mm, y - 16 * mm, width=22 * mm, height=16 * mm,
                        preserveAspectRatio=True, mask="auto")
        except Exception:
            pass
    x = width - 18 * mm
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(x, y - 4 * mm, BRANDING["company"])
    c.setFont("Helvetica", 8.5)
    c.drawRightString(x, y - 9 * mm, premises(_profile.block_codes()[0], 1))
    c.drawRightString(x, y - 13 * mm, f"Tel {BRANDING['tel']}  ·  {BRANDING['email']}")
    return y - 22 * mm


def render_credit_note_pdf(*, out_path: str, credit_note: dict[str, Any], leaseholder_name: str = "",
                           block: str = "", flat_number: int | str = "") -> str:
    """A one-page branded credit note (spec R26)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors
    width, height = A4
    c = canvas.Canvas(str(out_path), pagesize=A4)
    y = _brand_header(c, width, height - 16 * mm)
    c.setFillColor(colors.HexColor("#8a8f2f"))
    c.setFont("Helvetica-Bold", 15)
    c.drawString(18 * mm, y, "Service charge credit note")
    c.setFillColor(colors.black)
    y -= 8 * mm
    c.setFont("Helvetica", 10)
    c.drawString(18 * mm, y, f"Credit note: {credit_note.get('id', '')}    Issued: {credit_note.get('issued_date', '')}")
    y -= 5 * mm
    if block and flat_number != "":
        c.drawString(18 * mm, y, premises(block, flat_number))
        y -= 5 * mm
    c.drawString(18 * mm, y, f"Leaseholder: {leaseholder_name}    Flat ref: {credit_note.get('flat_id', '')}")
    y -= 12 * mm
    c.setFont("Helvetica", 10)
    c.drawString(18 * mm, y, f"Schedule: {credit_note.get('schedule', '')}   Period: {credit_note.get('period', '')}")
    y -= 6 * mm
    reason = credit_note.get("reason") or "Schedule over-recovery reconciled at period end."
    c.drawString(18 * mm, y, f"Reason: {reason}")
    y -= 12 * mm
    c.setFont("Helvetica-Bold", 13)
    c.drawString(18 * mm, y, "Amount credited to your account")
    c.drawRightString(width - 18 * mm, y, _money(int(credit_note.get("amount_pence", 0))))
    y -= 14 * mm
    c.setFont("Helvetica-Oblique", 8.5)
    c.setFillColor(colors.HexColor("#777777"))
    c.drawString(18 * mm, y, "This credit has been applied to your service charge account and reduces the balance shown in your portal.")
    c.showPage()
    c.save()
    return str(out_path)


def _gbp(pence: int) -> str:
    """£ with pence, e.g. 93992 -> '£939.92'. Uses the shared money formatter."""
    return _money(int(pence))


def _whole(pence: int) -> str:
    """Whole pounds with a thousands separator and no £/decimals, e.g. 610000 -> '6,100' (the Annual £
    column style in the budget table). Budget lines are whole-pound amounts."""
    return f"{int(pence) // 100:,}"


def _deed_share(ppm: int, amount_pence: int) -> int:
    """A flat's share of an amount at its exact lease percentage, simple-rounded to the penny. This is the
    deed-exact method the gold-standard demands use (each flat pays its stated percentage; the tiny drift
    because deed percentages do not sum to exactly 100% is real over-recovery, reconciled at year end)."""
    from decimal import Decimal, ROUND_HALF_UP
    return int((Decimal(int(ppm)) / Decimal(1_000_000) * Decimal(int(amount_pence))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _payment_ref(fl: dict, flat_id: str) -> str:
    """The flat's payment reference: the stored one, else the leaseholder surname UPPER-cased with
    apostrophes/spaces/punctuation stripped so bank systems do not mangle it (O'Brien -> OBRIEN,
    De Vermeulen -> DEVERMEULEN), matching how the 14 June demands went out. Never the bare flat id."""
    import re
    ref = str(fl.get("payment_reference") or "").strip()
    if ref:
        return ref
    name = str(fl.get("leaseholder_name") or "").strip()
    if name:
        # Surname = last holder's family name, including a prefix like "De"/"Van".
        parts = name.split(" and ")[-1].split()
        _prefix = {"de", "van", "von", "le", "la", "du", "del", "della", "di"}
        surname = (parts[-2] + parts[-1]) if len(parts) >= 2 and parts[-2].lower() in _prefix else (parts[-1] if parts else "")
        return re.sub(r"[^A-Z0-9]", "", surname.upper())
    return flat_id


def demand_docx_context(*, flat_id: str, year: str, period_label: str,
                        numerator: int = 1, denominator: int = 2,
                        demand_date: str, due_date: str, doc_number: str,
                        deliver_to_premises: bool = False,
                        opening_sentence: str = "This is the first service charge demand issued by the management company.") -> dict[str, Any]:
    """Build the exact merge context for the service_charge_demand.docx template from live data. Fills only
    the template's own fields with real figures; adds no wording, labels, or formatting of its own.

    Amounts use the deed-exact method (each flat's exact lease percentage, simple-rounded), matching the
    gold-standard demands. Budget lines come from the database, ordered by their stored sort_order so they
    print in the budget's own order, not alphabetically."""
    from . import rtm_apportionment as APP
    from .rtm_management import _get_flat
    from ..db import connect
    from .rtm_templates import premises as _premises, split_address, first_name_from

    fl = _get_flat(flat_id) or {}
    name = str(fl.get("leaseholder_name") or "")
    block = str(fl.get("block") or flat_id[:3])
    num = int(fl.get("flat_number") or 0)
    # HAND DELIVERY: address the copy to the FLAT rather than to where the leaseholder lives. A leaseholder
    # who lives elsewhere still gets post at the flat, and a demand pushed through that door must not carry
    # their home address on it: the envelope goes in a shared mailbox area where neighbours can read it.
    # The correspondence address remains untouched on the record; this changes only the printed copy.
    if deliver_to_premises:
        a1, a2 = _premises(block, num), ""
    else:
        a1, a2 = split_address(str(fl.get("correspondence_address") or ""))

    # Budget line items per schedule, in the budget's own order (sort_order when present, else stable).
    with connect() as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(rtm_mgmt_budget_lines)").fetchall()}
        order = "sort_order, rowid" if "sort_order" in cols else "rowid"
        s1 = con.execute(f"SELECT budget_line, amount_pence FROM rtm_mgmt_budget_lines WHERE year=? AND schedule='sch1' ORDER BY {order}", (str(year),)).fetchall()
        s2 = con.execute(f"SELECT budget_line, amount_pence FROM rtm_mgmt_budget_lines WHERE year=? AND schedule='sch2' ORDER BY {order}", (str(year),)).fetchall()
    sch1_items = [{"label": str(r[0]), "amount": _whole(int(r[1]))} for r in s1]
    sch2_items = [{"label": str(r[0]), "amount": _whole(int(r[1]))} for r in s2]
    sch1_budget = sum(int(r[1]) for r in s1)
    sch2_budget = sum(int(r[1]) for r in s2)
    sch1_half_budget = APP.schedule_period_pence("sch1", year, numerator, denominator)
    sch2_half_budget = APP.schedule_period_pence("sch2", year, numerator, denominator)

    ppm1 = APP.flat_apportionment(flat_id).get("sch1", 0)
    ppm2 = APP.flat_apportionment(flat_id).get("sch2", 0)
    # Deed-exact per cell (matches the gold standard; annual is deed% of the annual budget, not 2 * half).
    sch1_annual = _deed_share(ppm1, sch1_budget); sch1_half = _deed_share(ppm1, sch1_half_budget)
    uc_annual = _deed_share(ppm2, sch2_budget); uc_half = _deed_share(ppm2, sch2_half_budget)
    total = sch1_half + uc_half

    return {
        "demand_date": demand_date, "doc_number": doc_number,
        "leaseholder_name": name, "addr_line_1": a1 or _premises(block, num), "addr_line_2": a2,
        # Salutation is stored verbatim as the director wrote it (not derived): titles, joint holders and
        # single names each follow their own convention. Falls back to a first-name guess only if unset.
        "premises": _premises(block, num),
        "greeting_name": str(fl.get("greeting_name") or "").strip() or first_name_from(name) or name,
        "opening_sentence": opening_sentence, "period": period_label,
        "sch1_pct": APP.ppm_to_pct(ppm1) + "%", "sch1_annual": _gbp(sch1_annual), "sch1_half": _gbp(sch1_half),
        "uc_pct": APP.ppm_to_pct(ppm2) + "%", "uc_annual": _gbp(uc_annual), "uc_half": _gbp(uc_half),
        "total_due": _gbp(total), "due_date": due_date,
        "payment_ref": _payment_ref(fl, flat_id),
        "budget_year_end": "30 June 2027",
        "sch1_subtotal": _whole(sch1_budget), "sch2_subtotal": _whole(sch2_budget),
        "total_annual": "£" + _whole(sch1_budget + sch2_budget), "half_year_all": _gbp(sch1_half_budget + sch2_half_budget),
        "sch1_items": sch1_items, "sch2_items": sch2_items,
    }


def render_demand_docx(*, flat_id: str, year: str, period_label: str, demand_date: str, due_date: str,
                       doc_number: str, out_path: str, numerator: int = 1, denominator: int = 2,
                       deliver_to_premises: bool = False,
                       opening_sentence: str = "This is the first service charge demand issued by the management company.") -> dict[str, Any]:
    """Render the flat's service charge demand from the real service_charge_demand.docx template. Output is
    .docx (the template's own fonts/layout, untouched). PDF export needs LibreOffice on the box."""
    from .rtm_templates import render_docx
    ctx = demand_docx_context(flat_id=flat_id, year=year, period_label=period_label, demand_date=demand_date,
                              due_date=due_date, doc_number=doc_number, numerator=numerator, denominator=denominator,
                              deliver_to_premises=deliver_to_premises,
                              opening_sentence=opening_sentence)
    return render_docx("service_charge_demand", ctx, out_path=out_path, validate=False)
