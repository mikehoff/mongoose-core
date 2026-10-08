from __future__ import annotations

import csv
import json
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..db import connect, utc_now_iso

# RTM company governance support. This is deliberately a tracker and guidance layer,
# not legal advice and not a filing agent. It stores the facts the director supplies,
# computes conservative deadline reminders, and produces step-by-step artefacts to
# prepare filings. The user/director still files/approves externally.

def _default_company_name() -> str:
    """'<RTM name> RTM Company Limited', the RTM name from the site profile."""
    from .. import profile as _profile
    return f"{_profile.rtm_name()} RTM Company Limited"


def _default_company_number() -> str:
    from .. import profile as _profile
    return _profile.company_number()


def __getattr__(name: str) -> str:
    # The historical module constants, now resolved from the site profile at use time.
    if name == "DEFAULT_COMPANY_NAME":
        return _default_company_name()
    if name == "DEFAULT_COMPANY_NUMBER":
        return _default_company_number()
    raise AttributeError(name)

_STATUS_ORDER = {"overdue": 0, "due_soon": 1, "upcoming": 2, "ongoing": 3, "done": 4}


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def _next_annual_date(anchor: date, today: date | None = None) -> date:
    today = today or _today()
    candidate = anchor
    while candidate < today:
        candidate = _add_years(candidate, 1)
    return candidate


def _money_status(due: date | None, today: date | None = None) -> str:
    if not due:
        return "ongoing"
    today = today or _today()
    if due < today:
        return "overdue"
    if due <= today + timedelta(days=30):
        return "due_soon"
    return "upcoming"


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _ensure_tables() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS rtm_company_profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                company_name TEXT NOT NULL,
                company_number TEXT,
                incorporation_date TEXT,
                accounting_reference_date TEXT,
                confirmation_statement_date TEXT,
                corporation_tax_period_end TEXT,
                dormant_for_corporation_tax INTEGER NOT NULL DEFAULT 0,
                vat_registered INTEGER NOT NULL DEFAULT 0,
                payroll_registered INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rtm_compliance_items (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                authority TEXT NOT NULL,
                title TEXT NOT NULL,
                due_date TEXT,
                period_start TEXT,
                period_end TEXT,
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                guidance_json TEXT NOT NULL,
                last_computed_at TEXT NOT NULL,
                completed_at TEXT,
                completed_note TEXT
            );
            """
        )


def get_profile() -> dict[str, Any]:
    _ensure_tables()
    with connect() as con:
        row = con.execute("SELECT * FROM rtm_company_profile WHERE id = 1").fetchone()
        if row:
            return dict(row)
    return {
        "company_name": _default_company_name(),
        "company_number": _default_company_number(),
        "incorporation_date": "",
        "accounting_reference_date": "",
        "confirmation_statement_date": "",
        "corporation_tax_period_end": "",
        "dormant_for_corporation_tax": 0,
        "vat_registered": 0,
        "payroll_registered": 0,
        "notes": "",
        "updated_at": "",
    }


def configure_profile(**updates: Any) -> dict[str, Any]:
    """Create/update the RTM company profile. Empty values leave existing/defaults in place."""
    _ensure_tables()
    current = get_profile()
    allowed = {
        "company_name", "company_number", "incorporation_date", "accounting_reference_date",
        "confirmation_statement_date", "corporation_tax_period_end", "dormant_for_corporation_tax",
        "vat_registered", "payroll_registered", "notes",
    }
    for k, v in updates.items():
        if k in allowed and v not in (None, ""):
            current[k] = int(v) if k.endswith("_registered") or k == "dormant_for_corporation_tax" else str(v)
    current["company_name"] = current.get("company_name") or _default_company_name()
    current["company_number"] = current.get("company_number") or _default_company_number()
    current["updated_at"] = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO rtm_company_profile(id, company_name, company_number, incorporation_date,
                accounting_reference_date, confirmation_statement_date, corporation_tax_period_end,
                dormant_for_corporation_tax, vat_registered, payroll_registered, notes, updated_at)
            VALUES(1,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET company_name=excluded.company_name,
                company_number=excluded.company_number, incorporation_date=excluded.incorporation_date,
                accounting_reference_date=excluded.accounting_reference_date,
                confirmation_statement_date=excluded.confirmation_statement_date,
                corporation_tax_period_end=excluded.corporation_tax_period_end,
                dormant_for_corporation_tax=excluded.dormant_for_corporation_tax,
                vat_registered=excluded.vat_registered, payroll_registered=excluded.payroll_registered,
                notes=excluded.notes, updated_at=excluded.updated_at
            """,
            (current["company_name"], current.get("company_number", ""), current.get("incorporation_date", ""),
             current.get("accounting_reference_date", ""), current.get("confirmation_statement_date", ""),
             current.get("corporation_tax_period_end", ""), int(current.get("dormant_for_corporation_tax") or 0),
             int(current.get("vat_registered") or 0), int(current.get("payroll_registered") or 0),
             current.get("notes", ""), current["updated_at"]),
        )
    return get_profile()


