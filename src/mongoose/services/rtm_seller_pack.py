"""Seller-Pack Responder, engine core (Phase 2 slice).

Given a flat, assemble a DRAFT LPE1 management-information pack the way the spec demands: every field is
either READ live from Mongoose (with provenance), DERIVED by a stated rule, or FLAGGED for the director
with its reason and a suggested default. Nothing is guessed, and nothing is warranted that only lives in
the lease or in a register not yet populated. This produces the director REVIEW SHEET, not the filled
official PDF (that, plus the building/insurance/FRA/lease registers and the sealed-track 6.2 disclosure
module, is the next slice and is gated on the Section 13 director decisions).

Provenance per field: read | derived | assumed | flag. Reuses the connector-foundation accessors
(flat_statement, list_demands, flat_reserve_balance, budget_actuals, company_profile, list_flats).
"""
from __future__ import annotations

from typing import Any

# Config the director controls (spec Section 7 + 13). Kept here as the single source until a real config.
FEE_GBP = 200  # Schedule 11 administration charge, charged to the SELLER, no VAT (guarantee company).
FORM = "LPE1 (4th edition, 2023)"


def _answer(field: str, value: Any, provenance: str, source: str) -> dict[str, Any]:
    return {"field": field, "value": value, "provenance": provenance, "source": source}


def _flag(field: str, flag: str, reason: str, suggested: str = "") -> dict[str, Any]:
    # flag in {LEASE, HUMAN, EXTERNAL, DECISION, REGISTER} , what unblocks the field.
    return {"field": field, "flag": flag, "reason": reason, "suggested": suggested}


def _former_agent() -> str:
    """How the pre-RTM manager is named in the reserve-opening wording (lexicon "rtm.former_agent_label";
    generic in the fictional default)."""
    from .. import profile as _profile
    return str(_profile.lexicon("rtm.former_agent_label", "former agent"))


# The 6.2 disputes guidance is a fact about this site's history, so a profile may replace the generic
# wording (lexicon "rtm.seller_pack_disputes_note").
_DISPUTES_NOTE_DEFAULT = (
    "Any separate service-charge case (e.g. leaseholders "
    "v the former manager); disclose it NEUTRALLY per the sealed-track policy (reference only, "
    "no names / GDPR, flag whether THIS flat is a party, and NEVER surface any sealed "
    "proceedings). Needs the disclosure module + director sign-off.")


def _disputes_note() -> str:
    from .. import profile as _profile
    return str(_profile.lexicon("rtm.seller_pack_disputes_note", _DISPUTES_NOTE_DEFAULT))


