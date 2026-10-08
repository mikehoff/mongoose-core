"""RTM payment plans: agree an instalment plan for a flat in arrears.

A director agrees a plan for a debit flat; Mongoose splits the outstanding balance into N equal monthly
instalments (penny-exact: the remainder rides on the final instalment so the parts always sum to the exact
balance), records the plan, and the schedule then shows on the portal ("Payment plan agreed") and can be
sent to the leaseholder as a branded letter. Mongoose owns the plan (the ledger of record); WordPress only
displays it and queues the "agree" action.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from sqlite3 import IntegrityError
from typing import Any

from ..db import audit, connect, utc_now_iso


def _ensure_tables(con) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS rtm_payment_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flat_id TEXT NOT NULL,
            total_pence INTEGER NOT NULL,
            months INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            instalments_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'agreed',
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            cancelled_at TEXT
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_rtm_plan_flat ON rtm_payment_plans(flat_id, status)")
    # ONE ACTIVE PLAN PER FLAT, enforced by the database rather than by a check in application code.
    # agree_payment_plan reads "is there already a plan?" and then inserts, which is only safe if nothing
    # runs between the two. Two concurrent pull_doc_requests (the WordPress nudge racing the catch-up
    # timer) did exactly that on 2026-09-01 and produced two identical live plans for one flat 3 seconds
    # apart - two schedules for one debt, and arrears counted twice. A partial unique index closes the
    # window: the loser of the race now fails its INSERT instead of succeeding.
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_rtm_plan_one_active "
                "ON rtm_payment_plans(flat_id) WHERE status='agreed'")
    # instalments_json is the AGREED SCHEDULE and must stay immutable: it is what the leaseholder was told
    # in writing, so a payment must never rewrite it. Payments are recorded here instead, one row per
    # instalment actually paid, which keeps "what we agreed" and "what has happened" separately auditable.
    #
    # UNIQUE(plan_id, n) is the idempotency guarantee. Recording an instalment is a one-click action the
    # owner will inevitably double-tap, and a plan payment recorded twice both overstates collection and
    # under-chases a real debt.
    con.execute(
        """CREATE TABLE IF NOT EXISTS rtm_plan_instalment_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            flat_id TEXT NOT NULL,
            n INTEGER NOT NULL,
            amount_pence INTEGER NOT NULL,
            paid_on TEXT NOT NULL,
            method TEXT NOT NULL DEFAULT 'bank_transfer',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(plan_id, n)
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_rtm_plan_paid_flat "
                "ON rtm_plan_instalment_payments(flat_id, paid_on)")


def _today_iso() -> str:
    """Today in the local calendar sense. A plan instalment is due on a calendar date, so comparing it to
    a UTC instant would call an instalment overdue late on the evening it is actually due."""
    return datetime.now().strftime("%Y-%m-%d")


def _money(pence: int) -> str:
    return f"£{pence / 100:,.2f}"