def _guidance(kind: str) -> list[str]:
    base = {
        "confirmation_statement": [
            "Review registered office, officers, PSCs/members and SIC/company details.",
            "Check whether any changes need filing before/with the confirmation statement.",
            "Prepare CS01/online filing notes and obtain director approval before filing.",
            "Record filing reference and receipt in Mongoose after submission.",
        ],
        "companies_house_accounts": [
            "Confirm accounting reference date and whether accounts are dormant/micro/small/full.",
            "Prepare statutory accounts from the RTM/company books and accountant output if used.",
            "Director approves accounts before filing.",
            "Record filing receipt and update next accounting period.",
        ],
        "corporation_tax_payment": [
            "Check whether the RTM company is dormant/non-trading for Corporation Tax or has taxable activity.",
            "If tax is due, calculate/payment details should come from the accountant or tax software.",
            "If no Corporation Tax is due, ensure HMRC is told where required.",
            "Record payment/told-HMRC evidence.",
        ],
        "company_tax_return": [
            "Prepare CT600/company tax return only from confirmed accounting period figures.",
            "Attach company accounts/tax computations where required by the chosen filing route.",
            "Director/accountant reviews before submission.",
            "Record submission receipt and period covered.",
        ],
        "vat_return": [
            "Only applies if VAT registered. Confirm quarters and Making Tax Digital route.",
            "Reconcile VAT control account before filing.",
            "File/pay through HMRC-compatible software if applicable.",
        ],
        "payroll_rti": [
            "Only applies if payroll registered or PAYE is operated.",
            "Confirm directors/contractors are not being accidentally treated as payroll staff.",
            "Submit RTI/EPS/FPS through payroll software where applicable.",
        ],
        "statutory_registers": [
            "Keep directors, members, PSCs and registered-office records up to date.",
            "Record board/member decisions and major approvals.",
            "File Companies House changes promptly where required.",
        ],
        "service_charge_accounts": [
            "Keep service charge and reserve monies ring-fenced by fund/account.",
            "Reconcile bank statements to the service charge/reserve fund ledgers.",
            "Prepare service-charge receipts/payments and reserve movement report for accountant/director review.",
            "Do not mix company administrative funds with service charge trust funds.",
        ],
    }
    return base.get(kind, ["Review the underlying obligation, gather evidence, obtain approval, and record the outcome."])


