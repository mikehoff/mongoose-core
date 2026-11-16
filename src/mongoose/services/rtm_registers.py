"""RTM registers , the durable data stores the Seller-Pack Responder reads to CLEAR its flags.

Scaffolding for spec Section 6/10: building profiles, the insurance register, the FRA/compliance
register, lease terms, and a small company register (number/VAT/freeholder that company_profile does not
carry). Each has get/set accessors and a rule layer that DERIVES the BSA/asbestos/HMO answers from the
building profile rather than guessing. Populate once (per block, per lease); reuse across every sale, so
the next sale in a block is near-instant. Empty until the director provides the data/decisions , the
seller pack then still flags those fields, but as "register ready, not populated" rather than "unknown".
"""
from __future__ import annotations

from typing import Any

from ..db import connect, utc_now_iso


def _ensure(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS rtm_building_profiles (
        block TEXT PRIMARY KEY, storeys INTEGER, height_m REAL, build_date TEXT, conversion_date TEXT,
        purpose_built INTEGER, external_wall_system TEXT, notes TEXT, updated_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS rtm_insurance_register (
        id INTEGER PRIMARY KEY AUTOINCREMENT, policy_type TEXT, insurer TEXT, broker TEXT,
        policy_number TEXT, period_start TEXT, period_end TEXT, sum_insured_pence INTEGER,
        day_one_basis TEXT, premium_pence INTEGER, who_arranges TEXT, recovered_via_service_charge INTEGER,
        placed_by TEXT, named_insured TEXT, current INTEGER DEFAULT 1, notes TEXT, updated_at TEXT)""")
    # who ARRANGED/placed the policy vs who is NAMED on it are different (a manager can place a policy that
    # names the freeholder); guarded ALTER so an existing table gains the columns without a migration tool.
    _ins_cols = {r[1] for r in con.execute("PRAGMA table_info(rtm_insurance_register)").fetchall()}
    for _c in ("placed_by", "named_insured"):
        if _c not in _ins_cols:
            con.execute(f"ALTER TABLE rtm_insurance_register ADD COLUMN {_c} TEXT")
    con.execute("""CREATE TABLE IF NOT EXISTS rtm_fra_register (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, occurred_on TEXT, outcome TEXT,
        report_status TEXT, next_due TEXT, notes TEXT, updated_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS rtm_lease_terms (
        scope TEXT, field_key TEXT, value TEXT, notes TEXT, updated_at TEXT,
        PRIMARY KEY (scope, field_key))""")
    con.execute("""CREATE TABLE IF NOT EXISTS rtm_company_register (
        field_key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)""")


# ── building profiles + the BSA/asbestos/HMO rule layer ──────────────────────────
def set_building_profile(block: str, **fields: Any) -> dict[str, Any]:
    block = (block or "").strip().upper()
    if not block:
        return {"ok": False, "error": "block required"}
    cols = ("storeys", "height_m", "build_date", "conversion_date", "purpose_built",
            "external_wall_system", "notes")
    with connect() as con:
        _ensure(con)
        cur = dict(con.execute("SELECT * FROM rtm_building_profiles WHERE block=?", (block,)).fetchone() or {})
        for k in cols:
            if k in fields:
                cur[k] = fields[k]
        con.execute(
            "INSERT OR REPLACE INTO rtm_building_profiles(block,storeys,height_m,build_date,conversion_date,"
            "purpose_built,external_wall_system,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (block, cur.get("storeys"), cur.get("height_m"), cur.get("build_date"), cur.get("conversion_date"),
             cur.get("purpose_built"), cur.get("external_wall_system"), cur.get("notes"), utc_now_iso()))
        con.commit()
    return {"ok": True, "block": block, "derivations": building_derivations(block)}


def get_building_profile(block: str) -> dict[str, Any] | None:
    with connect() as con:
        _ensure(con)
        row = con.execute("SELECT * FROM rtm_building_profiles WHERE block=?",
                          ((block or "").strip().upper(),)).fetchone()
    return dict(row) if row else None


def building_derivations(block: str) -> dict[str, Any]:
    """Derive the BSA / asbestos / HMO answers from the profile by RULE, never by guess. Returns each with
    a 'known' flag so the pack can tell a derived answer from a still-missing profile."""
    p = get_building_profile(block)
    if not p:
        return {"known": False, "reason": "no building profile for block; populate rtm_building_profiles"}
    storeys = p.get("storeys")
    height = p.get("height_m")
    # BSA leaseholder protections (Schedule 8, Building Safety Act 2022): a "relevant building" is at least
    # 11m in height OR at least 5 storeys. This (NOT the 18m/7-storey higher-risk-building test) is the
    # threshold the LPE1 BSA certificate questions turn on. Tri-state: only conclude "not relevant" when a
    # limb is KNOWN to fail. A low storey count alone cannot rule it out, because a building under 5
    # storeys can still be 11m or taller, so with the height unrecorded the answer is None (needs the
    # height), never a false N/A.
    def _over(h_limit: float, s_limit: int):
        if (height is not None and height >= h_limit) or (storeys is not None and storeys >= s_limit):
            return True
        if height is not None:  # height known and under the limit, storeys also under => definitively no
            return False
        return None  # storeys under the limit but height unrecorded => cannot conclude
    bsa = _over(11, 5)
    higher_risk = _over(18, 7)
    bsa_reason = None
    if bsa is None:
        bsa_reason = (f"storeys {storeys} is under 5 but the height (m) is not recorded; a relevant "
                      "building is >=11m OR >=5 storeys, so record the height (under 11m => not relevant).")
    # Asbestos: construction wholly after 2000 => no asbestos-containing materials duty (N/A).
    year = _year(p.get("conversion_date") or p.get("build_date"))
    asbestos_na = (year is not None and year > 2000)
    # HMO s.257: purpose-built post-2001 => No; converted pre-2001 => flag for review.
    hmo = None
    if p.get("purpose_built") and year is not None and year > 2001:
        hmo = "No (purpose-built post-2001)"
    elif p.get("purpose_built") == 0:
        hmo = "Flag: converted building, confirm s.257 HMO status"
    return {"known": True, "bsa_relevant_building": bsa, "bsa_higher_risk_building": higher_risk,
            "bsa_reason": bsa_reason, "asbestos_na": asbestos_na, "hmo_s257": hmo,
            "storeys": storeys, "height_m": height, "build_year": year}


def _year(s: Any) -> int | None:
    try:
        return int(str(s)[:4])
    except Exception:
        return None


# ── insurance register + the budget reconciliation ───────────────────────────────
def set_insurance_policy(**fields: Any) -> dict[str, Any]:
    with connect() as con:
        _ensure(con)
        con.execute(
            "INSERT INTO rtm_insurance_register(policy_type,insurer,broker,policy_number,period_start,"
            "period_end,sum_insured_pence,day_one_basis,premium_pence,who_arranges,"
            "recovered_via_service_charge,placed_by,named_insured,current,notes,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fields.get("policy_type", "buildings"), fields.get("insurer"), fields.get("broker"),
             fields.get("policy_number"), fields.get("period_start"), fields.get("period_end"),
             fields.get("sum_insured_pence"), fields.get("day_one_basis"), fields.get("premium_pence"),
             fields.get("who_arranges"), 1 if fields.get("recovered_via_service_charge") else 0,
             fields.get("placed_by"), fields.get("named_insured"),
             1 if fields.get("current", True) else 0, fields.get("notes"), utc_now_iso()))
        con.commit()
    return {"ok": True}


def list_insurance(current_only: bool = False) -> list[dict[str, Any]]:
    with connect() as con:
        _ensure(con)
        q = "SELECT * FROM rtm_insurance_register" + (" WHERE current=1" if current_only else "") + " ORDER BY id DESC"
        return [dict(r) for r in con.execute(q).fetchall()]


def insurance_reconciliation() -> dict[str, Any]:
    """Cross-check the buildings-insurance register against the budget. A policy PLACED by the managing
    agent (previous or the RTM) and recovered via the service charge is the NORMAL arrangement, even when
    the freeholder is the named insured , that is not a conflict. A genuine conflict is only a policy the
    freeholder arranged for their OWN account that is ALSO recharged (double recovery). The LPE1 point is
    the TRANSITION (who placed the current cover, to when, and the arrangement from renewal)."""
    from . import rtm_management as M
    ba = M.budget_actuals("2027") or {}
    budget_lines = [b for b in (ba.get("lines") or []) if "buildings" in str(b.get("budget_line", "")).lower()
                    and "insur" in str(b.get("budget_line", "")).lower()]
    recovered_in_budget = bool(budget_lines)
    pols = [p for p in list_insurance(current_only=True) if str(p.get("policy_type", "")).lower() == "buildings"]
    if not pols:
        return {"status": "register_empty", "recovered_in_budget": recovered_in_budget,
                "detail": "No buildings policy in the insurance register yet; populate to reconcile."}
    pol = pols[0]
    arranger = str(pol.get("who_arranges", "")).lower()
    manager_placed = any(k in arranger for k in ("manager", "agent", "harbour", "rtm", "managing"))
    end = pol.get("period_end")
    if manager_placed:
        detail = ("Buildings insurance is placed by the managing agent and recovered via the service charge "
                  "(the normal arrangement); the freeholder is the named insured, not the arranger.")
        if end:
            detail += (f" The current policy runs to {end}; represent the transition explicitly (current cover "
                       f"to {end}, then the arrangement from renewal) and never warrant currency.")
        return {"status": "ok", "recovered_in_budget": recovered_in_budget, "placed_by": pol.get("placed_by"),
                "named_insured": pol.get("named_insured"), "period_end": end, "detail": detail}
    conflict = recovered_in_budget and arranger == "freeholder"
    return {"status": "conflict" if conflict else "ok", "recovered_in_budget": recovered_in_budget,
            "who_arranges": pol.get("who_arranges"), "period_end": end,
            "detail": ("A policy the freeholder arranged for their own account is being recharged via the "
                       "service charge: check for double recovery." if conflict else
                       "Buildings insurance register and budget-recovery basis are consistent.")}


# ── FRA / compliance register ────────────────────────────────────────────────────
def set_fra(**fields: Any) -> dict[str, Any]:
    with connect() as con:
        _ensure(con)
        con.execute(
            "INSERT INTO rtm_fra_register(kind,occurred_on,outcome,report_status,next_due,notes,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (fields.get("kind", "fra"), fields.get("occurred_on"), fields.get("outcome"),
             fields.get("report_status"), fields.get("next_due"), fields.get("notes"), utc_now_iso()))
        con.commit()
    return {"ok": True}


def latest_fra(kind: str = "fra") -> dict[str, Any] | None:
    with connect() as con:
        _ensure(con)
        row = con.execute("SELECT * FROM rtm_fra_register WHERE kind=? ORDER BY occurred_on DESC, id DESC LIMIT 1",
                          (kind,)).fetchone()
    return dict(row) if row else None


# ── lease terms + company register (simple key/value) ────────────────────────────
def set_lease_term(scope: str, field_key: str, value: str, notes: str = "") -> dict[str, Any]:
    with connect() as con:
        _ensure(con)
        con.execute("INSERT OR REPLACE INTO rtm_lease_terms(scope,field_key,value,notes,updated_at) "
                    "VALUES(?,?,?,?,?)", ((scope or "").strip().upper(), field_key, value, notes, utc_now_iso()))
        con.commit()
    return {"ok": True}


def get_lease_terms(scope: str) -> dict[str, str]:
    with connect() as con:
        _ensure(con)
        rows = con.execute("SELECT field_key, value FROM rtm_lease_terms WHERE scope=?",
                           ((scope or "").strip().upper(),)).fetchall()
    return {r[0]: r[1] for r in rows}


def set_company_field(field_key: str, value: str) -> dict[str, Any]:
    with connect() as con:
        _ensure(con)
        con.execute("INSERT OR REPLACE INTO rtm_company_register(field_key,value,updated_at) VALUES(?,?,?)",
                    (field_key, value, utc_now_iso()))
        con.commit()
    return {"ok": True}


def get_company_register() -> dict[str, str]:
    with connect() as con:
        _ensure(con)
        rows = con.execute("SELECT field_key, value FROM rtm_company_register").fetchall()
    return {r[0]: r[1] for r in rows}


# ── status overview: what is populated vs still empty ────────────────────────────
def registers_status() -> dict[str, Any]:
    with connect() as con:
        _ensure(con)
        def n(t: str) -> int:
            return int(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        counts = {t: n(t) for t in ("rtm_building_profiles", "rtm_insurance_register", "rtm_fra_register",
                                    "rtm_lease_terms", "rtm_company_register")}
    return {"ok": True, "populated": counts,
            "empty": [t for t, c in counts.items() if c == 0],
            "note": "Registers scaffolded. Populate per block / per lease once; reused across every sale."}
