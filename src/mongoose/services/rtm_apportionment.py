"""RTM two-schedule apportionment engine (spec: docs/RTM_APPORTIONMENT_SPEC.md).

First-class schedules + per-flat per-schedule permille (parts per thousand), the penny-exact
largest-remainder allocator, apportionment validity, and schedule period amounts. Demand derivation,
reconciliation and credit notes build on these primitives (later spec steps) but the allocator and
stores here are self-contained and independently tested.

Money is integer pence. Shares are stored as integer PARTS PER MILLION (ppm) of a schedule, because real
lease-deed percentages carry four decimal places (e.g. 6.1667% -> 61667 ppm) that a coarser unit could
not hold exactly. Real deed percentages also do NOT sum to exactly 100% (developer rounding: Example Court
Schedule 1 sums to 100.0002%, undercroft to 100.008%), so the model treats the ppm as WEIGHTS: the
allocator apportions each schedule's total in exact proportion to the weights, penny-exact, regardless of
tiny drift. Validity is therefore a TOLERANCE check (catches a missing flat at several percent off, accepts
deed rounding within a fraction of a percent), not a demand for a perfect 1,000,000 sum.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

# 1,000,000 ppm == 100%. Deed percentages rarely sum to exactly this; within this tolerance the drift is
# deed rounding (accepted), beyond it a flat is likely missing or mis-entered (billing refused).
_BASIS_PPM = 1_000_000
_TOLERANCE_PPM = 5_000  # 0.5%


def pct_to_ppm(pct: str | float) -> int:
    """Convert a percentage (e.g. 6.1667 or '6.1667%') to integer ppm (61667). Exact via Decimal."""
    s = str(pct).strip().rstrip("%").strip()
    return int((Decimal(s) * Decimal(10_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def ppm_to_pct(ppm: int) -> str:
    """Format ppm back to a percentage string with up to 4 decimals, trimmed."""
    d = (Decimal(int(ppm)) / Decimal(10_000)).quantize(Decimal("0.0001"))
    return format(d.normalize(), "f")


# ── Penny-exact allocator (spec 6.3) ──────────────────────────────────────────

def largest_remainder(total_pence: int, weights: dict[str, int]) -> dict[str, int]:
    """Split `total_pence` across keys in proportion to integer `weights` (permille that sum to 1000),
    returning integer pence that sum EXACTLY to `total_pence`.

    Deterministic: remainder pennies go to the largest fractional remainders, ties broken by key ASC, so
    the same input always yields the same allocation and an independent recomputation agrees.
    """
    total_pence = int(total_pence)
    keys = list(weights.keys())
    if not keys:
        return {}
    wsum = sum(int(weights[k]) for k in keys)
    if wsum <= 0:
        return {k: 0 for k in keys}
    # exact_k = total * w_k ; base_k = exact_k // wsum ; rem_k = exact_k % wsum. Using wsum (not a hard
    # 1000) keeps the allocator correct even for a partial/invalid set, though callers gate on validity.
    base: dict[str, int] = {}
    rem: dict[str, int] = {}
    for k in keys:
        exact = total_pence * int(weights[k])
        base[k] = exact // wsum
        rem[k] = exact % wsum
    allocated = sum(base.values())
    shortfall = total_pence - allocated  # pennies still to place; 0 <= shortfall < len(keys) when wsum==total's basis
    if shortfall > 0:
        order = sorted(keys, key=lambda k: (-rem[k], k))
        for k in order[:shortfall]:
            base[k] += 1
    elif shortfall < 0:
        # Only possible if weights sum to more than the basis (defensive); remove from smallest remainders.
        order = sorted(keys, key=lambda k: (rem[k], k))
        for k in order[: -shortfall]:
            base[k] -= 1
    return base


def _round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def period_pence(year_pence: int, numerator: int, denominator: int) -> int:
    """Pence drawn for a period that takes `numerator/denominator` of the annual amount (half-year=1/2)."""
    if denominator <= 0:
        return 0
    return _round_half_up(Decimal(int(year_pence)) * Decimal(int(numerator)) / Decimal(int(denominator)))


# ── Schema ────────────────────────────────────────────────────────────────────

_DB_VERSION = "1"


def _ensure() -> None:
    from ..db import connect
    with connect() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS rtm_mgmt_schedules (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                apportionment_basis TEXT NOT NULL DEFAULT '',
                is_reserve_linked INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS rtm_mgmt_flat_apportionment (
                flat_id TEXT NOT NULL,
                schedule TEXT NOT NULL,
                ppm INTEGER NOT NULL,
                source_clause TEXT NOT NULL DEFAULT '',
                effective_from TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (flat_id, schedule),
                CHECK (ppm >= 0 AND ppm <= 1000000)
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_flat_appn_schedule ON rtm_mgmt_flat_apportionment(schedule)")
        # Credit notes (spec 4.6).
        con.execute(
            """CREATE TABLE IF NOT EXISTS rtm_mgmt_credit_notes (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                flat_id TEXT NOT NULL,
                schedule TEXT NOT NULL,
                year TEXT NOT NULL,
                period TEXT NOT NULL,
                ppm INTEGER NOT NULL,
                pool_pence INTEGER NOT NULL,
                amount_pence INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                pdf_key TEXT,
                issued_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_credit_flat ON rtm_mgmt_credit_notes(flat_id)")
        # Additive columns on rtm_management's tables (owned there, extended here per spec 4.4/4.5/4.6).
        dcols = {r[1] for r in con.execute("PRAGMA table_info(rtm_mgmt_demands)").fetchall()}
        if dcols:  # table exists (rtm_management._ensure_tables ran)
            if "ppm" not in dcols:
                con.execute("ALTER TABLE rtm_mgmt_demands ADD COLUMN ppm INTEGER")
            if "period_fraction" not in dcols:
                con.execute("ALTER TABLE rtm_mgmt_demands ADD COLUMN period_fraction TEXT")
            if "derivation_run_id" not in dcols:
                con.execute("ALTER TABLE rtm_mgmt_demands ADD COLUMN derivation_run_id TEXT")
        pcols = {r[1] for r in con.execute("PRAGMA table_info(rtm_mgmt_payables)").fetchall()}
        if pcols and "budget_line_id" not in pcols:
            con.execute("ALTER TABLE rtm_mgmt_payables ADD COLUMN budget_line_id INTEGER")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_budget_line ON rtm_mgmt_budget_lines(year, schedule, budget_line)")
        con.commit()


def _new_id(prefix: str) -> str:
    import uuid
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _pounds(pence: int) -> str:
    """Exact pounds string for an integer pence value, safe to round-trip through _money_to_pence."""
    pence = int(pence)
    sign = "-" if pence < 0 else ""
    pence = abs(pence)
    return f"{sign}{pence // 100}.{pence % 100:02d}"


def _credit_note_dir():
    """Directory for stored credit-note PDFs, under the data home alongside the brain."""
    import os
    from pathlib import Path
    try:
        from ..config import get_settings
        base = Path(get_settings().db_path).resolve().parent
    except Exception:
        base = Path(os.environ.get("MONGOOSE_DATA_DIR", ".")).resolve()
    d = base / "portal" / "credit_notes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def seed_default_schedules() -> None:
    """Seed sch1/sch2 (+ utility rows for legacy tags so existing data never orphans). Idempotent."""
    from ..db import connect, utc_now_iso
    _ensure()
    rows = [
        ("sch1", "Building service costs", "Whole-building services shared by all flats.", "", 1, 1, 1),
        ("sch2", "Undercroft parking", "Undercroft/parking services shared by parking flats only.", "", 0, 2, 1),
        ("reserve", "Reserve fund", "Reserve fund movements.", "", 1, 3, 0),
        ("one_off", "One-off / project", "One-off or Section 20 project costs.", "", 0, 4, 0),
        ("company_admin", "Company administration", "RTM company administration.", "", 0, 5, 0),
        ("unknown", "Unclassified", "Legacy/unclassified tag.", "", 0, 9, 0),
    ]
    now = utc_now_iso()
    with connect() as con:
        for code, name, desc, basis, reserve, order, active in rows:
            con.execute(
                "INSERT INTO rtm_mgmt_schedules(code,name,description,apportionment_basis,is_reserve_linked,sort_order,active,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(code) DO NOTHING",
                (code, name, desc, basis, reserve, order, active, now, now),
            )
        con.commit()


# ── Schedule CRUD ──────────────────────────────────────────────────────────────

def upsert_schedule(*, code: str, name: str, description: str = "", apportionment_basis: str = "",
                    is_reserve_linked: bool = False, sort_order: int = 0, active: bool = True) -> dict[str, Any]:
    from ..db import connect, utc_now_iso
    code = (code or "").strip().lower()
    if not code or not (name or "").strip():
        return {"ok": False, "error": "code and name required"}
    _ensure()
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            "INSERT INTO rtm_mgmt_schedules(code,name,description,apportionment_basis,is_reserve_linked,sort_order,active,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name, description=excluded.description, "
            "apportionment_basis=excluded.apportionment_basis, is_reserve_linked=excluded.is_reserve_linked, "
            "sort_order=excluded.sort_order, active=excluded.active, updated_at=excluded.updated_at",
            (code, name.strip(), description, apportionment_basis, 1 if is_reserve_linked else 0,
             int(sort_order), 1 if active else 0, now, now),
        )
        con.commit()
    return {"ok": True, "code": code}