def compute_compliance(today: date | None = None) -> dict[str, Any]:
    """Compute/upsert current compliance reminders from the stored company profile.

    The date rules are intentionally conservative and evidence-led. Missing source facts produce
    setup-needed/ongoing watchlist items rather than fake deadlines.
    """
    _ensure_tables()
    today = today or _today()
    p = get_profile()
    inc = _parse_date(p.get("incorporation_date"))
    ard = _parse_date(p.get("accounting_reference_date"))
    csd = _parse_date(p.get("confirmation_statement_date"))
    ct_end = _parse_date(p.get("corporation_tax_period_end"))
    items: list[dict[str, Any]] = []

    if csd or inc:
        review_date = _next_annual_date(csd or inc, today)
        due = review_date + timedelta(days=14)
        items.append(_item("confirmation_statement", "Companies House", "Confirmation statement",
                           due, period_end=review_date, source="computed:confirmation_statement_date_or_incorporation",
                           evidence={"confirmation_statement_date": str(csd or ""), "incorporation_date": str(inc or ""), "review_date": str(review_date)}, today=today))
    else:
        items.append(_item("confirmation_statement", "Companies House", "Confirmation statement date needed",
                           None, source="missing:confirmation_statement_date", evidence={"missing": "confirmation_statement_date or incorporation_date"}, today=today))

    if ard or inc:
        if inc and (not ard or today <= _add_months(inc, 21)):
            first_due = _add_months(inc, 21)
            items.append(_item("companies_house_accounts", "Companies House", "First/next statutory accounts",
                               first_due if first_due >= today - timedelta(days=365) else _add_months(_next_annual_date(ard or inc, today), 9),
                               period_end=ard, source="computed:accounts_deadline", evidence={"incorporation_date": str(inc or ""), "accounting_reference_date": str(ard or ""), "first_accounts_due": str(first_due)}, today=today))
        else:
            period_end = _next_annual_date(ard or inc, today)
            due = _add_months(period_end, 9)
            items.append(_item("companies_house_accounts", "Companies House", "Annual accounts",
                               due, period_end=period_end, source="computed:accounts_deadline", evidence={"accounting_reference_date": str(ard or ""), "period_end": str(period_end)}, today=today))
    else:
        items.append(_item("companies_house_accounts", "Companies House", "Accounts deadline needs ARD/incorporation date",
                           None, source="missing:accounting_reference_date", evidence={"missing": "accounting_reference_date or incorporation_date"}, today=today))

    if ct_end:
        ct_period_end = _next_annual_date(ct_end, today)
        pay_due = _add_months(ct_period_end, 9) + timedelta(days=1)
        return_due = _add_months(ct_period_end, 12)
        items.append(_item("corporation_tax_payment", "HMRC", "Corporation Tax payment / tell HMRC if none due",
                           pay_due, period_end=ct_period_end, source="computed:corporation_tax_period_end", evidence={"period_end": str(ct_period_end), "dormant_for_corporation_tax": bool(p.get("dormant_for_corporation_tax"))}, today=today))
        items.append(_item("company_tax_return", "HMRC", "Company Tax Return (CT600)",
                           return_due, period_end=ct_period_end, source="computed:corporation_tax_period_end", evidence={"period_end": str(ct_period_end)}, today=today))
    else:
        items.append(_item("company_tax_return", "HMRC", "Corporation Tax period end needed",
                           None, source="missing:corporation_tax_period_end", evidence={"missing": "corporation_tax_period_end"}, today=today))

    if p.get("vat_registered"):
        items.append(_item("vat_return", "HMRC", "VAT returns/payments (configured as VAT registered)",
                           None, source="profile:vat_registered", evidence={"vat_registered": True}, today=today))
    if p.get("payroll_registered"):
        items.append(_item("payroll_rti", "HMRC", "PAYE/RTI filings (configured as payroll registered)",
                           None, source="profile:payroll_registered", evidence={"payroll_registered": True}, today=today))
    items.append(_item("statutory_registers", "Companies House / company records", "Statutory registers and company record changes",
                       None, source="ongoing:company_records", evidence={"ongoing": True}, today=today))
    items.append(_item("service_charge_accounts", "RTM / service charge accounting", "Service charge and reserve trust accounting pack",
                       None, source="ongoing:service_charge_trust", evidence={"funds": ["service_charge_trust", "reserve_fund_trust"]}, today=today))

    with connect() as con:
        for it in items:
            old = con.execute("SELECT completed_at, completed_note FROM rtm_compliance_items WHERE id = ?", (it["id"],)).fetchone()
            completed_at = old["completed_at"] if old else None
            completed_note = old["completed_note"] if old else None
            status = "done" if completed_at else it["status"]
            con.execute(
                """
                INSERT INTO rtm_compliance_items(id,kind,authority,title,due_date,period_start,period_end,
                    status,source,evidence_json,guidance_json,last_computed_at,completed_at,completed_note)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET authority=excluded.authority, title=excluded.title,
                    due_date=excluded.due_date, period_start=excluded.period_start,
                    period_end=excluded.period_end, status=excluded.status, source=excluded.source,
                    evidence_json=excluded.evidence_json, guidance_json=excluded.guidance_json,
                    last_computed_at=excluded.last_computed_at, completed_at=excluded.completed_at,
                    completed_note=excluded.completed_note
                """,
                (it["id"], it["kind"], it["authority"], it["title"], it.get("due_date"),
                 it.get("period_start"), it.get("period_end"), status, it["source"], _json(it["evidence"]),
                 _json(it["guidance"]), utc_now_iso(), completed_at, completed_note),
            )
    return status_report()


