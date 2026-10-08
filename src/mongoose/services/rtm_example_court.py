from __future__ import annotations

import json
import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

# Example Court real-data operating knowledge.
#
# The tenant-specific DATA (the 18 flats + leaseholder surnames, the budget lines, and the headline
# budget totals) is no longer baked into this module as code literals: it lives as CONFIG in a JSON
# file so the same code can serve any building and no leaseholder / money figure is a source literal.
# This module LOADS that data at runtime (cached) and keeps the historical public API
# (EXAMPLE_COURT_FLATS, EXAMPLE_COURT_BUDGET, ANNUAL_BUDGET, HALF_YEAR_BUDGET, DEMAND_PACK_TOTAL)
# working, plus a first-class POLICY + CAVEAT layer that is queryable any time , not just import-time
# warnings , so the dashboard, capability state, readiness picture and conversational brain can all
# consult "what's true and what's not yet proven about Example Court" on demand.
#
# Config location: MONGOOSE_RTM_PROPERTY_FILE if set, else <data_dir>/rtm/property.json where
# data_dir = get_settings().data_dir (~/.mongoose/data). Source of the values:
# Service_Charge_Budget_FINAL_v9.xlsx + Example_Court_Demands_All_18_Flats.zip (June 2026),
# cross-validated. Figures in pounds here; the management module stores them in pence.


# The default tenant config shipped inside the package (data-as-asset, not a code literal). It is
# the fallback when neither MONGOOSE_RTM_PROPERTY_FILE nor a per-user <data_dir>/rtm/property.json
# exists, so the code file itself carries none of the flats/leaseholders/budget values.
_PACKAGED_PROPERTY_FILE = Path(__file__).resolve().parent.parent / "assets" / "rtm" / "property.json"


def _property_file() -> Path:
    """Resolve the tenant property-config path in priority order:
      1. MONGOOSE_RTM_PROPERTY_FILE  (explicit override; used exactly as given)
      2. <data_dir>/rtm/property.json  (per-user brain dir, ~/.mongoose/data) if it exists
      3. the packaged default asset shipped with the code
    On the real machine (2) exists and wins; in tests/fresh checkouts (3) provides the same data."""
    override = os.getenv("MONGOOSE_RTM_PROPERTY_FILE")
    if override:
        return Path(override).expanduser()
    try:
        from ..settings import get_settings
        data_dir_file = Path(get_settings().data_dir) / "rtm" / "property.json"
        if data_dir_file.exists():
            return data_dir_file
    except Exception:
        pass
    return _PACKAGED_PROPERTY_FILE


