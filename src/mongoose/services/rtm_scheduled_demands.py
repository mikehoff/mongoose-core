"""Scheduled half-year demand runs.

The service-charge year is billed in two half-year on-account instalments (lease clause 12.11: each half
is a defined instalment = half the approved annual budget, not a re-approval). The lease payment days are
1 January and 1 July (clause 12.10). So the NEXT half-year is already approved; it only needs issuing at
the right time.

This module lets the owner SCHEDULE the next run: on a chosen prepare-date (a fortnight or so ahead of the
gale day) Mongoose runs a DRY-RUN preview of the exact deed-exact figures and NOTIFIES the owner to review
and confirm. It deliberately does NOT auto-issue: raising a service-charge demand to 18 leaseholders is
legally significant (s.21B summary, s.47/s.48, and there is live FTT litigation), so a human confirms the
actual issue + send. The preview uses the SAME code path as the real run (derive_demands dry_run=True), so
what the owner reviews is exactly what will be issued.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from ..db import get_app_state, set_app_state, utc_now_iso

_KEY = "rtm_scheduled_demand_run_v1"


def get_schedule() -> dict[str, Any] | None:
    raw = get_app_state(_KEY, "") or ""
    if not raw:
        return None
    try:
        rec = json.loads(raw)
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


def set_schedule(*, year: str, period: str, doc_prefix: str, prepare_on: str,
                 issue_date: str, due_date: str, payable_label: str = "",
                 numerator: int = 1, denominator: int = 2) -> dict[str, Any]:
    """Schedule the next half-year demand run. Overwrites any existing schedule (one pending run at a time).
    prepare_on/issue_date/due_date are YYYY-MM-DD. status: scheduled -> prepared -> issued/cancelled."""
    rec = {
        "year": str(year), "period": str(period), "doc_prefix": str(doc_prefix),
        "numerator": int(numerator), "denominator": int(denominator),
        "prepare_on": str(prepare_on)[:10], "issue_date": str(issue_date)[:10], "due_date": str(due_date)[:10],
        "payable_label": payable_label or str(due_date)[:10],
        "status": "scheduled", "created_at": utc_now_iso(),
        "prepared_at": "", "issued_at": "", "last_preview": None,
    }
    set_app_state(_KEY, json.dumps(rec))
    return rec


def cancel_schedule() -> dict[str, Any]:
    rec = get_schedule()
    if not rec:
        return {"ok": False, "error": "no scheduled demand run"}
    rec["status"] = "cancelled"
    set_app_state(_KEY, json.dumps(rec))
    return {"ok": True, "cancelled": rec.get("period")}


def preview(rec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dry-run the scheduled (or given) run: exact per-flat figures, nothing written."""
    rec = rec or get_schedule()
    if not rec:
        return {"ok": False, "error": "no scheduled demand run"}
    from . import rtm_apportionment as A
    return A.derive_demands(
        year=rec["year"], period=rec["period"], numerator=int(rec.get("numerator", 1)),
        denominator=int(rec.get("denominator", 2)), doc_prefix=rec["doc_prefix"],
        issue_date=rec.get("issue_date") or None, due_date=rec.get("due_date") or None, dry_run=True)


def _today_iso() -> str:
    return date.today().isoformat()


async def run_prepare(now_iso: str | None = None) -> dict[str, Any]:
    """Scheduler entry: on/after prepare_on, run the preview once and notify the owner to confirm. No write
    to the ledger, no send to leaseholders. Idempotent via the status flag (fires once, then 'prepared')."""
    rec = get_schedule()
    if not rec:
        return {"ok": True, "skipped": "no schedule"}
    if rec.get("status") != "scheduled":
        return {"ok": True, "skipped": f"status={rec.get('status')}"}
    today = (now_iso or _today_iso())[:10]
    if today < str(rec.get("prepare_on", "")):
        return {"ok": True, "skipped": "not yet", "prepare_on": rec.get("prepare_on")}

    prev = preview(rec)
    if not prev.get("ok"):
        return {"ok": False, "error": prev.get("error") or "preview failed", "refused": prev.get("refused")}

    total = prev.get("demanded_total", "")
    n = prev.get("flats", 0)
    payable = rec.get("payable_label") or rec.get("due_date")
    period = rec.get("period")
    body = (f"The {period} service-charge demands are ready to raise: {total} across {n} flats, "
            f"payable {payable} (lease clause 12.10). This is the second half of the approved annual budget "
            f"(clause 12.11) - no re-approval needed. Review the figures and confirm to issue and send. "
            f"Nothing has been issued or sent yet.")
    # Durable owner message on the bus + a phone push.
    try:
        from . import bus
        bus.send_message(sender="mongoose", to="owner",
                         subject=f"{period} demands ready to raise ({total})", body=body, type="note")
    except Exception:
        pass
    pushed = False
    try:
        from .notifications import send_to_phone
        r = await send_to_phone(f"{period} demands ready", body[:300], priority="high", tags="money")
        pushed = bool(r.get("sent"))
    except Exception:
        pass

    rec["status"] = "prepared"
    rec["prepared_at"] = utc_now_iso()
    rec["last_preview"] = {"demanded_total": total, "flats": n, "demanded_total_pence": prev.get("demanded_total_pence")}
    set_app_state(_KEY, json.dumps(rec))
    return {"ok": True, "prepared": period, "demanded_total": total, "flats": n, "pushed": pushed}


def issue(*, confirm: bool = False) -> dict[str, Any]:
    """Confirm and ISSUE the scheduled run: writes the demands to the ledger (deed-exact). Sending the
    documents to leaseholders is the SEPARATE, already-confirm-gated batch mailer, so this only issues.
    confirm=True is required (a human reviewed the preview)."""
    rec = get_schedule()
    if not rec:
        return {"ok": False, "error": "no scheduled demand run"}
    if rec.get("status") == "issued":
        return {"ok": True, "already_issued": True, "period": rec.get("period"), "issued_at": rec.get("issued_at")}
    if rec.get("status") == "cancelled":
        return {"ok": False, "error": "this run was cancelled"}
    if not confirm:
        prev = preview(rec)
        return {**prev, "issued": False, "reason": "confirm_required",
                "note": f"Preview only. Re-run with confirm=True to issue {rec.get('period')} to the ledger, "
                        f"then send with the demand mailer."}
    from . import rtm_apportionment as A
    res = A.derive_demands(
        year=rec["year"], period=rec["period"], numerator=int(rec.get("numerator", 1)),
        denominator=int(rec.get("denominator", 2)), doc_prefix=rec["doc_prefix"],
        issue_date=rec.get("issue_date") or None, due_date=rec.get("due_date") or None, dry_run=False)
    if res.get("ok"):
        rec["status"] = "issued"
        rec["issued_at"] = utc_now_iso()
        set_app_state(_KEY, json.dumps(rec))
    return {**res, "issued": bool(res.get("ok")),
            "next_step": "Generate the demand PDFs and send with the payment-due batch mailer (dry-run first)."}


def status() -> dict[str, Any]:
    rec = get_schedule()
    if not rec:
        return {"ok": True, "scheduled": False}
    return {"ok": True, "scheduled": True, **rec}