def _item(kind: str, authority: str, title: str, due: date | None, *, source: str,
          evidence: dict[str, Any], today: date, period_start: date | None = None,
          period_end: date | None = None) -> dict[str, Any]:
    # ID follows kind + period/due so a new year naturally creates a new item while preserving
    # completion history for older periods.
    ident = f"{kind}:{period_end or due or 'ongoing'}"
    status = _money_status(due, today)
    return {
        "id": ident,
        "kind": kind,
        "authority": authority,
        "title": title,
        "due_date": str(due) if due else "",
        "period_start": str(period_start) if period_start else "",
        "period_end": str(period_end) if period_end else "",
        "status": status,
        "source": source,
        "evidence": evidence,
        "guidance": _guidance(kind),
    }


def status_report() -> dict[str, Any]:
    _ensure_tables()
    with connect() as con:
        rows = con.execute("SELECT * FROM rtm_compliance_items ORDER BY CASE status WHEN 'overdue' THEN 0 WHEN 'due_soon' THEN 1 WHEN 'upcoming' THEN 2 WHEN 'ongoing' THEN 3 WHEN 'done' THEN 4 ELSE 5 END, due_date").fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
        d["guidance"] = json.loads(d.pop("guidance_json") or "[]")
        items.append(d)
    counts: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    return {"profile": get_profile(), "items": items, "counts": counts,
            "note": "Tracker only: verify deadlines with Companies House/HMRC/accountant before filing."}


def mark_done(item_id: str, note: str = "") -> dict[str, Any]:
    _ensure_tables()
    with connect() as con:
        row = con.execute("SELECT id FROM rtm_compliance_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            compute_compliance()
            row = con.execute("SELECT id FROM rtm_compliance_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "unknown compliance item"}
        con.execute("UPDATE rtm_compliance_items SET status='done', completed_at=?, completed_note=? WHERE id=?",
                    (utc_now_iso(), note[:500], item_id))
    return {"ok": True, "item_id": item_id}


def guidance(item_id: str) -> dict[str, Any]:
    rep = status_report()
    for it in rep["items"]:
        if it["id"] == item_id:
            return {"ok": True, "item": it, "steps": it.get("guidance", [])}
    return {"ok": False, "error": "unknown compliance item"}


def export_calendar_csv(path: str | Path) -> dict[str, Any]:
    rep = status_report()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "title", "authority", "kind", "due_date", "status", "next_action"])
        w.writeheader()
        for it in rep["items"]:
            w.writerow({"id": it["id"], "title": it["title"], "authority": it["authority"],
                        "kind": it["kind"], "due_date": it.get("due_date", ""), "status": it["status"],
                        "next_action": (it.get("guidance") or [""])[0]})
    return {"ok": True, "path": str(path), "count": len(rep["items"])}


def artifact_pack(out_dir: str | Path) -> dict[str, Any]:
    rep = status_report()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for it in rep["items"]:
        safe = it["id"].replace(":", "_").replace("/", "_")
        p = out_dir / f"{safe}.md"
        lines = [f"# {it['title']}", "", f"Authority: {it['authority']}",
                 f"Status: {it['status']}", f"Due date: {it.get('due_date') or 'ongoing / not yet known'}", "",
                 "## Evidence", "", "```json", json.dumps(it.get("evidence", {}), indent=2), "```", "",
                 "## Step-by-step", ""]
        lines.extend(f"{i}. {step}" for i, step in enumerate(it.get("guidance", []), 1))
        lines.extend(["", "## Completion notes", "", "- Filing/submission reference:", "- Date completed:", "- Who approved:", "- Where evidence is stored:", ""])
        p.write_text("\n".join(lines), encoding="utf-8")
        written.append(str(p))
    return {"ok": True, "dir": str(out_dir), "files": written}


def format_status(rep: dict[str, Any] | None = None) -> str:
    rep = rep or status_report()
    p = rep.get("profile", {})
    lines = [f"RTM company secretary tracker: {p.get('company_name')} ({p.get('company_number') or 'no company number'})"]
    if not rep.get("items"):
        lines.append("No compliance items computed yet. Run: mongoose rtm secretary discover")
        return "\n".join(lines)
    for it in rep["items"]:
        due = it.get("due_date") or "ongoing"
        lines.append(f"  [{it['status']}] {due:10} {it['authority']}: {it['title']}  ({it['id']})")
    lines.append("\nTracker only: verify deadlines with Companies House/HMRC/accountant before filing.")
    return "\n".join(lines)