def list_schedules(*, active_only: bool = False) -> list[dict[str, Any]]:
    from ..db import connect
    _ensure()
    q = "SELECT code,name,description,apportionment_basis,is_reserve_linked,sort_order,active FROM rtm_mgmt_schedules"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY sort_order, code"
    with connect() as con:
        return [dict(r) for r in con.execute(q).fetchall()]


# ── Per-flat per-schedule apportionment ─────────────────────────────────────────

def set_apportionment(*, flat_id: str, schedule: str, ppm: int | None = None, pct: str | float | None = None,
                      source_clause: str = "", effective_from: str = "", note: str = "") -> dict[str, Any]:
    """Set a flat's share of a schedule, as ppm (parts per million) or a percentage. 0 removes
    participation (kept as a 0 row for provenance)."""
    from ..db import connect, utc_now_iso
    fid = (flat_id or "").strip().upper().replace("-", "")
    sch = (schedule or "").strip().lower()
    if not fid or not sch:
        return {"ok": False, "error": "flat_id and schedule required"}
    if ppm is None and pct is not None:
        ppm = pct_to_ppm(pct)
    if ppm is None:
        return {"ok": False, "error": "ppm or pct required"}
    p = int(ppm)
    if p < 0 or p > _BASIS_PPM:
        return {"ok": False, "error": "ppm must be 0..1000000"}
    _ensure()
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            "INSERT INTO rtm_mgmt_flat_apportionment(flat_id,schedule,ppm,source_clause,effective_from,note,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(flat_id,schedule) DO UPDATE SET ppm=excluded.ppm, "
            "source_clause=excluded.source_clause, effective_from=excluded.effective_from, note=excluded.note, updated_at=excluded.updated_at",
            (fid, sch, p, source_clause, effective_from, note, now, now),
        )
        con.commit()
    return {"ok": True, "flat_id": fid, "schedule": sch, "ppm": p, "pct": ppm_to_pct(p)}


