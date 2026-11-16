"""RTM director dashboard: read-only financial aggregation for the owner's Cockpit.

Pulls together the itemised budget, paid-vs-demanded, service-charge vs reserves balances, and the
arrears chasing shortlist from the existing rtm_management / rtm_bookkeeper stores. No mutations here;
this is the view layer the director uses to decide who to chase. Chaser drafts reuse the established
generate_reminder path so the recovery timeline and wording stay in one place.
"""
from __future__ import annotations

from typing import Any


def director_summary(*, year: str | None = None) -> dict[str, Any]:
    """Aggregated director financials. Safe on empty data (returns zeros / empty lists)."""
    from .rtm_management import _money, arrears, budget_actuals, list_demands
    from .rtm_bookkeeper import latest_bank_balances

    # 1. Itemised budget with actuals (budgeted / spent / committed / variance per line).
    try:
        budget = budget_actuals(year)
    except Exception as exc:
        budget = {"year": year, "lines": [], "total": {}, "error": str(exc)}

    # 2. Paid vs demanded across the current demand cycle (the half-year demand run).
    demands = list_demands(limit=5000) or []
    demanded_pence = sum(int(d.get("amount_pence", 0) or 0) for d in demands)
    outstanding_pence = sum(int(d.get("outstanding_pence", 0) or 0) for d in demands)
    paid_pence = max(0, demanded_pence - outstanding_pence)
    pct_paid = round((paid_pence / demanded_pence) * 100.0, 1) if demanded_pence > 0 else 0.0

    # 3. Fund balances: service charge account next to the reserve fund.
    bals = latest_bank_balances() or {}
    svc = bals.get("service_charge_trust", {}) or {}
    res = bals.get("reserve_fund_trust", {}) or {}
    svc_pence = int(svc.get("balance_pence", 0) or 0)
    res_pence = int(res.get("balance_pence", 0) or 0)

    # 4. Chasing shortlist: overdue demands, largest outstanding first.
    arr = arrears() or {}
    shortlist = sorted(
        list(arr.get("items", []) or []),
        key=lambda x: int(x.get("amount_outstanding_pence", 0) or 0),
        reverse=True,
    )

    # 5. Per-schedule breakdown (spec R28): budget/actual/variance, apportionment integrity, creditable.
    schedules_out: list[dict[str, Any]] = []
    try:
        from . import rtm_apportionment as APP
        period = f"{year}-H2" if year else None
        for s in APP.list_schedules(active_only=True):
            code = s["code"]
            v = APP.validate_schedule(code)
            if v["participant_count"] == 0:
                continue
            sp = APP.schedule_period_pence(code, str(year or ""))
            spend = APP.schedule_spend(code, str(year or ""))
            actual = spend["paid_pence"] + spend["committed_pence"]
            demanded = APP.demanded_for_schedule(code, str(year or ""), period)
            schedules_out.append({
                "code": code, "name": s["name"],
                "budgeted_pence": APP.schedule_year_pence(code, str(year or "")),
                "budgeted": _money(APP.schedule_year_pence(code, str(year or ""))),
                "period_pence": sp, "period": _money(sp),
                "demanded_pence": demanded, "demanded": _money(demanded),
                "paid": _money(spend["paid_pence"]), "committed": _money(spend["committed_pence"]),
                "variance": _money(sp - actual),
                "apportionment_ok": v["ok"], "apportionment_pct": v["pct_sum"],
                "participants": v["participant_count"],
                "creditable_pence": max(0, (demanded or sp) - actual),
                "creditable": _money(max(0, (demanded or sp) - actual)),
            })
    except Exception as exc:
        schedules_out = [{"error": str(exc)}]

    return {
        "ok": True,
        "budget": budget,
        "schedules": schedules_out,
        "paid_vs_demanded": {
            "demanded_pence": demanded_pence, "demanded": _money(demanded_pence),
            "paid_pence": paid_pence, "paid": _money(paid_pence),
            "outstanding_pence": outstanding_pence, "outstanding": _money(outstanding_pence),
            "pct_paid": pct_paid,
        },
        "balances": {
            "service_charge_pence": svc_pence, "service_charge": _money(svc_pence),
            "service_charge_as_at": svc.get("as_at", ""),
            "reserve_pence": res_pence, "reserve": _money(res_pence),
            "reserve_as_at": res.get("as_at", ""),
        },
        "chasing_shortlist": shortlist,
        "shortlist_count": len(shortlist),
    }


def chase_draft(*, document_number: str, action_type: str | None = None) -> dict[str, Any]:
    """Generate (do not send) a chaser for one overdue demand, via the established reminder path."""
    from .rtm_management import generate_reminder
    doc = (document_number or "").strip()
    if not doc:
        return {"ok": False, "error": "document_number required"}
    try:
        rem = generate_reminder(doc, action_type)
    except Exception as exc:
        return {"ok": False, "error": f"could not generate chaser: {exc}", "document_number": doc}
    return {"ok": True, "document_number": doc, "reminder": rem}
