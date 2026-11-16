"""Example Court RTM. Receipts and trust reconciliation on the turnstile model.

This module records and checks; it never moves money. Every actual bank transfer is made by
the director by hand. Nothing here issues, confirms, or simulates a bank instruction. The whole
surface is read / record / report only. That is a hard guarantee, asserted by a test.

The turnstile (Layer 2 section 1):

  running                -> the only world-facing account. Leaseholders pay in; suppliers paid out.
  service_charge_trust   -> ring-fenced trust holding for service charge monies (s.42 LTA 1987).
  reserve_fund_trust     -> ring-fenced trust holding for the reserve fund. (The spec calls this
                            "reserve_trust"; the existing bookkeeper already names the fund
                            "reserve_fund_trust", so that is the canonical ledger account here and
                            "reserve"/"reserve_trust" are accepted as input aliases. We extend the
                            existing trust ledger, we do not fork a second one.)

running is target-zero: a turnstile, not a store. Service charge money landing in running is
"exposed" until swept to a trust account, and trust money is never reported as available operating
cash. The trust accounts only ever exchange money with running, never with each other-as-outside and
never with the world.

Design note on commingling (guardrail 3). Trust movements continue to flow through the existing
trust-only bookkeeper (``rtm_bookkeeper``), whose fund set deliberately excludes ``running``. The
world-facing ``running`` account is tracked here in its own table. Keeping running out of the trust
ledger at the data layer is what makes commingling structurally hard rather than merely discouraged.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from pathlib import Path
from typing import Any

from .. import profile as _profile
from ..db import connect, utc_now_iso
from . import rtm_bookkeeper as BK
from . import rtm_management as M
from . import rtm_example_court as PC

# --- account model -----------------------------------------------------------

RUNNING = "running"
SERVICE_TRUST = "service_charge_trust"
RESERVE_TRUST = "reserve_fund_trust"   # canonical; "reserve"/"reserve_trust" are aliases

# Every ledger account and whether it is held on trust. running is the only non-trust, only
# world-facing account. "{rtm}" in a bank account name is the RTM company's name from the site profile,
# filled by accounts().
ACCOUNTS: dict[str, dict[str, Any]] = {
    RUNNING: {
        "trust": False,
        "world_facing": True,
        "bank_account_name": "{rtm} RTM Running Account",
        "role": "World-facing turnstile. All leaseholder income in, all supplier payments out. Target-zero.",
    },
    SERVICE_TRUST: {
        "trust": True,
        "world_facing": False,
        "bank_account_name": 'Metro "{rtm} RTM Service Charge"',
        "role": "Ring-fenced service charge trust (s.42 LTA 1987). Never faces the outside.",
    },
    RESERVE_TRUST: {
        "trust": True,
        "world_facing": False,
        "bank_account_name": 'Metro "{rtm} RTM Reserve"',
        "role": "Ring-fenced reserve fund trust. Never faces the outside.",
    },
}

# Grace window for trust money resting in the world-facing account before it is "overdue".
# Tight on purpose: this is trust money in a non-trust account.
TRUST_EXPOSURE_GRACE_DAYS = 7
# How close in time a debit and its matching credit must appear to be tagged as one sweep.
SWEEP_MATCH_WINDOW_DAYS = 2


def _account(name: str) -> str:
    """Resolve an account name, accepting the spec's aliases. running is recognised here even though
    the trust-only bookkeeper does not know it."""
    f = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "run": RUNNING, "running": RUNNING, "operational": RUNNING, "operating": RUNNING,
        "service": SERVICE_TRUST, "service_charge": SERVICE_TRUST, "service_charge_trust": SERVICE_TRUST,
        "reserve": RESERVE_TRUST, "reserve_fund": RESERVE_TRUST, "reserve_trust": RESERVE_TRUST,
        "reserve_fund_trust": RESERVE_TRUST,
    }
    f = aliases.get(f, f)
    if f not in ACCOUNTS:
        raise ValueError(f"account must be one of {sorted(ACCOUNTS)} (got {name!r})")
    return f


def is_trust(account: str) -> bool:
    return ACCOUNTS[_account(account)]["trust"]


def accounts() -> list[dict[str, Any]]:
    """The three ledger accounts with their trust tag and role. running is the only world-facing one."""
    out = []
    rtm = _profile.rtm_name()
    for name, meta in ACCOUNTS.items():
        out.append({"account": name, **meta,
                    "bank_account_name": str(meta.get("bank_account_name", "")).replace("{rtm}", rtm)})
    return out


# --- money helpers (mirror the management/bookkeeper Decimal discipline) ------

def _money_to_pence(amount: str | float | int) -> int:
    if isinstance(amount, int):
        return amount * 100
    try:
        d = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"bad money value {amount!r}") from e
    return int((d * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _money(pence: int | None) -> str:
    if pence is None:
        return ""
    neg = pence < 0
    p = abs(int(pence))
    s = f"£{p // 100:,}.{p % 100:02d}"
    return f"-{s}" if neg else s


def _today() -> str:
    return date.today().isoformat()


def _day(value: str | None) -> str:
    if not value:
        return _today()
    return date.fromisoformat(str(value)[:10]).isoformat()


# --- tables ------------------------------------------------------------------

def _ensure_tables() -> None:
    with connect() as con:
        # World-facing running-account movements. Deliberately separate from the trust ledger.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS rtm_trust_running (
                id TEXT PRIMARY KEY,
                occurred_on TEXT NOT NULL,
                direction TEXT NOT NULL,          -- in | out | opening
                amount_pence INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'uncategorised',  -- income | supplier_payment | sweep_in | sweep_out | opening
                trust_tagged INTEGER NOT NULL DEFAULT 0,     -- 1 if this is identified trust money sitting in running
                flat_id TEXT,
                document_number TEXT,
                counterparty TEXT DEFAULT '',
                reference TEXT DEFAULT '',
                transfer_group TEXT DEFAULT '',
                source TEXT DEFAULT 'manual',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_rtm_trust_running_date ON rtm_trust_running(occurred_on, created_at)")
        # Reserve liability: the running tally of reserve money collected vs landed-in-reserve.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS rtm_trust_reserve_ledger (
                id TEXT PRIMARY KEY,
                occurred_on TEXT NOT NULL,
                movement TEXT NOT NULL,           -- collected | transferred
                amount_pence INTEGER NOT NULL,
                flat_id TEXT,
                document_number TEXT,
                transfer_group TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_rtm_trust_reserve_date ON rtm_trust_reserve_ledger(occurred_on, created_at)")
        # Receipts issued (one per demand that reaches zero outstanding).
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS rtm_trust_receipts (
                receipt_number TEXT PRIMARY KEY,
                flat_id TEXT NOT NULL,
                document_number TEXT NOT NULL,
                payment_ref TEXT DEFAULT '',
                amount_pence INTEGER NOT NULL,
                received_date TEXT NOT NULL,
                issue_date TEXT NOT NULL,
                method TEXT DEFAULT 'Bank transfer',
                period TEXT DEFAULT '',
                leaseholder TEXT DEFAULT '',
                address TEXT DEFAULT '',
                premises TEXT DEFAULT '',
                pdf_path TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )


# --- bank feed (per-account observed lines, SHA-256 de-duplicated) ------------
# The feeds are what the bank actually did. Reconciliation compares them to what the ledger says
# and to the sweeps that should have happened. Feed ingestion records only; it moves nothing.

def _ensure_feed_table() -> None:
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS rtm_trust_feed (
                id TEXT PRIMARY KEY,
                account TEXT NOT NULL,
                occurred_on TEXT NOT NULL,
                direction TEXT NOT NULL,          -- in | out
                amount_pence INTEGER NOT NULL,
                description TEXT DEFAULT '',
                counterparty TEXT DEFAULT '',
                sha256 TEXT NOT NULL UNIQUE,
                matched_group TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_rtm_trust_feed_acct ON rtm_trust_feed(account, occurred_on)")


def _feed_sha(account: str, occurred_on: str, direction: str, amount_pence: int, description: str) -> str:
    import hashlib
    key = f"{account}|{occurred_on}|{direction}|{amount_pence}|{description}".encode("utf-8")
    return hashlib.sha256(key).hexdigest()


def ingest_feed_line(*, account: str, occurred_on: str, direction: str,
                     amount: str | float | int, description: str = "",
                     counterparty: str = "") -> dict[str, Any]:
    """Record one observed bank line. De-duplicated by SHA-256 so re-ingesting a CSV is safe."""
    _ensure_feed_table()
    account = _account(account)
    direction = (direction or "").strip().lower()
    if direction not in {"in", "out"}:
        raise ValueError("direction must be 'in' or 'out'")
    occurred_on = _day(occurred_on)
    pence = abs(_money_to_pence(amount))
    sha = _feed_sha(account, occurred_on, direction, pence, description)
    with connect() as con:
        exists = con.execute("SELECT 1 FROM rtm_trust_feed WHERE sha256=?", (sha,)).fetchone()
        if exists:
            return {"ok": True, "duplicate": True, "sha256": sha}
        fid = "feed_" + uuid.uuid4().hex[:12]
        con.execute(
            "INSERT INTO rtm_trust_feed(id,account,occurred_on,direction,amount_pence,description,counterparty,sha256,matched_group,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (fid, account, occurred_on, direction, pence, description, counterparty, sha, "", utc_now_iso()),
        )
    return {"ok": True, "duplicate": False, "id": fid, "account": account, "amount": _money(pence)}


def _feed_lines(account: str | None = None) -> list[dict[str, Any]]:
    _ensure_feed_table()
    with connect() as con:
        if account:
            rows = con.execute("SELECT * FROM rtm_trust_feed WHERE account=? ORDER BY occurred_on, created_at",
                               (_account(account),)).fetchall()
        else:
            rows = con.execute("SELECT * FROM rtm_trust_feed ORDER BY occurred_on, created_at").fetchall()
    return [dict(r) for r in rows]


# --- running-account movements -----------------------------------------------

def record_running(*, direction: str, amount: str | float | int, kind: str,
                   occurred_on: str | None = None, trust_tagged: bool = False,
                   flat_id: str = "", document_number: str = "", counterparty: str = "",
                   reference: str = "", transfer_group: str = "", source: str = "manual",
                   notes: str = "") -> dict[str, Any]:
    """Record a movement on the world-facing running account. Records only; moves nothing."""
    _ensure_tables()
    direction = (direction or "").strip().lower()
    if direction not in {"in", "out", "opening"}:
        raise ValueError("direction must be in, out or opening")
    rid = "run_" + uuid.uuid4().hex[:12]
    pence = abs(_money_to_pence(amount))
    with connect() as con:
        con.execute(
            "INSERT INTO rtm_trust_running(id,occurred_on,direction,amount_pence,kind,trust_tagged,flat_id,"
            "document_number,counterparty,reference,transfer_group,source,notes,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, _day(occurred_on), direction, pence, kind, 1 if trust_tagged else 0, flat_id,
             document_number, counterparty, reference, transfer_group, source, notes, utc_now_iso()),
        )
    return {"ok": True, "id": rid, "account": RUNNING, "amount": _money(pence), "direction": direction, "kind": kind}


def _running_rows() -> list[dict[str, Any]]:
    _ensure_tables()
    with connect() as con:
        rows = con.execute("SELECT * FROM rtm_trust_running ORDER BY occurred_on, created_at").fetchall()
    return [dict(r) for r in rows]


def _running_ledger_balance_pence() -> int:
    total = 0
    for r in _running_rows():
        total += int(r["amount_pence"]) if r["direction"] in ("in", "opening") else -int(r["amount_pence"])
    return total


def _trust_money_in_running_pence() -> int:
    """Identified service-charge/reserve trust money that has landed in running and not yet been
    swept onward. Tagged company money is excluded. A sweep out reduces the exposed amount."""
    exposed = 0
    for r in _running_rows():
        if not r["trust_tagged"]:
            continue
        if r["direction"] == "in":
            exposed += int(r["amount_pence"])
        elif r["direction"] == "out" and r["kind"] in ("sweep_out",):
            exposed -= int(r["amount_pence"])
    return max(exposed, 0)


def _oldest_unswept_days(as_of: str | None = None) -> int:
    """Age in days of the oldest still-exposed trust income in running."""
    rows = [r for r in _running_rows() if r["trust_tagged"] and r["direction"] == "in" and r["kind"] == "income"]
    swept_out = sum(int(r["amount_pence"]) for r in _running_rows()
                    if r["trust_tagged"] and r["direction"] == "out" and r["kind"] == "sweep_out")
    asof = date.fromisoformat(_day(as_of))
    running_swept = swept_out
    oldest = 0
    for r in sorted(rows, key=lambda x: (x["occurred_on"], x["created_at"])):
        amt = int(r["amount_pence"])
        if running_swept >= amt:
            running_swept -= amt
            continue
        # this income (or its tail) is still exposed
        age = (asof - date.fromisoformat(r["occurred_on"])).days
        oldest = max(oldest, age)
        running_swept = 0
    return oldest


# --- reserve maths (Layer 2 section 3) ---------------------------------------

def reserve_fraction() -> float:
    """reserve_annual / sch1_total, computed from the live budget (4710 / 30320 = 0.15534)."""
    reserve_annual, sch1_total = _budget_reserve_and_sch1()
    if sch1_total == 0:
        return 0.0
    return float((Decimal(reserve_annual) / Decimal(sch1_total)))


def _budget_reserve_and_sch1() -> tuple[int, int]:
    """Pull the reserve line and the Schedule 1 total from the building's live budget, in pounds.

    Schedule 1 total INCLUDES the reserve line (the reserve contribution is one line inside sch1).
    """
    reserve = 0
    sch1_total = 0
    for (label, schedule, amount) in PC.EXAMPLE_COURT_BUDGET:
        if schedule == "sch1":
            sch1_total += int(round(float(amount)))
            if "reserve" in label.lower():
                reserve += int(round(float(amount)))
    return reserve, sch1_total


def reserve_portion(flat_id: str, allocation_pence: int) -> int:
    """The reserve slice of a Schedule 1 allocation. allocation_pence must be the Schedule 1 part of
    the payment only (Schedule 2 contributes zero reserve)."""
    frac = Decimal(str(reserve_fraction()))
    slice_pence = (Decimal(int(allocation_pence)) * frac).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(slice_pence)


def subaccount_split(flat_id: str, year: str = "2027", numerator: int = 1, denominator: int = 2) -> dict[str, Any]:
    """THE canonical reserve/service-charge trust split for a flat's half-year demand, the "two
    sub-accounts". Distinct from the SCHEDULE split (Sch1 building / Sch2 undercroft): here the whole
    demand is swept into the reserve trust and the service-charge trust. The reserve slice is
    reserve_fraction (reserve line / Schedule-1 total, 4710/30320) of the SCHEDULE-1 HALF ONLY (undercroft
    contributes nothing); the service slice is the remainder of the whole demand. Everything (the chat
    tool, any sweep logic, any report) must call THIS rather than recomputing, so a hand-done arithmetic
    slip can never diverge from the engine. Deed-exact figures, round-half-up on pence."""
    from . import rtm_apportionment as APP
    ppm = APP.flat_apportionment(flat_id) or {}
    sch1_half = APP._deed_pence(int(ppm.get("sch1", 0)), APP.schedule_period_pence("sch1", year, numerator, denominator))
    sch2_half = APP._deed_pence(int(ppm.get("sch2", 0)), APP.schedule_period_pence("sch2", year, numerator, denominator))
    total = sch1_half + sch2_half
    reserve = reserve_portion(flat_id, sch1_half)
    service = total - reserve
    return {
        "flat_id": flat_id,
        "schedule_split": {"sch1_building_services": _money(sch1_half), "sch2_undercroft": _money(sch2_half)},
        "subaccount_split": {"reserve_fund_trust": _money(reserve), "service_charge_trust": _money(service)},
        "total": _money(total),
        "reserve_pence": reserve, "service_pence": service, "sch1_half_pence": sch1_half, "total_pence": total,
        "reserve_fraction_pct": round(reserve_fraction() * 100, 4),
        "note": "Reserve = reserve_fraction of the Schedule-1 half only (undercroft excluded); service = remainder of the whole demand.",
    }


def record_reserve_collected(*, flat_id: str, sch1_allocation_pence: int,
                             document_number: str = "", occurred_on: str | None = None,
                             notes: str = "") -> dict[str, Any]:
    """When service charge income is allocated, log the reserve slice as 'collected' (a liability to
    move that money into the reserve trust). Records only."""
    _ensure_tables()
    portion = reserve_portion(flat_id, sch1_allocation_pence)
    rid = "resl_" + uuid.uuid4().hex[:12]
    with connect() as con:
        con.execute(
            "INSERT INTO rtm_trust_reserve_ledger(id,occurred_on,movement,amount_pence,flat_id,document_number,transfer_group,notes,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (rid, _day(occurred_on), "collected", portion, flat_id, document_number, "", notes, utc_now_iso()),
        )
    return {"ok": True, "id": rid, "movement": "collected", "amount": _money(portion), "amount_pence": portion}


def record_reserve_transferred(*, amount: str | float | int, transfer_group: str = "",
                               occurred_on: str | None = None, notes: str = "") -> dict[str, Any]:
    """Log reserve money confirmed landed in reserve_fund_trust (matched in the feeds). Records only."""
    _ensure_tables()
    rid = "rest_" + uuid.uuid4().hex[:12]
    pence = abs(_money_to_pence(amount))
    with connect() as con:
        con.execute(
            "INSERT INTO rtm_trust_reserve_ledger(id,occurred_on,movement,amount_pence,flat_id,document_number,transfer_group,notes,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (rid, _day(occurred_on), "transferred", pence, "", "", transfer_group, notes, utc_now_iso()),
        )
    return {"ok": True, "id": rid, "movement": "transferred", "amount": _money(pence), "amount_pence": pence}


def _reserve_totals() -> tuple[int, int]:
    _ensure_tables()
    with connect() as con:
        rows = con.execute("SELECT movement, amount_pence FROM rtm_trust_reserve_ledger").fetchall()
    collected = sum(int(r["amount_pence"]) for r in rows if r["movement"] == "collected")
    transferred = sum(int(r["amount_pence"]) for r in rows if r["movement"] == "transferred")
    return collected, transferred


def reserve_position() -> dict[str, Any]:
    """collected, transferred, in_transit (collected - transferred), and the reserve_fund_trust balance."""
    collected, transferred = _reserve_totals()
    in_transit = collected - transferred
    reserve_bal = BK.ledger().get("balances_pence", {}).get(RESERVE_TRUST, 0)
    return {
        "collected": _money(collected),
        "collected_pence": collected,
        "transferred": _money(transferred),
        "transferred_pence": transferred,
        "in_transit": _money(in_transit),
        "in_transit_pence": in_transit,
        "reserve_trust_balance": _money(reserve_bal),
        "reserve_trust_balance_pence": reserve_bal,
        "reserve_fraction": reserve_fraction(),
    }


def flat_reserve_balance(flat_id: str) -> dict[str, Any]:
    """The reserve-fund amount held for ONE flat (LPE1 4.6.1), the hardest figure by hand. Read-only.

    Two components:
      (a) OPENING share , the flat's Schedule-1 deed share (penny-exact largest-remainder) of the reserve
          fund's opening capital, i.e. the lump-sum reserve monies transferred in from the predecessor
          manager at handover. That historical reserve belongs to the flats by size, so it is
          apportioned by the same Schedule-1 permille the reserve line itself is billed on.
      (b) POST-ACQUISITION slice , the reserve slice (reserve_fraction of the Schedule-1 half) of demands
          the flat has since PAID, via subaccount_split (prorated for part payment).
    The per-flat reserve ledger (rtm_trust_reserve_ledger) is not populated, so (b) is computed
    canonically; if it ever gains rows, they take precedence for (b)."""
    fid = (flat_id or "").strip().upper().replace("-", "")
    if not fid:
        return {"flat_id": "", "reserve_pence": 0, "reserve": _money(0),
                "opening_share_pence": 0, "post_acquisition_pence": 0, "basis": "none"}

    # (a) deed share of the opening (former agent handover) reserve, penny-exact across all flats
    opening_share = 0
    try:
        from . import rtm_management as M
        fr = M.fund_reconciliation() or {}
        opening_total = int(((fr.get("funds") or {}).get("reserve") or {}).get("opening_capital_pence") or 0)
        if opening_total > 0:
            from . import rtm_apportionment as APP
            opening_share = int(APP.largest_remainder(opening_total, APP.participants("sch1")).get(fid, 0))
    except Exception:
        opening_share = 0

    # (b) post-acquisition slice: explicit ledger rows if present, else canonical (reserve slice of PAID)
    post_acq = 0
    ledger_rows = 0
    from ..db import connect
    try:
        with connect() as con:
            rows = con.execute(
                "SELECT LOWER(COALESCE(movement,'')), COALESCE(amount_pence,0) "
                "FROM rtm_trust_reserve_ledger WHERE UPPER(REPLACE(COALESCE(flat_id,''),'-',''))=?",
                (fid,)).fetchall()
        ledger_rows = len(rows)
        for mv, amt in rows:
            post_acq += -int(amt or 0) if mv in ("refund", "adjustment_out", "transferred") else int(amt or 0)
    except Exception:
        ledger_rows = 0
    if not ledger_rows:
        try:
            from . import rtm_management as M
            for d in M.list_demands(flat=fid):
                if d.get("status") in ("Superseded", "Cancelled"):
                    continue
                amount = int(d.get("amount_pence") or 0)
                paid = amount - int(d.get("outstanding_pence") or 0)
                if amount <= 0 or paid <= 0:
                    continue
                sp = subaccount_split(fid)
                post_acq += int(round(int(sp.get("reserve_pence") or 0) * (paid / amount)))
        except Exception:
            post_acq = 0

    total = opening_share + post_acq
    return {"flat_id": fid, "reserve_pence": total, "reserve": _money(total),
            "opening_share_pence": opening_share, "opening_share": _money(opening_share),
            "post_acquisition_pence": post_acq, "post_acquisition": _money(post_acq),
            "basis": (f"Schedule-1 deed share of the reserve-fund opening ({_former_agent()} handover) + "
                      "reserve slice of paid demands" + ("" if ledger_rows else "; reserve ledger not populated"))}


def _former_agent() -> str:
    """How the pre-RTM manager is named in the reserve-opening wording (lexicon "rtm.former_agent_label";
    generic in the fictional default)."""
    return str(_profile.lexicon("rtm.former_agent_label", "former agent"))


# --- receipts (Layer 2 section 2) --------------------------------------------

def _flat_meta(flat_id: str) -> dict[str, Any]:
    fid = flat_id.strip().upper().replace("-", "").replace(" ", "")
    fl = M._get_flat(fid) or {}
    leaseholder = fl.get("leaseholder_name", "")
    address = fl.get("correspondence_address", "")
    return {"flat_id": fid, "leaseholder": leaseholder, "address": address}


def _demand_row(flat_id: str, document_number: str) -> dict[str, Any] | None:
    fid = flat_id.strip().upper().replace("-", "").replace(" ", "")
    with connect() as con:
        row = con.execute(
            "SELECT * FROM rtm_mgmt_demands WHERE document_number=? AND flat_id=?",
            (document_number, fid),
        ).fetchone()
    return dict(row) if row else None


def _demand_outstanding_pence(flat_id: str, document_number: str) -> int | None:
    """Outstanding balance on a specific demand, in pence. None if the demand is not found.

    Outstanding is computed exactly as the management module does it: amount minus the sum of
    allocations against the demand. There is no stored 'paid'/'outstanding' column.
    """
    d = _demand_row(flat_id, document_number)
    if not d:
        return None
    amount = int(d.get("amount_pence") or 0)
    paid = M.allocated_to_demand(document_number)
    return amount - paid


def build_receipt(flat_id: str, document_number: str, payment_ref: str, amount_pence: int,
                  received_date: str, method: str = "Bank transfer") -> dict[str, Any]:
    """Build (and store) a receipt for a demand that has reached zero outstanding.

    A receipt is only issued when the demand's outstanding reaches zero. A partial payment updates
    the statement only and returns ``{"ok": False, "reason": "demand_not_fully_paid"}``.

    SECURITY/INTEGRITY (external audit, additional findings #2/#3): amount_pence and
    received_date used to be trusted verbatim from the caller. The system already knows the
    true figures once outstanding is confirmed zero: the demand's own billed amount (paid
    must equal billed, or outstanding would not be zero) and the real bank_date on the
    transaction(s) actually allocated against this demand. A caller-supplied amount that
    disagrees with the demand's billed total is now refused outright rather than accepted and
    printed on a PDF: a real leaseholder could otherwise be handed a receipt that confidently
    says "£0.01 received" against a genuine £50 payment. received_date is derived from the
    linked bank transaction when one exists, falling back to the caller's value only if no
    transaction is linked (so a manually-recorded payment isn't blocked). receipt_number now
    includes the document_number, so a second flat receipt can never silently INSERT OR
    REPLACE and erase the audit trail of an earlier one for the same flat.
    """
    _ensure_tables()
    meta = _flat_meta(flat_id)
    fid = meta["flat_id"]
    outstanding = _demand_outstanding_pence(fid, document_number)
    if outstanding is None:
        return {"ok": False, "reason": "demand_not_found", "flat_id": fid, "document_number": document_number}
    if outstanding > 0:
        return {"ok": False, "reason": "demand_not_fully_paid", "outstanding": _money(outstanding),
                "outstanding_pence": outstanding}

    drow = _demand_row(fid, document_number)
    true_amount_pence = int((drow or {}).get("amount_pence") or 0)
    if int(amount_pence) != true_amount_pence:
        return {"ok": False, "reason": "amount_mismatch",
                "supplied_pence": int(amount_pence), "true_amount_pence": true_amount_pence,
                "message": (f"the demand's billed amount is {_money(true_amount_pence)}; a "
                           f"receipt for {_money(int(amount_pence))} would misstate what was "
                           f"actually paid, so it was not issued")}

    # Prefer the real bank_date from the transaction(s) actually allocated to this demand
    # (the latest one, if it took more than one payment to clear it) over the caller's date.
    true_received_date = received_date
    try:
        with connect() as con:
            row = con.execute(
                "SELECT bt.bank_date FROM rtm_mgmt_allocations al "
                "JOIN rtm_mgmt_bank_transactions bt ON bt.id = al.bank_transaction_id "
                "WHERE al.document_number=? ORDER BY bt.bank_date DESC LIMIT 1",
                (document_number,)).fetchone()
        if row and row["bank_date"]:
            true_received_date = row["bank_date"]
    except Exception:
        pass

    # receipt_number scoped to the DOCUMENT, not just the flat: two paid demands for the same
    # flat used to collide on RCPT-26-{flat} and the second INSERT OR REPLACE erased the
    # first's row entirely, losing that receipt's existence from the audit trail.
    receipt_number = f"RCPT-26-{fid}-{document_number}"
    issue_date = _day(true_received_date)
    period = (drow or {}).get("period", "") or ""
    # The premises line is the flat's address (there is no separate premises column on the demand).
    premises = meta["address"] or fid
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO rtm_trust_receipts(receipt_number,flat_id,document_number,payment_ref,"
            "amount_pence,received_date,issue_date,method,period,leaseholder,address,premises,pdf_path,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (receipt_number, fid, document_number, payment_ref, true_amount_pence, _day(true_received_date),
             issue_date, method, period, meta["leaseholder"], meta["address"], premises, "", utc_now_iso()),
        )
    return {
        "ok": True,
        "receipt_number": receipt_number,
        "flat_id": fid,
        "document_number": document_number,
        "payment_ref": payment_ref,
        "amount": _money(true_amount_pence),
        "amount_pence": true_amount_pence,
        "received_date": _day(true_received_date),
        "issue_date": issue_date,
        "method": method,
        "period": period,
        "leaseholder": meta["leaseholder"],
        "address": meta["address"],
        "premises": premises,
        "outstanding": "£0.00",
    }


def list_receipts(flat_id: str | None = None) -> list[dict[str, Any]]:
    _ensure_tables()
    with connect() as con:
        if flat_id:
            fid = flat_id.strip().upper().replace("-", "").replace(" ", "")
            rows = con.execute("SELECT * FROM rtm_trust_receipts WHERE flat_id=? ORDER BY created_at DESC", (fid,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM rtm_trust_receipts ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["amount"] = _money(int(d["amount_pence"]))
        out.append(d)
    return out



def _receipts_dir() -> str:
    import os
    base = os.getenv("MONGOOSE_RTM_RECEIPTS_DIR")
    if base:
        return base
    return str(Path(_data_root()) / "rtm_receipts")


def _data_root() -> str:
    import os
    p = os.getenv("MONGOOSE_DB_PATH")
    if p:
        return str(Path(p).parent)
    # Fall back to the canonical data tree. The old fallback was ~/mongoose/data (missing the dot),
    # which stranded receipts in a wholly separate tree; prefer settings.data_dir, then the correct
    # ~/.mongoose/data.
    try:
        from ..settings import get_settings
        return str(get_settings().data_dir)
    except Exception:
        return str(Path.home() / ".mongoose" / "data")



# --- weekly reconciliation (Layer 2 section 4) -------------------------------

def reconcile_weekly(as_of: str | None = None) -> dict[str, Any]:
    """Per-account reconcile across the three feeds, plus reserve position, exposure, transfers and
    flags. Status ok | attention | overdue. Records nothing; computes a report."""
    asof = _day(as_of)
    flags: list[dict[str, Any]] = []
    accounts_out: dict[str, Any] = {}

    # Trust accounts: drift = bank - ledger; any drift is attention.
    trust_ledger = BK.ledger().get("balances_pence", {})
    trust_bank = {f: (BK.latest_bank_balances().get(f) or {}).get("balance_pence") for f in (SERVICE_TRUST, RESERVE_TRUST)}
    status = "ok"
    for f in (SERVICE_TRUST, RESERVE_TRUST):
        ledger_bal = int(trust_ledger.get(f, 0))
        bank_bal = trust_bank.get(f)
        unmatched = _unmatched_feed(f)
        if bank_bal is None:
            drift = None
            accounts_out[f] = {"bank_balance": "", "ledger_balance": _money(ledger_bal),
                               "drift": "", "unmatched": unmatched,
                               "note": "No bank balance recorded yet."}
            status = _raise(status, "attention")
        else:
            drift = int(bank_bal) - ledger_bal
            accounts_out[f] = {"bank_balance": _money(int(bank_bal)), "ledger_balance": _money(ledger_bal),
                               "drift": _money(drift), "drift_pence": drift, "unmatched": unmatched}
            if drift != 0 or unmatched:
                status = _raise(status, "attention")
                flags.append({"flag": "trust_drift", "account": f, "drift": _money(drift),
                              "detail": "Trust account drift must be zero.", "unmatched": unmatched})
                _queue_unmatched(f, unmatched)

    # running: target-zero. The signal is trust money resting here, NOT bank-vs-ledger difference.
    run_ledger = _running_ledger_balance_pence()
    run_bank = (_latest_running_bank_balance())
    run_unmatched = _unmatched_feed(RUNNING)
    accounts_out[RUNNING] = {
        "bank_balance": _money(run_bank) if run_bank is not None else "",
        "ledger_balance": _money(run_ledger),
        "drift": "",  # deliberately not an error signal for running
        "unmatched": run_unmatched,
        "note": "Target-zero turnstile. A transient non-zero balance is not an error; the signal is trust money resting here.",
    }
    if run_unmatched:
        _queue_unmatched(RUNNING, run_unmatched)

    # Reserve + exposure
    reserve = reserve_position()
    exposed = _trust_money_in_running_pence()
    oldest = _oldest_unswept_days(asof)
    exposure = {
        "trust_money_in_running": _money(exposed),
        "trust_money_in_running_pence": exposed,
        "oldest_unswept_days": oldest,
    }
    if exposed > 0:
        if oldest > TRUST_EXPOSURE_GRACE_DAYS:
            status = _raise(status, "overdue")
            flags.append({"flag": "trust_exposure_overdue", "account": RUNNING,
                          "amount": _money(exposed), "oldest_unswept_days": oldest,
                          "detail": f"Trust money exposed in running beyond the {TRUST_EXPOSURE_GRACE_DAYS}-day window."})
        else:
            status = _raise(status, "attention")
            flags.append({"flag": "trust_exposure", "account": RUNNING, "amount": _money(exposed),
                          "oldest_unswept_days": oldest,
                          "detail": "Service charge income landed in running, awaiting sweep to trust."})

    # Transfers (section 5)
    transfers = transfer_status(asof)
    for fl in transfers.get("flags", []):
        flags.append(fl)
        sev = fl.get("severity", "attention")
        status = _raise(status, "overdue" if sev == "overdue" else "attention")

    return {
        "as_of": asof,
        "status": status,
        "accounts": accounts_out,
        "reserve": {
            "collected": reserve["collected"], "transferred": reserve["transferred"],
            "in_transit": reserve["in_transit"], "reserve_trust_balance": reserve["reserve_trust_balance"],
        },
        "exposure": exposure,
        "transfers": transfers.get("obligations", []),
        "flags": flags,
    }


def _raise(current: str, new: str) -> str:
    order = {"ok": 0, "attention": 1, "overdue": 2}
    return new if order[new] > order[current] else current


def _unmatched_feed(account: str) -> list[dict[str, Any]]:
    """Feed lines on an account not yet tagged to a sweep group. For trust accounts these are the
    drift culprits; surfaced for the review queue."""
    out = []
    for line in _feed_lines(account):
        if not line.get("matched_group"):
            out.append({"occurred_on": line["occurred_on"], "direction": line["direction"],
                        "amount": _money(int(line["amount_pence"])), "description": line.get("description", "")})
    return out


def _queue_unmatched(account: str, unmatched: list[dict[str, Any]]) -> None:
    """Send unmatched lines to the existing management review queue (best-effort; records only)."""
    if not unmatched:
        return
    try:
        for u in unmatched:
            M.add_evidence(
                target_type="reconcile_review",
                target_id=account,
                document_kind="unmatched_feed_line",
                title=f"Unmatched {account} line {u.get('amount','')} on {u.get('occurred_on','')}",
                path="",
                notes=u.get("description", ""),
            )
    except Exception:
        pass


def _latest_running_bank_balance() -> int | None:
    """A separately recorded observed running-account bank balance, if any. Stored under the feed
    table convention is avoided; we derive from the most recent reconciliation input if present."""
    # We keep this simple: callers may record a running bank balance via record_running opening/in/out;
    # for reconciliation the meaningful figure is the ledger position plus any explicit balance note.
    import os
    # Allow an explicit observed balance via env for tests/automation; otherwise None.
    val = os.getenv("MONGOOSE_RTM_RUNNING_BANK_PENCE")
    if val is not None:
        try:
            return int(val)
        except ValueError:
            return None
    return None


# --- transfer monitoring (Layer 2 section 5) ---------------------------------

def expected_transfers(as_of: str | None = None) -> list[dict[str, Any]]:
    """The sweeps that SHOULD have happened given the current state:
      1. Protect: running -> service_charge_trust, whenever service charge income is exposed in running.
      2. Reserve: -> reserve_fund_trust, whenever reserve_in_transit is positive.
      3. Fund operating: service_charge_trust -> running, before an operating-cost payment.
      4. Fund works: reserve_fund_trust -> running, before a reserve-funded works payment.
    """
    asof = _day(as_of)
    exp: list[dict[str, Any]] = []
    exposed = _trust_money_in_running_pence()
    if exposed > 0:
        exp.append({"kind": "protect", "from": RUNNING, "to": SERVICE_TRUST,
                    "amount_pence": exposed, "amount": _money(exposed),
                    "reason": "Service charge income landed in running and not yet swept to trust."})
    collected, transferred = _reserve_totals()
    in_transit = collected - transferred
    if in_transit > 0:
        exp.append({"kind": "reserve", "from": RUNNING, "to": RESERVE_TRUST,
                    "amount_pence": in_transit, "amount": _money(in_transit),
                    "reason": "Reserve collected but not yet landed in the reserve trust."})
    # Fund sweeps are driven by due payables; computed from any payment intents recorded as 'due'.
    for due in _due_payments(asof):
        if due["fund_source"] == SERVICE_TRUST:
            exp.append({"kind": "fund_operating", "from": SERVICE_TRUST, "to": RUNNING,
                        "amount_pence": due["amount_pence"], "amount": _money(due["amount_pence"]),
                        "reason": f"Operating-cost payment due: {due['description']}."})
        elif due["fund_source"] == RESERVE_TRUST:
            exp.append({"kind": "fund_works", "from": RESERVE_TRUST, "to": RUNNING,
                        "amount_pence": due["amount_pence"], "amount": _money(due["amount_pence"]),
                        "reason": f"Reserve-funded works payment due: {due['description']}."})
    return exp


def _due_payments(as_of: str) -> list[dict[str, Any]]:
    """Supplier payments flagged as due, with their intended funding source. Read-only; from the
    running table 'kind=payment_due' rows if present (none by default)."""
    out = []
    for r in _running_rows():
        if r["kind"] == "payment_due":
            src = SERVICE_TRUST if (r.get("notes") or "").startswith("operating") else RESERVE_TRUST
            if "works" in (r.get("notes") or ""):
                src = RESERVE_TRUST
            out.append({"amount_pence": int(r["amount_pence"]), "description": r.get("counterparty", "") or r.get("notes", ""),
                        "fund_source": src})
    return out


def match_transfers(as_of: str | None = None) -> list[dict[str, Any]]:
    """Pair a debit on a source account with a matching credit on the destination of the same amount
    within the window; tag the pair as one transaction. Returns matched pairs. A lone leg is reported
    as transfer_incomplete and never auto-resolved."""
    asof = _day(as_of)
    feed = [line for line in _feed_lines() if line.get("occurred_on", "") <= asof]
    debits = [l for l in feed if l["direction"] == "out" and not l.get("matched_group")]
    credits = [l for l in feed if l["direction"] == "in" and not l.get("matched_group")]
    matched: list[dict[str, Any]] = []
    used_credit_ids: set[str] = set()
    for d in sorted(debits, key=lambda x: (x["occurred_on"], x["created_at"])):
        d_day = date.fromisoformat(d["occurred_on"])
        for cr in sorted(credits, key=lambda x: (x["occurred_on"], x["created_at"])):
            if cr["id"] in used_credit_ids:
                continue
            if cr["account"] == d["account"]:
                continue  # a sweep moves between two different accounts
            if int(cr["amount_pence"]) != int(d["amount_pence"]):
                continue
            c_day = date.fromisoformat(cr["occurred_on"])
            if abs((c_day - d_day).days) > SWEEP_MATCH_WINDOW_DAYS:
                continue
            group = "swp_" + uuid.uuid4().hex[:10]
            with connect() as con:
                con.execute("UPDATE rtm_trust_feed SET matched_group=? WHERE id IN (?,?)", (group, d["id"], cr["id"]))
            used_credit_ids.add(cr["id"])
            matched.append({
                "transfer_group": group,
                "from": d["account"], "to": cr["account"],
                "amount_pence": int(d["amount_pence"]), "amount": _money(int(d["amount_pence"])),
                "debit_on": d["occurred_on"], "credit_on": cr["occurred_on"],
            })
            break
    return matched


def transfer_status(as_of: str | None = None) -> dict[str, Any]:
    """in_sync | due | overdue per obligation, plus the section-5 flags."""
    asof = _day(as_of)
    expected = expected_transfers(asof)
    matched = match_transfers(asof)
    flags: list[dict[str, Any]] = []
    obligations: list[dict[str, Any]] = []

    def _has_match(frm: str, to: str, amount_pence: int) -> dict[str, Any] | None:
        for m in matched:
            if m["from"] == frm and m["to"] == to and m["amount_pence"] == amount_pence:
                return m
        return None

    for e in expected:
        m = _has_match(e["from"], e["to"], e["amount_pence"])
        state = "in_sync" if m else "due"
        # exposure overdue is decided in reconcile; here protect/reserve overdue if aged.
        if e["kind"] in ("protect", "reserve") and not m:
            oldest = _oldest_unswept_days(asof) if e["kind"] == "protect" else 0
            if e["kind"] == "protect" and oldest > TRUST_EXPOSURE_GRACE_DAYS:
                state = "overdue"
        obligations.append({**{k: e[k] for k in ("kind", "from", "to", "amount", "amount_pence", "reason")},
                            "state": state, "matched_group": m["transfer_group"] if m else ""})

    # Reserve raid: a reserve_fund_trust -> running sweep followed by an operating-cost payment.
    raid = _detect_reserve_raid(asof, matched)
    flags.extend(raid)

    # Unfunded supplier payment: a running 'out' supplier payment with no matching inbound trust->running sweep.
    flags.extend(_detect_unfunded_payment(asof, matched))

    # Half-open transfers: a debit with no matching credit in the window.
    flags.extend(_detect_half_open(asof))

    return {"as_of": asof, "obligations": obligations, "matched": matched, "flags": flags}


def _detect_reserve_raid(as_of: str, matched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A sweep reserve_fund_trust -> running, then an operating-cost (not works) supplier payment, means
    reserve money funded day-to-day costs. The serious flag. Reserve funds capital/cyclical works only."""
    out = []
    reserve_to_running = [m for m in matched if m["from"] == RESERVE_TRUST and m["to"] == RUNNING]
    if not reserve_to_running:
        return out
    for m in reserve_to_running:
        sweep_day = date.fromisoformat(m["credit_on"])
        for r in _running_rows():
            if r["direction"] != "out" or r["kind"] != "supplier_payment":
                continue
            classification = (r.get("notes") or "").lower()
            pay_day = date.fromisoformat(r["occurred_on"])
            if pay_day < sweep_day:
                continue
            if (pay_day - sweep_day).days > 14:
                continue
            if "works" in classification or "capital" in classification or "cyclical" in classification:
                continue  # legitimate reserve-funded works
            # operating-cost payment funded by a reserve sweep
            out.append({
                "flag": "reserve_raid", "severity": "attention",
                "account": RESERVE_TRUST,
                "amount": m["amount"], "sweep_on": m["credit_on"], "payment_on": r["occurred_on"],
                "detail": "Reserve money swept to running was followed by an operating-cost payment. "
                          "The reserve funds capital/cyclical works only; it must never fund cleaning, "
                          "insurance, electricity or other operating lines.",
            })
            break
    return out


def _detect_unfunded_payment(as_of: str, matched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A supplier paid from running with no matching inbound trust -> running sweep means trust money
    already resting in running was spent before being protected."""
    out = []
    inbound = [m for m in matched if m["to"] == RUNNING and m["from"] in (SERVICE_TRUST, RESERVE_TRUST)]
    for r in _running_rows():
        if r["direction"] != "out" or r["kind"] != "supplier_payment":
            continue
        amt = int(r["amount_pence"])
        funded = any(m["amount_pence"] == amt for m in inbound)
        if not funded:
            out.append({
                "flag": "unfunded_supplier_payment", "severity": "attention",
                "account": RUNNING, "amount": _money(amt), "payment_on": r["occurred_on"],
                "detail": "Supplier paid from running with no matching inbound trust->running sweep; "
                          "trust money resting in running may have been spent before being protected.",
            })
    return out


def _detect_half_open(as_of: str) -> list[dict[str, Any]]:
    """A debit on one account with no matching credit on the other within the window -> transfer_incomplete.
    For director investigation; never auto-resolved."""
    out = []
    asof = date.fromisoformat(_day(as_of))
    feed = _feed_lines()
    credits = [l for l in feed if l["direction"] == "in"]
    for d in feed:
        if d["direction"] != "out" or d.get("matched_group"):
            continue
        d_day = date.fromisoformat(d["occurred_on"])
        # if window has elapsed and still no counterpart of equal amount on a different account
        if (asof - d_day).days < SWEEP_MATCH_WINDOW_DAYS:
            continue
        has_counterpart = any(
            cr["account"] != d["account"] and int(cr["amount_pence"]) == int(d["amount_pence"])
            and abs((date.fromisoformat(cr["occurred_on"]) - d_day).days) <= SWEEP_MATCH_WINDOW_DAYS
            and not cr.get("matched_group")
            for cr in credits
        )
        if not has_counterpart:
            out.append({
                "flag": "transfer_incomplete", "severity": "attention",
                "account": d["account"], "amount": _money(int(d["amount_pence"])),
                "debit_on": d["occurred_on"],
                "detail": "Debit with no matching credit on the other account within the window. "
                          "For director investigation; never auto-resolved.",
            })
    return out


def exposure(as_of: str | None = None) -> dict[str, Any]:
    asof = _day(as_of)
    exposed = _trust_money_in_running_pence()
    return {
        "trust_money_in_running": _money(exposed),
        "trust_money_in_running_pence": exposed,
        "oldest_unswept_days": _oldest_unswept_days(asof),
    }


# --- the guarantee -----------------------------------------------------------

def moves_no_money() -> dict[str, Any]:
    """A standing declaration that this module records and checks only. There is no function here that
    initiates, confirms, or simulates a bank instruction. The director makes every transfer by hand."""
    return {
        "module": "rtm_trust",
        "moves_money": False,
        "capabilities": ["record", "reconcile", "report", "flag", "render_document"],
        "statement": "Records and checks only; never moves money.",
    }
