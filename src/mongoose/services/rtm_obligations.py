"""RTM statutory obligations calendar: the forward-looking deadlines an RTM company must meet,
tracked so the company surfaces them as they approach rather than the director remembering dates.

This is the "evolves on its own" piece: an RTM has recurring legal obligations (annual service-charge
accounts and the s.21 summary window, insurance renewal, the s.20 consultation timeline when major
works arise) and fixed milestones (the RTM acquisition date). Operationally the dashboard already
chases arrears/payables; this layer adds the STATUTORY clock so a deadline never silently passes.

Design choices:
  - Obligations are computed, not stored, from a small declarative spec + the company's own anchor
    dates (acquisition date, accounting year end). So they stay correct as time advances with no
    migration and no per-deadline row to maintain.
  - Each obligation carries: a stable key, title, due date, days_remaining, a window band
    (overdue / due_soon / upcoming / future), and the statutory basis. The scheduler surfaces
    anything that crosses into due_soon once (notify-once), so it nudges without nagging.
  - Read/compute only. It points the director at what's coming; it sends no demands and moves no
    money. Anchored on the real Example Court acquisition date (16 July 2026, s.90(2) CLRA 2002).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

# Anchor dates for the company. ACQUISITION_DATE is the s.90(2) CLRA 2002 date the RTM takes over.
# ACCOUNTING_YEAR_END is the service-charge accounting year end (defaults to 31 March, a common
# UK service-charge year; overridable per company once that's wired to settings).
ACQUISITION_DATE = "2026-07-16"
ACCOUNTING_YEAR_END_MONTH = 3
ACCOUNTING_YEAR_END_DAY = 31

# How many days before a due date counts as "due soon" (the band that triggers a nudge).
DUE_SOON_DAYS = 30
# How far ahead we list "upcoming" obligations at all.
HORIZON_DAYS = 180


def _today() -> date:
    return datetime.utcnow().date()


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _safe_d(s: str) -> date | None:
    """Parse a YYYY-MM-DD date, returning None for any malformed/empty/non-string input rather than
    raising. Advisory read functions take user-supplied dates and must never crash on bad input."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        return _d(s)
    except (ValueError, TypeError):
        return None


def _band(days_remaining: int) -> str:
    if days_remaining < 0:
        return "overdue"
    if days_remaining <= DUE_SOON_DAYS:
        return "due_soon"
    if days_remaining <= HORIZON_DAYS:
        return "upcoming"
    return "future"


def _next_year_end(today: date) -> date:
    """The next accounting-year-end on/after today."""
    ye = date(today.year, ACCOUNTING_YEAR_END_MONTH, ACCOUNTING_YEAR_END_DAY)
    if ye < today:
        ye = date(today.year + 1, ACCOUNTING_YEAR_END_MONTH, ACCOUNTING_YEAR_END_DAY)
    return ye


def _obligation(key: str, title: str, due: date, basis: str, today: date) -> dict[str, Any]:
    days = (due - today).days
    return {
        "key": key,
        "title": title,
        "due_date": due.isoformat(),
        "days_remaining": days,
        "band": _band(days),
        "basis": basis,
    }


def upcoming_obligations(today: str | None = None, *, horizon_days: int = HORIZON_DAYS) -> list[dict[str, Any]]:
    """All statutory obligations whose due date falls within the horizon, soonest first.

    Computed from the company's anchor dates + the recurring statutory spec, so it is always
    current. Includes overdue items (negative days_remaining) so nothing that has slipped is hidden.
    """
    # A bad/garbage `today` must not crash an advisory read: fall back to the real today.
    t = (_safe_d(today) or _today()) if today else _today()
    out: list[dict[str, Any]] = []

    # 1. Fixed milestone: the RTM acquisition date (one-off, but show it until it passes + a grace).
    acq = _d(ACQUISITION_DATE)
    if (acq - t).days >= -30:  # show up to 30 days after, so the handover stays visible
        out.append(_obligation(
            "rtm_acquisition", "RTM acquisition date (company takes over management)",
            acq, "s.90(2) Commonhold and Leasehold Reform Act 2002", t))

    # 2. Annual service-charge accounts: prepared after the accounting year end. A reasonable
    #    internal target is within 6 months of year end (and the s.21 summary is a leaseholder right
    #    on request). We surface the year-end and the 6-month accounts target.
    ye = _next_year_end(t)
    out.append(_obligation(
        "accounting_year_end", "Service-charge accounting year end",
        ye, "service-charge accounting period close", t))
    accounts_due = ye + timedelta(days=183)
    out.append(_obligation(
        "annual_accounts", "Annual service-charge accounts prepared / available",
        accounts_due, "Landlord and Tenant Act 1985 (s.21 summary on request); good-practice 6-month target", t))

    # 3. Buildings insurance renewal: a hard annual obligation. Anchor to the acquisition date's
    #    anniversary as the working assumption until a real policy date is recorded.
    next_renewal = date(t.year, acq.month, acq.day)
    if next_renewal < t:
        next_renewal = date(t.year + 1, acq.month, acq.day)
    out.append(_obligation(
        "insurance_renewal", "Buildings insurance renewal",
        next_renewal, "leaseholder obligation to keep the building insured (lease + LTA 1985)", t))

    out = [o for o in out if o["days_remaining"] <= horizon_days]
    out.sort(key=lambda o: o["days_remaining"])
    return out


def s20_timeline(notice_of_intention_date: str) -> dict[str, Any]:
    """The s.20 consultation timeline for major works, computed from the Notice of Intention date.

    s.20 (Service Charges (Consultation Requirements) Regulations 2003) requires a 30-day observation
    period after the Notice of Intention, then estimates + a further 30-day observation, before works
    over the £250/leaseholder threshold can be fully recovered. Getting this wrong is the Daejan risk.
    This returns the key dates so the director never under-runs a consultation window.
    """
    try:
        noi = _d(notice_of_intention_date)
    except Exception:
        return {"ok": False, "error": "notice_of_intention_date must be YYYY-MM-DD"}
    obs1_end = noi + timedelta(days=30)
    # Estimates stage is issued after stage 1; its own 30-day observation follows.
    estimates_obs_end = obs1_end + timedelta(days=30)
    t = _today()
    return {
        "ok": True,
        "notice_of_intention": noi.isoformat(),
        "stage1_observation_ends": obs1_end.isoformat(),
        "earliest_estimates_stage": obs1_end.isoformat(),
        "stage2_observation_ends": estimates_obs_end.isoformat(),
        "earliest_works_commitment": estimates_obs_end.isoformat(),
        "days_until_can_commit": (estimates_obs_end - t).days,
        "basis": "Service Charges (Consultation Requirements) (England) Regulations 2003; Daejan v Benson",
    }


def obligations_summary(today: str | None = None) -> dict[str, Any]:
    """A compact summary for the dashboard / digest / brain tool: counts by band + the next due item."""
    obs = upcoming_obligations(today)
    by_band: dict[str, int] = {}
    for o in obs:
        by_band[o["band"]] = by_band.get(o["band"], 0) + 1
    due_soon = [o for o in obs if o["band"] in ("overdue", "due_soon")]
    return {
        "obligations": obs,
        "counts": by_band,
        "due_soon": due_soon,
        "next": obs[0] if obs else None,
        "needs_attention": bool(due_soon),
    }