@lru_cache(maxsize=None)
def _load_property_cached(path_str: str) -> dict[str, Any]:
    with open(path_str, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _rtm_name_or_generic() -> str:
    try:
        from .. import profile as _profile
        return _profile.rtm_name()
    except Exception:
        return "RTM"


def load_property() -> dict[str, Any]:
    """Load the tenant property config (flats + budget + headline totals) from JSON, cached per
    resolved path. Raises a clear error if the file is missing rather than silently returning empty
    data, because these figures are money-critical (a blank budget would corrupt apportionment)."""
    path = _property_file()
    if not path.exists():
        raise FileNotFoundError(
            f"{_rtm_name_or_generic()} property config not found at {path}. This file holds the tenant data "
            "(flats, budget lines, headline budget totals). Create it or point "
            "MONGOOSE_RTM_PROPERTY_FILE at it.")
    return _load_property_cached(str(path))


def _flats_from_config() -> list[tuple[str, str, str, str, str, str]]:
    # (block, flat_number, leaseholder_surname, payment_reference, half_year_demand_pounds, deed_note)
    return [
        (f["block"], f["flat"], f["surname"], f["payment_reference"],
         f["half_year_demand"], f["deed_note"])
        for f in load_property().get("flats", [])
    ]


def _budget_from_config() -> list[tuple[str, str, float]]:
    # (label, schedule, annual_amount_pounds) , the real budget lines (subtotals/total excluded)
    return [
        (b["label"], b["schedule"], float(b["annual"]))
        for b in load_property().get("budget", [])
    ]


# Public API preserved: these module-level names are what other modules import and (in tests)
# monkeypatch. They are populated from the loaded config at import time.
EXAMPLE_COURT_FLATS: list[tuple[str, str, str, str, str, str]] = _flats_from_config()
EXAMPLE_COURT_BUDGET: list[tuple[str, str, float]] = _budget_from_config()

# Operating policy + facts , the things that must NOT be silently lost or "corrected".
_CONSTS = load_property().get("constants", {})
ANNUAL_BUDGET = Decimal(_CONSTS["annual_budget"])
HALF_YEAR_BUDGET = Decimal(_CONSTS["half_year_budget"])
DEMAND_PACK_TOTAL = Decimal(_CONSTS["demand_pack_total"])   # per-flat rounded total
ROUNDING_DELTA = DEMAND_PACK_TOTAL - HALF_YEAR_BUDGET   # +0.24
DUE_DATE_DAYS = 14                            # the real letters: issue + 14, NOT the R1 default 28
ISSUE_DATE = "2026-07-16"
DUE_DATE = "2026-07-30"

# The wording the demands still contain that the owner flagged for removal.
RECONCILIATION_SENTENCE_FLAG = (
    "A service charge statement reconciling actual costs to budget will follow within six months")

# The deed-share caveat is a fact about this site's leases, so a profile may replace the generic sample
# wording (lexicon "rtm.apportionment_note").
_APPORTIONMENT_NOTE_DEFAULT = (
    "Sample data: each block's shares sum to 50.0001% (lease rounding). Verify every flat's share against its registered lease "
    "before relying on it for demands.")


def _doc_scheme_example() -> str:
    """A demand document number for the site profile's first block, flat 1: 26-TO-DEC/<code>F1R0."""
    from .. import profile as _profile
    return f"26-TO-DEC/{_profile.block_codes()[0]}F1R0"


def _apportionment_note() -> str:
    from .. import profile as _profile
    return str(_profile.lexicon("rtm.apportionment_note", _APPORTIONMENT_NOTE_DEFAULT))


def __getattr__(name: str) -> str:
    # Historical module constants, now resolved from the site profile at use time.
    if name == "DOC_SCHEME_EXAMPLE":
        return _doc_scheme_example()
    if name == "APPORTIONMENT_NOTE":
        return _apportionment_note()
    raise AttributeError(name)


# The RTM company's own identity (director, contact, block addresses, bank) is CONFIG, not a source
# literal, for the same reason the flats/budget are. It lives in the SAME property.json and loads via
# the SAME resolver, so it shares the flats' prod/test boundary EXACTLY: the data_dir file (real) and
# the packaged asset (fictional) travel together key-for-key and can never half-swap. There is
# deliberately NO source-level fallback (real or fictional): identity is money-critical , a placeholder
# director or sort code on a real demand is a serious error , so a missing/incomplete block FAILS LOUD,
# the same discipline load_property() uses for a missing budget, never silently substituting. A fresh
# checkout gets the COMPLETE fictional block from the packaged asset; prod gets the complete real block
# from data_dir; a broken config raises rather than fabricating.
_REQUIRED_COMPANY_KEYS = ("name", "director", "tel", "town", "blocks", "bank")


def company_profile() -> dict[str, Any]:
    """The RTM company identity printed on every generated document, from property.json's `company`
    block (data_dir = real on the live machine, packaged asset = fictional, same file as the flats).
    Raises if the block is absent or incomplete: a real document must never carry placeholder identity,
    and there is no source fallback that could cross the prod/test boundary."""
    co = load_property().get("company") or {}
    missing = [k for k in _REQUIRED_COMPANY_KEYS if not co.get(k)]
    bank = co.get("bank") or {}
    if not all(bank.get(k) for k in ("name", "sort_code", "account_number")):
        missing.append("bank.{name,sort_code,account_number}")
    if missing:
        raise ValueError(
            f"property.json has no complete `company` identity block (absent: {missing}). This block "
            "carries the director, address, phone and bank printed on every RTM document; refusing to "
            "substitute placeholder identity onto a real document. Add a complete `company` block to "
            "the property.json in use (data_dir on the live machine, packaged asset for a checkout).")
    return co


def _pounds_to_pence(s: str | float) -> int:
    from decimal import ROUND_HALF_UP
    return int(Decimal(str(s)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


# --- queryable operating knowledge + caveats ---------------------------------

def operating_facts() -> dict[str, Any]:
    """The hard operating facts for Example Court, queryable any time."""
    return {
        "annual_budget": f"\u00a3{ANNUAL_BUDGET:,.2f}",
        "half_year_budget": f"\u00a3{HALF_YEAR_BUDGET:,.2f}",
        "demand_pack_total": f"\u00a3{DEMAND_PACK_TOTAL:,.2f}",
        "rounding_delta": f"\u00a3{ROUNDING_DELTA:,.2f}",
        "due_date_policy_days": DUE_DATE_DAYS,
        "issue_date": ISSUE_DATE, "due_date": DUE_DATE,
        "document_scheme_example": _doc_scheme_example(),
        "flats": len(EXAMPLE_COURT_FLATS), "budget_lines": len(EXAMPLE_COURT_BUDGET),
    }


def caveats() -> list[dict[str, Any]]:
    """The things that are NOT yet finally proven, or that must be handled a particular way. These
    are first-class so any surface (dashboard, readiness, brain) can warn before relying on the data
    operationally."""
    out: list[dict[str, Any]] = []
    out.append({"code": "ROUNDING_DELTA", "severity": "info",
                "detail": f"Per-flat demands total {DEMAND_PACK_TOTAL} vs half-year budget "
                          f"{HALF_YEAR_BUDGET} (delta {ROUNDING_DELTA}). Treat at the cl.12.12 "
                          "reconciliation, NOT as arrears or a real debt."})
    out.append({"code": "DUE_DATE_POLICY", "severity": "info",
                "detail": f"The real demands ask payment {DUE_DATE_DAYS} days after issue "
                          f"({ISSUE_DATE} -> {DUE_DATE}), not the generic R1 issue+28. Preserve the "
                          "document due date; do not 'correct' it to 28 days."})
    out.append({"code": "RECONCILIATION_WORDING_PRESENT", "severity": "warning",
                "detail": "All 18 demand letters still contain the 'statement within six months' "
                          "sentence you flagged for removal. Remove it from the template before the "
                          "next issue."})
    unverified = [f"{b}{n}" for (b, n, _s, _r, _a, note) in EXAMPLE_COURT_FLATS
                  if "verify" in note.lower() or "missing" in note.lower()]
    out.append({"code": "DEED_VERIFICATION", "severity": "warning",
                "detail": f"{len(unverified)} flats' deeds/mapping not finally proven "
                          f"({', '.join(unverified[:6])}{'...' if len(unverified) > 6 else ''}). "
                          "Verify via HMLR before treating the apportionment mapping as final. "
                          + _apportionment_note()})
    return out


# --- applying the real data into the management module ------------------------

def validate_against_management() -> dict[str, Any]:
    """Check the baked fixture is internally consistent before importing: per-flat demands sum to
    the demand-pack total, and the delta is exactly the known 24p. Catches a fixture typo."""
    total = sum(Decimal(a) for (_b, _n, _s, _r, a, _note) in EXAMPLE_COURT_FLATS)
    ok = (total == DEMAND_PACK_TOTAL)
    return {"ok": ok, "computed_total": f"\u00a3{total:,.2f}",
            "expected_total": f"\u00a3{DEMAND_PACK_TOTAL:,.2f}",
            "rounding_delta": f"\u00a3{(total - HALF_YEAR_BUDGET):,.2f}",
            "flats": len(EXAMPLE_COURT_FLATS)}


def apply_real_data(*, with_demands: bool = False) -> dict[str, Any]:
    """Load the real Example Court flats (and budget lines, and optionally the half-year demands)
    into the management module. Idempotent for flats/budget (upsert). Demands are only created if
    with_demands=True and only if not already present (never overwrites, preserves the audit trail).
    Importing a demand here does NOT mark it served; Docmail/post proof remains separate evidence.

    Returns a summary plus the caveats, so the operator sees what was loaded AND what's not yet
    proven in one place."""
    v = validate_against_management()
    if not v["ok"]:
        return {"ok": False, "reason": "fixture_inconsistent", "validation": v}
    from . import rtm_management as M

    flats_loaded = 0
    for (block, num, surname, ref, _amt, note) in EXAMPLE_COURT_FLATS:
        try:
            M.upsert_flat(block=block, flat_number=num, leaseholder_name=surname,
                          payment_reference=ref, notes=note)
            flats_loaded += 1
        except Exception:
            pass

    budget_loaded = 0
    for (label, sched, annual) in EXAMPLE_COURT_BUDGET:
        try:
            M.set_budget_line(year="2026", schedule=sched, budget_line=label, amount=annual)
            budget_loaded += 1
        except Exception:
            pass

    demands_created = 0
    if with_demands:
        for (block, num, _s, _r, amt, _note) in EXAMPLE_COURT_FLATS:
            flat_id = f"{block}F{num}"
            doc = f"26-TO-DEC/{flat_id}R0"
            try:
                existing = M.list_demands(flat=flat_id) or []
                if any(d.get("document_number") == doc for d in existing):
                    continue
                # preserve the REAL due date policy (issue + 14), not the generic +28
                M.create_demand(document_number=doc, flat=flat_id,
                                period="Half-year ending 31 Dec 2026", amount=amt,
                                issue_date=ISSUE_DATE, due_date=DUE_DATE,
                                delivery_method="Docmail", schedule="sch1")
                demands_created += 1
            except Exception:
                pass

    return {"ok": True, "flats_loaded": flats_loaded, "budget_lines_loaded": budget_loaded,
            "demands_created": demands_created, "validation": v,
            "caveats": caveats(),
            "note": "Demands imported are NOT marked served; Docmail/post proof is separate evidence."}


def knowledge_summary() -> dict[str, Any]:
    """Everything in one object, facts + caveats + validation, for the operator/Claude."""
    return {"facts": operating_facts(), "caveats": caveats(),
            "validation": validate_against_management()}