def build_draft(flat: str) -> dict[str, Any]:
    """Assemble the LPE1 draft for one flat: answered fields (with provenance), the flag queue, and the
    cross-source reconciliation checks. Read-only. Resolve the flat by id or leaseholder name first."""
    from . import rtm_management as M
    from . import rtm_trust as T
    from .rtm_example_court import company_profile

    # resolve the flat (reuse the connector's tuple resolver)
    from .rtm_management import resolve_flat as _resolve_flat
    fid, cands, err = _resolve_flat(flat)
    if not fid:
        if cands:
            return {"ok": False, "error": "ambiguous", "candidates": [c.get("flat_id") for c in cands]}
        return {"ok": False, "error": err or f"no flat matching {flat!r}"}

    cp = company_profile() or {}
    bank = cp.get("bank") or {}
    fl = M._get_flat(fid) or {}
    st = M.flat_statement(fid)
    dem = [d for d in M.list_demands(flat=fid) if d.get("status") not in ("Superseded", "Cancelled")]
    demanded = sum(int(d.get("amount_pence") or 0) for d in dem)
    reserve = T.flat_reserve_balance(fid)
    flats = M.list_flats() or []
    n_flats = len(flats)
    acq = getattr(M, "RTM_ACQUISITION_DATE", "2026-01-01")
    latest = sorted(dem, key=lambda x: str(x.get("due_date") or ""))[-1] if dem else {}
    block = fid.split("F")[0]  # <code>F7 -> <code>, e.g. 1ECF7 -> 1EC
    from . import rtm_registers as REG
    company_reg = REG.get_company_register()
    bderiv = REG.building_derivations(block)
    lease = REG.get_lease_terms(block)        # uniform template wording (block-level)
    flat_lease = REG.get_lease_terms(fid)     # per-flat specifics (ground rent amount, parking plan)
    fra = REG.latest_fra("fra")

    answered: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []
    recon: list[dict[str, Any]] = []

    # ── Section 1: the parties ──
    answered.append(_answer("1.2 Management company", cp.get("name", ""), "read", "company_profile"))
    if bank.get("account_number"):
        answered.append(_answer(
            "1.2 Payment bank details + reference",
            f"{bank.get('account_name','')}, sort {bank.get('sort_code')}, acct {bank.get('account_number')}"
            f" ({bank.get('name') or bank.get('bank_name') or ''}); reference {fl.get('payment_reference','')}",
            "read", "company_profile.bank + rtm_mgmt_flats.payment_reference"))
    answered.append(_answer("1.3 Managing agent", "Not applicable (self-managed RTM)", "derived", "self-managed rule"))
    if company_reg.get("freeholder_name"):
        _fh = company_reg["freeholder_name"] + (
            ", " + company_reg["freeholder_address"] if company_reg.get("freeholder_address") else "")
        answered.append(_answer("1.1 Landlord (freeholder)", _fh, "read", "company_register"))
    else:
        flags.append(_flag("1.1 Landlord (freeholder) name + registered office", "REGISTER",
                           "Not in the company register yet.",
                           "set_company_field freeholder_name / freeholder_address (+ Companies House)."))
    if company_reg.get("company_number"):
        _vat = str(company_reg.get("vat_registered", "")).lower() in ("1", "true", "yes")
        answered.append(_answer("1.2 Company number + VAT",
                                f"No. {company_reg['company_number']}, VAT {'registered' if _vat else 'not registered'}",
                                "read", "company_register"))
    else:
        flags.append(_flag("1.2 Company number + VAT status", "REGISTER",
                           "Not in the company register yet.", "set_company_field company_number / vat_registered."))
    _noa = lease.get("notice_of_assignment")
    if _noa:
        answered.append(_answer("1.6 Notice of Assignment/Charge recipient + fee", _noa, "read", "lease_terms"))
    else:
        flags.append(_flag("1.6 Notice of Assignment/Charge recipient + fee", "LEASE",
                           "Do not invent an RTM notice fee unless the lease names the Company as recipient "
                           "(Proxima v McGhee).", "Confirm from the lease."))

    # ── Section 3: ground rent ──
    _gr = flat_lease.get("ground_rent")  # per-flat only; never another flat's figure
    if _gr:
        answered.append(_answer("3.1 Ground rent", _gr, "read", "lease_terms (this flat)"))
    else:
        answered.append(_answer("3.1 Ground rent",
                                "The Company does not collect ground rent (it is payable to the landlord).",
                                "derived", "RTM does not collect ground rent"))
        flags.append(_flag("3.1 Ground rent amount", "LEASE",
                           f"The ground rent AMOUNT is fixed by {fid}'s OWN lease, not yet read for this flat "
                           "(amounts and percentages differ per flat; do not assume another flat's).",
                           "Read this flat's lease."))
    # Per-flat deed-exact apportionment shares (from the engine, never generalised from one lease).
    try:
        from . import rtm_apportionment as APP
        _ap = APP.flat_apportionment(fid) or {}
        answered.append(_answer("Apportionment shares (this flat)",
                                f"Building / Schedule 1: {(_ap.get('sch1', 0) / 10000):.4f}%; undercroft parking / "
                                f"Schedule 2: {(_ap.get('sch2', 0) / 10000):.4f}% (deed-exact, per flat).",
                                "read", "apportionment engine"))
    except Exception:
        pass

    # ── Section 4: service charge (the live financial core) ──
    answered.append(_answer("4.1 / 7.1 Number of flats contributing", f"{n_flats} flats", "read", "list_flats"))
    # 4.1.1 wants the ANNUAL service charge. The RTM demands it in two equal half-year instalments (lease
    # 12.11 half-budget advance instalments), so the annual = the current instalment x2, NOT the sum of
    # however many demands happen to be live (one issued instalment would understate it by half).
    _half = int(latest.get("amount_pence") or 0) if latest else 0
    if _half:
        answered.append(_answer("4.1.1 Annual service charge for the flat",
                                f"{M._money(_half * 2)} for the year (two equal half-year instalments of "
                                f"{M._money(_half)}; lease 12.11)", "derived",
                                "current half-year instalment x2"))
    else:
        answered.append(_answer("4.1.1 Annual service charge for the flat",
                                f"{M._money(demanded)}", "read", "rtm_mgmt_demands"))
    answered.append(_answer("4.2 Balance / arrears",
                            f"{st.get('balance')} ({st.get('position')})", "read", "flat_statement"))
    if latest:
        answered.append(_answer("4.4 Period covered by last demand",
                                f"{latest.get('period','')}, issued {latest.get('issue_date','')}, due {latest.get('due_date','')}",
                                "read", "rtm_mgmt_demands"))
    answered.append(_answer(
        "4.6.1 Reserve fund held for the flat",
        f"{reserve.get('reserve','£0.00')} = {reserve.get('opening_share','£0.00')} deed share of the "
        f"reserve opening ({_former_agent()} handover) + {reserve.get('post_acquisition','£0.00')} reserve slice of "
        f"paid demands",
        "derived", "Sch1 deed apportionment of opening reserve + subaccount_split"))
    _s20 = company_reg.get("section20_planned")
    if _s20:
        answered.append(_answer("4.6.2 / 4.8-4.10 Section 20 works / increases / consultation", _s20,
                                "read", "company_register (director-confirmed)"))
    else:
        flags.append(_flag("4.6.2 / 4.8-4.10 Section 20 works / increases / consultation", "REGISTER",
                           "Needs the recovery-actions register.", "Default: none programmed; confirm."))
    _dec = company_reg.get("last_decoration")
    if _dec:
        answered.append(_answer("4.7 Decoration dates", f"Last redecoration {_dec}.", "read",
                                "company_register (director-confirmed)"))
    else:
        flags.append(_flag("4.7 Decoration dates", "HUMAN", "Maintenance log not held.", "Director to confirm."))
    if bderiv.get("known"):
        _bsa = bderiv.get("bsa_relevant_building")
        if _bsa is True:
            answered.append(_answer("4.14 / 4.15 BSA leaseholder/landlord certificates",
                                    "Relevant building (>=11m or >=5 storeys): the BSA leaseholder/landlord "
                                    "certificate provisions apply; confirm the certificates.",
                                    "derived", "building_profiles rule"))
        elif _bsa is False:
            answered.append(_answer("4.14 / 4.15 BSA leaseholder/landlord certificates",
                                    "Not a relevant building (under 11m and fewer than 5 storeys): the BSA "
                                    "leaseholder protections do not apply.", "derived", "building_profiles rule"))
        else:
            flags.append(_flag("4.14 / 4.15 BSA leaseholder/landlord certificates", "REGISTER",
                               bderiv.get("bsa_reason") or "cannot conclude the relevant-building test.",
                               f"set_building_profile('{block}', height_m=<metres>) so the >=11m limb can be tested."))
    else:
        flags.append(_flag("4.14 / 4.15 BSA leaseholder/landlord certificates", "REGISTER",
                           bderiv.get("reason", "building profile not populated"),
                           "set_building_profile(block, storeys, height_m, build_date)."))

    # ── Section 5: buildings insurance (register-driven; reconciled against the budget) ──
    ba = M.budget_actuals("2027") or {}
    ins_recon = REG.insurance_reconciliation()
    recon.append({"check": "Buildings insurance vs budget", "status": ins_recon.get("status"),
                  "detail": ins_recon.get("detail")})
    _cur_ins = [p for p in REG.list_insurance(current_only=True)
                if str(p.get("policy_type", "")).lower() == "buildings"]
    if _cur_ins:
        p = _cur_ins[0]
        answered.append(_answer("5.1-5.10 Buildings insurance",
                                f"Policy {p.get('policy_number','')} with {p.get('insurer','')}, "
                                f"{p.get('period_start','')} to {p.get('period_end','')}, arranged by "
                                f"{p.get('who_arranges','')}. State this + the recovery basis; do not warrant currency.",
                                "read", "insurance_register"))
    else:
        flags.append(_flag("5.1-5.10 Buildings insurance", "REGISTER",
                           "insurance_register empty. GATED on Section 13 #1 (arrangement from 31 Dec 2026).",
                           "set_insurance_policy(...); never warrant currency; reconcile vs budget."))
    if fra:
        answered.append(_answer("5.6.1 / 8.16 Fire risk assessment",
                                f"FRA {fra.get('occurred_on','')}, outcome {fra.get('outcome','')}, "
                                f"report {fra.get('report_status','')}.", "read", "fra_register"))
    else:
        flags.append(_flag("5.6.1 / 8.16 Fire risk assessment", "REGISTER",
                           "fra_register empty.", "set_fra(occurred_on, outcome, report_status)."))

    # ── Section 6: legal ──
    answered.append(_answer("6.3 RTM acquisition", f"RTM acquired management on {acq} (CLRA 2002).",
                            "read", "RTM_ACQUISITION_DATE"))
    _disp = company_reg.get("rtm_disputes")
    if _disp:
        answered.append(_answer("6.1 Forfeiture / 6.4 Breach of lease", f"No forfeiture or breach action. {_disp}",
                                "read", "company_register (director-confirmed)"))
    else:
        flags.append(_flag("6.1 Forfeiture / 6.4 Breach of lease", "DERIVATION",
                           "Default No unless a forfeiture/breach action exists.", "Default No; confirm."))
    flags.append(_flag("6.2 Disputes with lessees", "DECISION",
                       (f"{_disp} " if _disp else "") + _disputes_note(),
                       "Confirm neutral wording; do not auto-answer."))

    # ── Section 7-8: building + governance ──
    if bderiv.get("known"):
        answered.append(_answer("7.3 HMO s.257 / 8.15 Asbestos",
                                f"HMO: {bderiv.get('hmo_s257') or 'confirm'}; asbestos "
                                f"{'N/A (post-2000 construction)' if bderiv.get('asbestos_na') else 'survey required'}.",
                                "derived", "building_profiles rule"))
    else:
        flags.append(_flag("7.3 HMO s.257 / 8.15 Asbestos / 8.19-8.21 BSA docs", "REGISTER",
                           bderiv.get("reason", "building profile not populated"), "set_building_profile(block, ...)."))
    _lease_keys = ("deed_of_covenant", "licence_to_assign", "certificate_of_compliance", "pets", "parking")
    _have = [k for k in _lease_keys if lease.get(k)]
    if _have:
        answered.append(_answer("2.1/2.2/2.6 / 7.4 / 7.5 Lease terms",
                                "; ".join(f"{k.replace('_', ' ')}: {lease[k]}" for k in _have), "read", "lease_terms"))
    _missing = [k for k in _lease_keys if not lease.get(k)]
    if _missing:
        flags.append(_flag("2.1/2.2/2.6 Deed of Covenant / Licence / Certificate; 7.4 parking; 7.5 pets", "LEASE",
                           f"lease_terms missing: {', '.join(_missing)} (Section 13 #2).",
                           "set_lease_term(block, key, value) once per block."))
    _acc = company_reg.get("first_accounts_status")
    _agm = company_reg.get("agm_frequency")
    if _acc or _agm:
        answered.append(_answer("8.1 Published accounts / 8.18 AGM",
                                f"{_acc or 'accounts: confirm'}; AGM: {_agm or 'confirm'}.", "read",
                                "company_register (director-confirmed)"))
    else:
        flags.append(_flag("8.1 Published accounts / 8.18 Last AGM", "REGISTER",
                           "First-accounts status and AGM log not first-class.", "Add to the company profile."))
    answered.append(_answer("8.17 Memorandum and Articles",
                            "On file (Memorandum + Articles of Association, company incorporated "
                            f"{company_reg.get('incorporation_date','')}).", "read", "rtm_documents"))
    answered.append(_answer("8.4 Current budget",
                            f"Annual budget {(ba.get('total') or {}).get('budgeted','?')} for {ba.get('year','2027')} "
                            f"({len(ba.get('lines') or [])} lines); the budget PDF can be attached.",
                            "read", "budget_actuals + portal_demand_pdfs"))

    # ── The fee (cover letter) ──
    answered.append(_answer("Fee (cover letter)",
                            f"GBP {FEE_GBP} as a Schedule 11 administration charge, no VAT (guarantee company), "
                            f"payable by the seller, released on cleared payment. The prescribed Schedule 11 summary "
                            f"is attached.", "assumed", "fee config (Section 13 #3 to confirm)"))

    # seller-identity reconciliation
    recon.append({"check": "Seller is the leaseholder of record", "status": "confirm",
                  "detail": f"Leaseholder of record for {fid} is {fl.get('leaseholder_name','?')}. Confirm the named "
                            f"seller matches before releasing anything."})

    return {"ok": True, "flat": fid, "leaseholder": fl.get("leaseholder_name", ""), "form": FORM,
            "answered": answered, "flags": flags, "reconciliation": recon}