def participants(schedule: str) -> dict[str, int]:
    """flat_id -> ppm for flats participating (ppm > 0) in a schedule."""
    from ..db import connect
    _ensure()
    sch = (schedule or "").strip().lower()
    with connect() as con:
        return {str(r[0]): int(r[1]) for r in con.execute(
            "SELECT flat_id, ppm FROM rtm_mgmt_flat_apportionment WHERE schedule=? AND ppm>0", (sch,)).fetchall()}


def flat_apportionment(flat_id: str) -> dict[str, int]:
    """schedule -> ppm for one flat (only its non-zero shares)."""
    from ..db import connect
    _ensure()
    fid = (flat_id or "").strip().upper().replace("-", "")
    with connect() as con:
        return {str(r[0]): int(r[1]) for r in con.execute(
            "SELECT schedule, ppm FROM rtm_mgmt_flat_apportionment WHERE flat_id=? AND ppm>0", (fid,)).fetchall()}


def validate_schedule(schedule: str) -> dict[str, Any]:
    """Apportionment integrity for one schedule (spec R10/R18, amended for real deed data). Participating
    ppm should sum to ~1,000,000; within _TOLERANCE_PPM the drift is deed rounding (OK), beyond it a flat
    is likely missing or mis-entered (billing refused). Billing normalises by the actual sum regardless."""
    p = participants(schedule)
    total = sum(p.values())
    delta = total - _BASIS_PPM
    return {"schedule": (schedule or "").strip().lower(), "participant_count": len(p),
            "ppm_sum": total, "pct_sum": ppm_to_pct(total),
            "ok": abs(delta) <= _TOLERANCE_PPM, "delta_ppm": delta,
            "within_tolerance": abs(delta) <= _TOLERANCE_PPM, "tolerance_ppm": _TOLERANCE_PPM}