def _add_month(d: date, n: int) -> date:
    """d shifted n whole months, clamped to the month's last day (so 31 Jan + 1 month -> 28/29 Feb)."""
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def compute_instalments(total_pence: int, months: int = 6, start: date | None = None) -> list[dict[str, Any]]:
    """N monthly instalments that sum EXACTLY to total_pence.

    Every instalment except the last is the SAME rounded-UP amount and the final one absorbs the
    (smaller) balance. That is deliberate: the leaseholder sets ONE standing order for a single
    repeating figure and the odd payment is the last and smaller one. The original code did the
    reverse (equal floors, larger final payment), producing 5 x £180.03 then £180.06 , a figure no
    standing order matches and a sting in the tail.

    The schedule is anchored to the DEMAND'S DUE DATE, not to the day someone clicked a button, so it
    reads as a way to pay that demand rather than an arbitrary new run of dates.
    """
    months = max(1, int(months))
    total_pence = int(total_pence)
    per = -(-total_pence // months)                 # ceiling: the standing-order figure
    parts = [per] * (months - 1)
    parts.append(total_pence - per * (months - 1))  # final instalment takes the smaller remainder
    start = start or _default_start()
    out = []
    for i, p in enumerate(parts):
        due = _add_month(start, i)
        out.append({"n": i + 1, "due_date": due.isoformat(), "amount_pence": p, "amount": _money(p)})
    return out


def _default_start() -> date:
    """First instalment date when the caller names none: the oldest OPEN demand's due date (the date
    the money was already due). Falls back to the 30th of next month."""
    try:
        from . import rtm_management as M
        dues = sorted(str(d.get("due_date") or "")[:10] for d in M.list_demands(limit=500)
                      if str(d.get("status") or "") not in ("Paid", "Superseded", "Cancelled")
                      and d.get("due_date"))
        if dues:
            return date.fromisoformat(dues[0])
    except Exception:
        pass
    import calendar
    nxt = _add_month(date.today(), 1)
    return nxt.replace(day=min(30, calendar.monthrange(nxt.year, nxt.month)[1]))


def agree_payment_plan(*, flat: str, months: int = 6, start_date: str | None = None,
                       note: str = "") -> dict[str, Any]:
    """Agree a plan for a flat's CURRENT outstanding balance. Idempotent-ish: if an active plan already
    exists for the flat it is returned unchanged (call cancel_payment_plan first to re-agree)."""
    from . import rtm_management as M
    fid = (flat or "").strip().upper().replace("-", "").replace(" ", "")
    st = M.flat_statement(fid)
    total = int(st.get("balance_pence") or 0)
    if total <= 0:
        return {"ok": False, "error": f"flat {fid} has no outstanding balance to plan"}
    start = None
    if start_date:
        try:
            start = date.fromisoformat(start_date[:10])
        except Exception:
            start = None
    import json as _json
    with connect() as con:
        _ensure_tables(con)
        existing = con.execute(
            "SELECT id, instalments_json, months, total_pence, start_date FROM rtm_payment_plans "
            "WHERE flat_id=? AND status='agreed' ORDER BY id DESC LIMIT 1", (fid,)).fetchone()
        if existing:
            return {"ok": True, "already_agreed": True, "flat_id": fid,
                    "plan_id": existing["id"] if not isinstance(existing, tuple) else existing[0],
                    "instalments": _json.loads(existing["instalments_json"] if not isinstance(existing, tuple) else existing[1]),
                    "total": _money(int(existing["total_pence"] if not isinstance(existing, tuple) else existing[3]))}
        inst = compute_instalments(total, months, start)
        now = datetime.now(timezone.utc).isoformat()
        try:
            cur = con.execute(
                "INSERT INTO rtm_payment_plans(flat_id,total_pence,months,start_date,instalments_json,status,note,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (fid, total, months, inst[0]["due_date"], _json.dumps(inst), "agreed", note or "", now))
        except IntegrityError:
            # Lost the race against a concurrent agree for the same flat. The other one is just as valid,
            # so return it rather than failing the caller: a double-click must read as "already agreed".
            con.rollback()
            row = con.execute(
                "SELECT id, instalments_json, total_pence FROM rtm_payment_plans "
                "WHERE flat_id=? AND status='agreed' ORDER BY id DESC LIMIT 1", (fid,)).fetchone()
            if row is None:
                raise
            return {"ok": True, "already_agreed": True, "flat_id": fid,
                    "plan_id": row["id"] if not isinstance(row, tuple) else row[0],
                    "instalments": _json.loads(row["instalments_json"] if not isinstance(row, tuple) else row[1]),
                    "total": _money(int(row["total_pence"] if not isinstance(row, tuple) else row[2]))}
        plan_id = int(cur.lastrowid)
        con.commit()
    return {"ok": True, "flat_id": fid, "plan_id": plan_id, "total": _money(total), "total_pence": total,
            "months": months, "instalments": inst,
            "leaseholder": st.get("leaseholder_name") or fid}


def payment_plan_for(flat_id: str) -> dict[str, Any] | None:
    """The active plan for a flat (for the portal 'Payment plan agreed' indicator), or None."""
    import json as _json
    fid = (flat_id or "").strip().upper().replace("-", "").replace(" ", "")
    if not fid:
        return None
    try:
        with connect() as con:
            _ensure_tables(con)
            r = con.execute(
                "SELECT id, total_pence, months, start_date, instalments_json, created_at FROM rtm_payment_plans "
                "WHERE flat_id=? AND status='agreed' ORDER BY id DESC LIMIT 1", (fid,)).fetchone()
    except Exception:
        return None
    if not r:
        return None
    d = dict(r)
    return {"plan_id": d["id"], "months": d["months"], "total": _money(int(d["total_pence"])),
            "total_pence": int(d["total_pence"]), "start_date": d["start_date"],
            "created_at": d["created_at"],
            "instalments": _json.loads(d["instalments_json"])}


def plan_progress(flat_id: str) -> dict[str, Any] | None:
    """The agreed schedule with what has actually been paid against it, or None if there is no plan.

    This is the single source for every plan-aware figure: the amber colouring (owed, but not in default),
    the paid-so-far segment of the collection bar, and the two headline percentages. Computing it once here
    stops the plugin and Mongoose disagreeing about the same money, which is how the phone surfaces drifted.
    """
    plan = payment_plan_for(flat_id)
    if not plan:
        return None
    with connect() as con:
        _ensure_tables(con)
        paid_rows = {int(r["n"]): dict(r) for r in con.execute(
            "SELECT n, amount_pence, paid_on FROM rtm_plan_instalment_payments WHERE plan_id=?",
            (plan["plan_id"],)).fetchall()}
    today = _today_iso()
    # The date the plan was agreed, and the date any pre-agreement catch-up must be paid by: the same
    # 7-day window the payment-plan letter states, so the chart and the letter cannot disagree.
    agreed_on = str(plan.get("created_at") or "")[:10]
    _grace_deadline = today
    if agreed_on:
        try:
            _grace_deadline = (date.fromisoformat(agreed_on) + timedelta(days=7)).isoformat()
        except Exception:
            agreed_on = ""
    schedule = []
    paid_pence = 0
    overdue = 0
    for i in plan["instalments"]:
        n = int(i["n"])
        got = paid_rows.get(n)
        if got:
            paid_pence += int(got["amount_pence"])
        # Overdue means the due date has passed and nothing is recorded. Stated separately from "unpaid"
        # because a future instalment is not a debt yet, and colouring it as one would be wrong.
        #
        # A PLAN CANNOT BE IN DEFAULT BEFORE IT EXISTED. Agreeing a plan uses the demand's own start date,
        # so a plan granted today against an older demand is born with its first instalments already in
        # the past. Judged on the raw due date those are instantly "overdue", the plan is instantly "not
        # on track", and the flat lands in the arrears band on the very day the directors agreed to help
        # it - which is the opposite of what agreeing a plan means. Those instalments are the catch-up
        # sum, and the letter gives 7 days from the date of agreement to pay it, so that is the date they
        # are judged against. Instalments that fell due after the plan was agreed are unaffected.
        _due = str(i["due_date"])
        if agreed_on and _due < agreed_on:
            _due = _grace_deadline
        is_overdue = (not got) and _due < today
        overdue += 1 if is_overdue else 0
        schedule.append({**i, "paid": bool(got),
                         "paid_on": (got or {}).get("paid_on", ""),
                         "paid_amount_pence": int((got or {}).get("amount_pence") or 0),
                         "overdue": is_overdue,
                         "due_today": str(i["due_date"]) == today})
    total = int(plan["total_pence"])
    return {**plan, "schedule": schedule,
            "paid_pence": paid_pence, "paid": _money(paid_pence),
            "outstanding_pence": max(0, total - paid_pence),
            "outstanding": _money(max(0, total - paid_pence)),
            "instalments_paid": sum(1 for s in schedule if s["paid"]),
            "instalments_overdue": overdue,
            # "on track" is the honest read of a plan: paying to schedule is compliance, not arrears.
            "on_track": overdue == 0,
            "percent_paid": round(100.0 * paid_pence / total, 1) if total else 0.0}


def next_instalment(flat_id: str) -> dict[str, Any] | None:
    """The instalment the owner is about to record, with its amount already decided.

    The point of returning the amount rather than an empty box: a plan instalment has exactly ONE correct
    figure, and asking the owner to retype £180.04 invites the penny error that has already appeared once
    in a posted demand. Returns the earliest unpaid instalment, so a missed month is caught up before the
    current one rather than skipped.
    """
    prog = plan_progress(flat_id)
    if not prog:
        return None
    for s in prog["schedule"]:
        if not s["paid"]:
            return {"plan_id": prog["plan_id"], "flat_id": flat_id, "n": s["n"],
                    "amount_pence": int(s["amount_pence"]), "amount": s["amount"],
                    "due_date": s["due_date"], "overdue": s["overdue"], "due_today": s["due_today"],
                    "of_months": prog["months"]}
    return None


def record_instalment(flat: str, *, n: int | None = None, amount_pence: int | None = None,
                      paid_on: str | None = None, method: str = "bank_transfer",
                      note: str = "") -> dict[str, Any]:
    """Record a plan instalment as paid, and put the money through the SAME ledger as any other payment.

    Two properties matter. It is idempotent (UNIQUE(plan_id, n)), because this is a one-click button and a
    double tap must not double-count. And it does not invent its own money path: the payment is allocated to
    the flat's demand exactly like a manual payment, so the demand balance, the arrears list and the trust
    split all stay consistent with a plan payment instead of quietly diverging from it.
    """
    fid = (flat or "").strip().upper().replace("-", "").replace(" ", "")
    nxt = next_instalment(fid)
    prog = plan_progress(fid)
    if not prog:
        return {"ok": False, "error": f"no agreed payment plan for {fid}"}
    if n is None:
        if not nxt:
            return {"ok": False, "error": f"every instalment on {fid}'s plan is already recorded"}
        # A DOUBLE TAP MUST NOT ADVANCE THE PLAN. With n omitted this picks the earliest unpaid instalment,
        # so tapping the button twice recorded instalment 1 and then instalment 2, taking £360.08 for a
        # single £180.04 payment: collection overstated and a real future debt quietly marked settled.
        # UNIQUE(plan_id, n) cannot catch that, because the second write is a different n.
        #
        # So auto-selection only ever picks an instalment that is actually DUE. Paying ahead is legitimate
        # but rare, and must be deliberate: the caller names the instalment. An accidental second tap is
        # common, and now refuses instead of inventing a payment. The UI passes the n it displayed, which
        # makes the repeat hit UNIQUE and no-op properly.
        if str(nxt["due_date"]) > _today_iso():
            return {"ok": False, "not_yet_due": True, "flat_id": fid, "n": int(nxt["n"]),
                    "due_date": nxt["due_date"], "amount": nxt["amount"],
                    "error": (f"instalment {nxt['n']} is not due until {nxt['due_date']}. Every instalment "
                              f"due so far is already recorded. To record an early payment, name the "
                              f"instalment explicitly.")}
        n = int(nxt["n"])
    target = next((s for s in prog["schedule"] if int(s["n"]) == int(n)), None)
    if target is None:
        return {"ok": False, "error": f"plan for {fid} has no instalment {n}"}
    if target["paid"]:
        return {"ok": True, "already_recorded": True, "flat_id": fid, "n": n,
                "amount": target["amount"], "paid_on": target["paid_on"]}
    # Default to the agreed figure. A different amount is allowed (people pay what they pay) but is
    # reported back so a short payment is visible rather than silently treated as settling the instalment.
    amt = int(amount_pence) if amount_pence is not None else int(target["amount_pence"])
    day = paid_on or _today_iso()
    now = utc_now_iso()
    with connect() as con:
        _ensure_tables(con)
        try:
            con.execute(
                "INSERT INTO rtm_plan_instalment_payments(plan_id,flat_id,n,amount_pence,paid_on,method,note,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (prog["plan_id"], fid, int(n), amt, day, method, note, now))
        except Exception:
            return {"ok": True, "already_recorded": True, "flat_id": fid, "n": n}
        con.commit()
    ledger = {"ok": False, "error": "no open demand to allocate against"}
    try:
        from . import rtm_management as M
        dem = next((d for d in M.list_demands(flat=fid, limit=20)
                    if str(d.get("status", "")).lower() != "paid"), None)
        if dem:
            ledger = M.record_payment(document_number=dem["document_number"],
                                      amount=f"{amt / 100:.2f}", received_date=day,
                                      note=f"payment plan instalment {n} of {prog['months']}")
    except Exception as e:  # the instalment is recorded either way; surface the ledger failure honestly
        ledger = {"ok": False, "error": str(e)}
    after = plan_progress(fid)
    audit("rtm_plan", "instalment", True, payload={"flat": fid, "n": n, "amount_pence": amt})
    return {"ok": True, "flat_id": fid, "n": n, "amount": _money(amt),
            "short_by": _money(int(target["amount_pence"]) - amt) if amt < int(target["amount_pence"]) else "",
            "paid_on": day, "ledger": ledger,
            "instalments_paid": after["instalments_paid"], "of_months": after["months"],
            "plan_outstanding": after["outstanding"], "on_track": after["on_track"]}


def cancel_payment_plan(flat: str) -> dict[str, Any]:
    fid = (flat or "").strip().upper().replace("-", "").replace(" ", "")
    with connect() as con:
        _ensure_tables(con)
        n = con.execute("UPDATE rtm_payment_plans SET status='cancelled', cancelled_at=? "
                        "WHERE flat_id=? AND status='agreed'",
                        (datetime.now(timezone.utc).isoformat(), fid)).rowcount
        con.commit()
    return {"ok": True, "flat_id": fid, "cancelled": n}