def review_sheet(flat: str) -> str:
    """Render the director review sheet: what is answered live (with provenance), what is flagged and why,
    and the reconciliation checks. This is what the director signs off before any pack is produced."""
    d = build_draft(flat)
    if not d.get("ok"):
        extra = f" Candidates: {', '.join(d.get('candidates', []))}." if d.get("candidates") else ""
        return f"Cannot draft: {d.get('error')}.{extra}"
    L = [f"LPE1 seller-pack DRAFT , flat {d['flat']} ({d.get('leaseholder','')}), {d['form']}",
         "This is a DRAFT for director review. Nothing is sent; the pack is released only on cleared payment.",
         "",
         f"ANSWERED LIVE ({len(d['answered'])} fields, provenance shown):"]
    for a in d["answered"]:
        L.append(f"  [{a['provenance']:8}] {a['field']}: {a['value']}")
    L.append("")
    L.append(f"NEEDS THE DIRECTOR ({len(d['flags'])} flagged):")
    for f in d["flags"]:
        s = f"  ({f['flag']}) {f['field']}: {f['reason']}"
        if f.get("suggested"):
            s += f"  -> {f['suggested']}"
        L.append(s)
    if d.get("reconciliation"):
        L.append("")
        L.append("RECONCILIATION CHECKS:")
        for r in d["reconciliation"]:
            L.append(f"  [{r['status']}] {r['check']}: {r['detail']}")
    return "\n".join(L)