def validate_all(*, active_only: bool = True) -> dict[str, Any]:
    """Validity across schedules. A schedule with no participants is reported as empty (not billable)."""
    out = []
    for s in list_schedules(active_only=active_only):
        v = validate_schedule(s["code"])
        v["name"] = s["name"]
        v["empty"] = v["participant_count"] == 0
        out.append(v)
    return {"ok": all(v["ok"] or v["empty"] for v in out), "schedules": out}


# ── Schedule budgeted amount + period amount (spec 6.1) ─────────────────────────

def schedule_year_pence(schedule: str, year: str) -> int:
    """Sum of a schedule's budget lines for a financial year (from rtm_mgmt_budget_lines)."""
    from ..db import connect
    sch = (schedule or "").strip().lower()
    with connect() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(amount_pence),0) s FROM rtm_mgmt_budget_lines WHERE year=? AND schedule=?",
            (str(year), sch)).fetchone()
    return int(row[0] if row else 0)


def schedule_period_pence(schedule: str, year: str, numerator: int = 1, denominator: int = 2) -> int:
    """Pence a schedule draws for a period (default half-year = 1/2)."""
    return period_pence(schedule_year_pence(schedule, year), numerator, denominator)


def apportion_schedule_period(schedule: str, year: str, numerator: int = 1, denominator: int = 2) -> dict[str, Any]:
    """The penny-exact per-flat charge for a schedule/period, or a refusal if the schedule is invalid.

    Returns {ok, schedule, period_pence, shares:{flat->pence}, permille:{flat->permille}} on success, or
    {ok:False, error, validity} when participating permille do not sum to 1000 (billing refused, spec R10)."""
    v = validate_schedule(schedule)
    p = participants(schedule)
    if not p:
        return {"ok": False, "error": "no participants for schedule", "validity": v}
    if not v["ok"]:
        return {"ok": False,
                "error": f"apportionment invalid: ppm sum {v['ppm_sum']} ({v['pct_sum']}%) is {v['delta_ppm']:+d} "
                         f"ppm off 1,000,000, beyond the {v['tolerance_ppm']} ppm tolerance (likely a missing flat)",
                "validity": v}
    sp = schedule_period_pence(schedule, year, numerator, denominator)
    # Allocator normalises by the actual ppm sum, so per-flat charges are the deed proportion of the
    # schedule total and sum penny-exact even when deed percentages carry rounding drift.
    shares = largest_remainder(sp, p)
    return {"ok": True, "schedule": (schedule or "").strip().lower(), "period_pence": sp,
            "shares": shares, "ppm": p}


# ── Demand derivation (spec 6.4, step 5) ────────────────────────────────────────

def _live_demand(con, flat_id: str, period: str, schedule: str):
    """The current (not superseded) apportioned demand for (flat, period, schedule), or None."""
    return con.execute(
        "SELECT document_number, revision, amount_pence FROM rtm_mgmt_demands "
        "WHERE flat_id=? AND period=? AND schedule=? AND status IN ('Issued','Part-paid','Paid','Overdue') "
        "ORDER BY revision DESC LIMIT 1", (flat_id, period, schedule)).fetchone()


def _deed_pence(ppm: int, amount_pence: int) -> int:
    """A flat's share at its exact lease percentage, simple-rounded (deed-exact; matches the served
    demand documents). NOT the normalised largest-remainder used for schedule-total reconciliation."""
    from decimal import Decimal, ROUND_HALF_UP
    return int((Decimal(int(ppm)) / Decimal(1_000_000) * Decimal(int(amount_pence))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def derive_demands(*, year: str, period: str, numerator: int = 1, denominator: int = 2,
                   doc_prefix: str = "26-TO-DEC", issue_date: str | None = None,
                   due_date: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Issue ONE demand per flat for a period, covering BOTH schedules in a single document, using the
    DEED-EXACT method (each flat's exact lease percentage, simple-rounded), so the ledger matches the
    served demand documents exactly. Idempotent per (flat, period): a re-run supersedes the flat's prior
    demand (reversing its ledger debit), never double-charging. Refuses if any schedule's apportionment
    is invalid. Deed-exact means the total demanded is a few pence over the budget (real over-recovery
    from the deed percentages not summing to exactly 100%), reconciled at year end."""
    from .rtm_management import create_demand, _money
    from ..db import connect
    _ensure()
    run_id = _new_id("aprun")
    frac = f"{numerator}/{denominator}"
    schedules = [s for s in list_schedules(active_only=True) if participants(s["code"])]
    refused = [{"schedule": s["code"], "error": f"apportionment invalid ({validate_schedule(s['code'])['pct_sum']}%)"}
               for s in schedules if not validate_schedule(s["code"])["ok"]]
    if refused:
        return {"ok": False, "run_id": run_id, "year": year, "period": period, "created": 0,
                "superseded": 0, "refused": refused}
    sched_period = {s["code"]: schedule_period_pence(s["code"], year, numerator, denominator) for s in schedules}
    sched_names = {s["code"]: s["name"] for s in schedules}
    # flat -> {schedule: ppm}
    flat_ppm: dict[str, dict[str, int]] = {}
    for s in schedules:
        for flat_id, ppm in participants(s["code"]).items():
            flat_ppm.setdefault(flat_id, {})[s["code"]] = ppm
    created, superseded, demanded_total = 0, 0, 0
    preview = []
    for flat_id in sorted(flat_ppm):
        parts, total = [], 0
        for code in sched_period:
            ppm = flat_ppm[flat_id].get(code, 0)
            if ppm <= 0:
                continue
            pence = _deed_pence(ppm, sched_period[code])
            parts.append((code, ppm, pence))
            total += pence
        with connect() as con:
            prior = con.execute(
                "SELECT document_number, revision FROM rtm_mgmt_demands WHERE flat_id=? AND period=? "
                "AND status NOT IN ('Superseded','Cancelled') ORDER BY revision DESC LIMIT 1",
                (flat_id, period)).fetchone()
        supersedes = prior["document_number"] if prior else ""
        revision = (int(prior["revision"]) + 1) if prior else 0
        doc = f"{doc_prefix}/{flat_id}R{revision}"
        note = " + ".join(f"{sched_names.get(c, c)} {ppm_to_pct(p)}% {_money(pc)}" for c, p, pc in parts)
        preview.append({"flat_id": flat_id, "document_number": doc, "amount_pence": total,
                        "amount": _money(total), "supersedes": supersedes, "note": note})
        demanded_total += total
        # DRY RUN: compute and collect the exact figures (same deed-exact path) but write nothing.
        if dry_run:
            continue
        create_demand(document_number=doc, flat=flat_id, period=period, amount=_pounds(total),
                      schedule="combined", supersedes=supersedes, revision=revision,
                      issue_date=issue_date, due_date=due_date, notes=note)
        with connect() as con:
            con.execute("UPDATE rtm_mgmt_demands SET period_fraction=?, derivation_run_id=? WHERE document_number=?",
                        (frac, run_id, doc))
            con.commit()
        created += 1
        if supersedes:
            superseded += 1
    return {"ok": True, "run_id": run_id, "year": year, "period": period, "fraction": frac,
            "dry_run": dry_run, "created": created, "superseded": superseded,
            "flats": len(preview), "preview": preview,
            "demanded_total_pence": demanded_total, "demanded_total": _money(demanded_total),
            "issue_date": issue_date, "due_date": due_date,
            "schedule_period": {c: _money(v) for c, v in sched_period.items()}}


# ── Reconciliation (spec 6.5 + R17-R21, step 7) ─────────────────────────────────

def schedule_spend(schedule: str, year: str) -> dict[str, int]:
    """Paid + committed spend for a schedule from payables, preferring the budget_line_id FK and
    falling back to the legacy (schedule, budget_line) text match for un-migrated rows (spec 6.5)."""
    from ..db import connect
    sch = (schedule or "").strip().lower()
    with connect() as con:
        paid = con.execute(
            "SELECT COALESCE(SUM(paid_amount_pence),0) FROM rtm_mgmt_payables WHERE schedule=? AND status='Paid'",
            (sch,)).fetchone()[0] or 0
        committed = con.execute(
            "SELECT COALESCE(SUM(amount_pence),0) FROM rtm_mgmt_payables WHERE schedule=? AND status IN ('Scheduled','Approved')",
            (sch,)).fetchone()[0] or 0
    return {"paid_pence": int(paid), "committed_pence": int(committed)}


def demanded_for_schedule(schedule: str, year: str, period: str | None = None,
                          numerator: int = 1, denominator: int = 2) -> int:
    """Pence demanded for a schedule this period. Demands are issued as ONE combined document per flat
    (tagged 'combined'), so the per-schedule figure is the DEED-EXACT sum across participating flats
    (what each combined demand actually included for the schedule) whenever demands exist for the period.
    Returns 0 when nothing has been demanded yet."""
    from ..db import connect
    sch = (schedule or "").strip().lower()
    # Are there live demands for the period at all?
    q0 = "SELECT COUNT(*) FROM rtm_mgmt_demands WHERE status NOT IN ('Superseded','Cancelled')"
    args0: list[Any] = []
    if period:
        q0 += " AND period=?"
        args0.append(period)
    with connect() as con:
        has_demands = int(con.execute(q0, args0).fetchone()[0] or 0) > 0
        # Legacy per-schedule rows (pre-combined-model) if any still exist.
        legacy = int(con.execute(
            "SELECT COALESCE(SUM(amount_pence),0) FROM rtm_mgmt_demands WHERE schedule=? AND status NOT IN ('Superseded','Cancelled')"
            + (" AND period=?" if period else ""), ([sch] + ([period] if period else []))).fetchone()[0] or 0)
    if legacy:
        return legacy
    if not has_demands:
        return 0
    sp = schedule_period_pence(sch, year, numerator, denominator)
    return sum(_deed_pence(int(ppm), sp) for ppm in participants(sch).values())


def _demanded_legacy(schedule: str, year: str, period: str | None = None) -> int:
    from ..db import connect
    sch = (schedule or "").strip().lower()
    q = ("SELECT COALESCE(SUM(amount_pence),0) FROM rtm_mgmt_demands WHERE schedule=? "
         "AND status NOT IN ('Superseded','Cancelled')")
    args = [sch]
    if period:
        q += " AND period=?"
        args.append(period)
    with connect() as con:
        return int(con.execute(q, args).fetchone()[0] or 0)


def reconcile(*, year: str, period: str, numerator: int = 1, denominator: int = 2,
              published_period_total_pence: int | None = None) -> dict[str, Any]:
    """Run the reconciliation invariants (spec R17-R21) and return pass/fail with the offending deltas."""
    checks = []
    schedules = list_schedules(active_only=True)
    period_total = 0
    for s in schedules:
        code = s["code"]
        v = validate_schedule(code)
        if v["participant_count"] == 0:
            continue
        sp = schedule_period_pence(code, year, numerator, denominator)
        period_total += sp
        demanded = demanded_for_schedule(code, year, period)
        spend = schedule_spend(code, year)
        actual = spend["paid_pence"] + spend["committed_pence"]
        # R18 apportionment integrity (tolerance); deed-exact billing intentionally OVER-recovers the
        # budget by a few pence (deed percentages do not sum to exactly 100%), so we REPORT that drift
        # rather than treat it as a failure. R21 spend vs budget.
        over = (demanded - sp) if demanded else 0
        checks.append({
            "schedule": code, "name": s["name"],
            "apportionment_ok": v["ok"], "apportionment_pct": v["pct_sum"], "apportionment_delta_ppm": v["delta_ppm"],
            "schedule_period_pence": sp, "demanded_pence": demanded,
            "over_recovery_pence": over,  # deed-exact drift vs budget (expected, reconciled at year end)
            "paid_pence": spend["paid_pence"], "committed_pence": spend["committed_pence"],
            "variance_pence": sp - actual,  # period budget vs actual+committed
            "creditable_pence": max(0, (demanded or sp) - actual),
        })
    # R20 cross-schedule total (of the budget, not the deed-exact demanded which carries the drift).
    total_ok = (published_period_total_pence is None) or (period_total == int(published_period_total_pence))
    ok = all(c["apportionment_ok"] for c in checks) and total_ok
    return {"ok": ok, "year": year, "period": period, "period_total_pence": period_total,
            "published_period_total_pence": published_period_total_pence, "cross_total_ok": total_ok,
            "schedules": checks}


# ── Credit notes (spec 6.6 + R23-R27, step 8) ───────────────────────────────────

def credit_note_run(*, schedule: str, year: str, period: str, actual_pence: int,
                    numerator: int = 1, denominator: int = 2, reason: str = "",
                    demanded_pence: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Issue schedule-level credit notes for an over-recovered schedule (spec 6.6).

    credit_pool = max(0, demanded - actual); split across participating flats by the SAME ppm that raised
    the demand, penny-exact; each flat's credit posts a ledger CREDIT (kind='credit_note') and a
    rtm_mgmt_credit_notes row. Non-participants (ppm 0) never receive a credit. A deficit (actual >
    demanded) yields NO credit (a balancing charge is a separate, deliberate action)."""
    from ..db import connect, utc_now_iso
    from .rtm_management import _ledger_entry
    sch = (schedule or "").strip().lower()
    v = validate_schedule(sch)
    p = participants(sch)
    if not p:
        return {"ok": False, "error": "no participants for schedule"}
    if not v["ok"]:
        return {"ok": False, "error": f"apportionment invalid ({v['pct_sum']}%); refusing to credit", "validity": v}
    demanded = int(demanded_pence) if demanded_pence is not None else (
        demanded_for_schedule(sch, year, period) or schedule_period_pence(sch, year, numerator, denominator))
    pool = max(0, demanded - int(actual_pence))
    credits = largest_remainder(pool, p) if pool > 0 else {k: 0 for k in p}
    run_id = _new_id("cnrun")
    issued = utc_now_iso()[:10]
    lines = []
    if not dry_run and pool > 0:
        from .rtm_management import _get_flat
        for flat_id, amt in credits.items():
            if amt <= 0:
                continue
            cn_id = _new_id("cn")
            row = {"id": cn_id, "flat_id": flat_id, "schedule": sch, "year": str(year), "period": period,
                   "ppm": int(p[flat_id]), "pool_pence": pool, "amount_pence": int(amt), "reason": reason,
                   "issued_date": issued}
            # Branded credit-note PDF (spec R26), rendered via the shared reportlab document engine.
            pdf_key = None
            try:
                import re as _re
                from .rtm_apportionment_docs import render_credit_note_pdf
                cn_dir = _credit_note_dir()
                fl = _get_flat(flat_id) or {}
                from .. import profile as _profile
                m = _re.fullmatch(rf"({_profile.block_code_regex()})F(\d+)", flat_id)
                render_credit_note_pdf(out_path=str(cn_dir / f"{cn_id}.pdf"), credit_note=row,
                                       leaseholder_name=fl.get("leaseholder_name", ""),
                                       block=(m.group(1) if m else ""), flat_number=(int(m.group(2)) if m else ""))
                pdf_key = f"{cn_id}.pdf"
            except Exception:
                pdf_key = None
            with connect() as con:
                con.execute(
                    "INSERT INTO rtm_mgmt_credit_notes(id,run_id,flat_id,schedule,year,period,ppm,pool_pence,amount_pence,reason,pdf_key,issued_date,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cn_id, run_id, flat_id, sch, str(year), period, int(p[flat_id]), pool, int(amt),
                     reason, pdf_key, issued, utc_now_iso()),
                )
                con.commit()
            _ledger_entry(flat_id=flat_id, entry_date=issued, kind="credit_note",
                          description=f"Credit note {cn_id}: Schedule {sch} over-recovery {period}",
                          credit_pence=int(amt), document_number=cn_id, schedule=sch,
                          note=f"Credit of {ppm_to_pct(p[flat_id])}% of the £{pool/100:,.2f} Schedule {sch} surplus.")
            lines.append({"flat_id": flat_id, "ppm": int(p[flat_id]), "amount_pence": int(amt),
                          "amount": _money_str(amt), "credit_note_id": cn_id, "pdf_key": pdf_key})
    else:
        for flat_id, amt in credits.items():
            if amt > 0:
                lines.append({"flat_id": flat_id, "ppm": int(p[flat_id]), "amount_pence": int(amt),
                              "amount": _money_str(amt)})
    return {"ok": True, "run_id": run_id, "schedule": sch, "year": year, "period": period,
            "demanded_pence": demanded, "actual_pence": int(actual_pence), "pool_pence": pool,
            "pool": _money_str(pool), "credited_pence": sum(l["amount_pence"] for l in lines),
            "lines": lines, "dry_run": dry_run,
            "note": "deficit: actual exceeds demanded, no credit (a balancing charge is separate)" if pool == 0 and int(actual_pence) > demanded else ""}


def _money_str(pence: int) -> str:
    from .rtm_management import _money
    return _money(int(pence))


def credit_notes_for_flat(flat_id: str) -> list[dict[str, Any]]:
    from ..db import connect
    _ensure()
    fid = (flat_id or "").strip().upper().replace("-", "")
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT id, schedule, year, period, amount_pence, issued_date FROM rtm_mgmt_credit_notes "
            "WHERE flat_id=? ORDER BY issued_date DESC", (fid,)).fetchall()]
