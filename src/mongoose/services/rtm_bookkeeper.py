from __future__ import annotations

import csv
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from ..db import connect, utc_now_iso

FUNDS = {"service_charge_trust", "reserve_fund_trust"}
DIRECTIONS = {"in", "out", "opening"}


def _ensure_tables() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS rtm_book_transactions (
                id TEXT PRIMARY KEY,
                fund TEXT NOT NULL,
                occurred_on TEXT NOT NULL,
                description TEXT NOT NULL,
                amount_pence INTEGER NOT NULL,
                direction TEXT NOT NULL,
                category TEXT NOT NULL,
                counterparty TEXT,
                reference TEXT,
                transfer_group TEXT,
                source TEXT,
                notes TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rtm_book_tx_fund_date ON rtm_book_transactions(fund, occurred_on, created_at);

            CREATE TABLE IF NOT EXISTS rtm_bank_balances (
                id TEXT PRIMARY KEY,
                fund TEXT NOT NULL,
                as_at TEXT NOT NULL,
                balance_pence INTEGER NOT NULL,
                bank_account_name TEXT,
                source TEXT,
                notes TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rtm_balances_fund_date ON rtm_bank_balances(fund, as_at, created_at);
            """
        )


def _money_to_pence(amount: str | float | int) -> int:
    """Convert pounds to integer pence using Decimal (no float drift). Integers and floats are treated
    as POUNDS consistently, no magnitude-guessing (a plain 1500 is £1,500.00, not £15.00; the old
    heuristic silently turned 2100 into £21.00). Strings may include £ and commas. This is
    FTT-sensitive trust money, so it must be exact and predictable. Mirrors rtm_management._money_to_pence."""
    from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
    if isinstance(amount, bool):  # guard: bool is a subclass of int
        raise ValueError("amount must be a number, not a bool")
    if isinstance(amount, int):
        return amount * 100
    if isinstance(amount, float):
        d = Decimal(str(amount))
        return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    s = str(amount).strip().replace("£", "").replace(",", "")
    if not s:
        raise ValueError("amount required")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"not a valid money amount: {amount!r}")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _money(pence: int) -> str:
    sign = "-" if pence < 0 else ""
    pence = abs(int(pence))
    return f"{sign}£{pence//100:,}.{pence%100:02d}"


def _fund(fund: str) -> str:
    f = (fund or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"service": "service_charge_trust", "service_charge": "service_charge_trust",
               "reserve": "reserve_fund_trust", "reserve_fund": "reserve_fund_trust"}
    f = aliases.get(f, f)
    if f not in FUNDS:
        raise ValueError(f"fund must be one of {sorted(FUNDS)}")
    return f


def _today() -> str:
    return date.today().isoformat()


def _parse_day(value: str | None) -> str:
    if not value:
        return _today()
    return date.fromisoformat(str(value)[:10]).isoformat()


def record_transaction(*, fund: str, amount: str | float | int, direction: str,
                       occurred_on: str | None = None, description: str = "",
                       category: str = "uncategorised", counterparty: str = "",
                       reference: str = "", source: str = "manual", notes: str = "",
                       transfer_group: str = "") -> dict[str, Any]:
    _ensure_tables()
    fund = _fund(fund)
    direction = (direction or "").strip().lower()
    if direction not in DIRECTIONS:
        raise ValueError("direction must be in, out or opening")
    amount_pence = abs(_money_to_pence(amount))
    tx_id = "tx_" + uuid.uuid4().hex[:12]
    with connect() as con:
        con.execute(
            """
            INSERT INTO rtm_book_transactions(id,fund,occurred_on,description,amount_pence,direction,
                category,counterparty,reference,transfer_group,source,notes,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (tx_id, fund, _parse_day(occurred_on), description or category, amount_pence, direction,
             category, counterparty, reference, transfer_group, source, notes, utc_now_iso()),
        )
    return {"ok": True, "id": tx_id, "fund": fund, "amount": _money(amount_pence), "direction": direction}


def record_opening_balance(fund: str, amount: str | float | int, as_at: str | None = None, notes: str = "") -> dict[str, Any]:
    return record_transaction(fund=fund, amount=amount, direction="opening", occurred_on=as_at,
                              description="Opening balance", category="opening_balance", notes=notes)


def record_bank_balance(*, fund: str, balance: str | float | int, as_at: str | None = None,
                        bank_account_name: str = "", source: str = "manual_weekly_check", notes: str = "") -> dict[str, Any]:
    _ensure_tables()
    fund = _fund(fund)
    bal_id = "bal_" + uuid.uuid4().hex[:12]
    with connect() as con:
        con.execute(
            """
            INSERT INTO rtm_bank_balances(id,fund,as_at,balance_pence,bank_account_name,source,notes,created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (bal_id, fund, _parse_day(as_at), _money_to_pence(balance), bank_account_name, source, notes, utc_now_iso()),
        )
    return {"ok": True, "id": bal_id, "fund": fund}


def record_transfer(*, from_fund: str, to_fund: str, amount: str | float | int,
                    occurred_on: str | None = None, description: str = "Transfer between trust funds",
                    reference: str = "", notes: str = "") -> dict[str, Any]:
    from_fund = _fund(from_fund)
    to_fund = _fund(to_fund)
    if from_fund == to_fund:
        raise ValueError("from_fund and to_fund must differ")
    group = "tr_" + uuid.uuid4().hex[:12]
    out = record_transaction(fund=from_fund, amount=amount, direction="out", occurred_on=occurred_on,
                             description=description, category="inter_fund_transfer", counterparty=to_fund,
                             reference=reference, notes=notes, transfer_group=group)
    inc = record_transaction(fund=to_fund, amount=amount, direction="in", occurred_on=occurred_on,
                             description=description, category="inter_fund_transfer", counterparty=from_fund,
                             reference=reference, notes=notes, transfer_group=group)
    return {"ok": True, "transfer_group": group, "out": out, "in": inc}


def _signed_amount(row: dict[str, Any]) -> int:
    if row["direction"] in ("in", "opening"):
        return int(row["amount_pence"])
    return -int(row["amount_pence"])


def ledger(fund: str | None = None, limit: int = 500) -> dict[str, Any]:
    _ensure_tables()
    params: list[Any] = []
    where = ""
    if fund:
        where = "WHERE fund = ?"
        params.append(_fund(fund))
    with connect() as con:
        rows = con.execute(
            f"SELECT * FROM rtm_book_transactions {where} ORDER BY occurred_on, created_at, id LIMIT ?",
            (*params, max(1, min(limit, 5000))),
        ).fetchall()
    running: dict[str, int] = {f: 0 for f in FUNDS}
    entries = []
    for r in rows:
        d = dict(r)
        running[d["fund"]] = running.get(d["fund"], 0) + _signed_amount(d)
        d["signed_amount_pence"] = _signed_amount(d)
        d["running_balance_pence"] = running[d["fund"]]
        d["amount"] = _money(d["amount_pence"])
        d["signed_amount"] = _money(d["signed_amount_pence"])
        d["running_balance"] = _money(d["running_balance_pence"])
        entries.append(d)
    return {"entries": entries, "balances": {k: _money(v) for k, v in running.items()},
            "balances_pence": running}


def latest_bank_balances() -> dict[str, dict[str, Any]]:
    _ensure_tables()
    out: dict[str, dict[str, Any]] = {}
    with connect() as con:
        for f in sorted(FUNDS):
            row = con.execute(
                "SELECT * FROM rtm_bank_balances WHERE fund=? ORDER BY as_at DESC, created_at DESC LIMIT 1", (f,)
            ).fetchone()
            if row:
                d = dict(row)
                d["balance"] = _money(d["balance_pence"])
                out[f] = d
    return out


def reconcile() -> dict[str, Any]:
    led = ledger(limit=5000)
    latest = latest_bank_balances()
    funds = []
    for f in sorted(FUNDS):
        book_pence = led["balances_pence"].get(f, 0)
        bank = latest.get(f)
        if not bank:
            funds.append({"fund": f, "status": "missing_bank_balance", "book_balance": _money(book_pence),
                          "bank_balance": "", "difference": "", "reason": "No weekly bank balance has been recorded yet."})
            continue
        diff = int(bank["balance_pence"]) - book_pence
        status = "reconciled" if diff == 0 else "mismatch"
        funds.append({"fund": f, "status": status, "book_balance": _money(book_pence),
                      "bank_balance": _money(bank["balance_pence"]), "difference": _money(diff),
                      "as_at": bank["as_at"], "reason": "Bank balance equals ledger" if diff == 0 else "Investigate missing/duplicated transactions or opening balance."})
    return {"funds": funds, "overall": "ok" if all(f["status"] == "reconciled" for f in funds) else "attention"}


def weekly_prompt() -> dict[str, Any]:
    questions = [
        "What is the current Service Charge Account bank balance?",
        "What is the current Reserve Fund Account bank balance?",
        "Any service-charge income received this week (flat, amount, reference)?",
        "Any supplier expenses paid this week (supplier, amount, invoice/reference, fund)?",
        "Any transfers between Service Charge and Reserve Fund?",
        "Any items still uncleared, disputed, refunded, or paid personally pending reimbursement?",
    ]
    return {"title": "RTM weekly bookkeeping check-in", "questions": questions,
            "note": "Record trust fund money separately. Do not mix company/admin funds with service charge or reserve funds."}


def summary() -> dict[str, Any]:
    led = ledger(limit=5000)
    rec = reconcile()
    with connect() as con:
        tx_count = con.execute("SELECT COUNT(*) c FROM rtm_book_transactions").fetchone()["c"]
        bal_count = con.execute("SELECT COUNT(*) c FROM rtm_bank_balances").fetchone()["c"]
    return {"ledger_balances": led["balances"], "reconciliation": rec, "transaction_count": tx_count,
            "bank_balance_count": bal_count, "weekly_prompt": weekly_prompt()}


def chart() -> str:
    s = summary()
    lines = ["RTM trust bookkeeping", ""]
    max_abs = max([abs(v) for v in ledger(limit=5000)["balances_pence"].values()] + [1])
    for f, bal in ledger(limit=5000)["balances_pence"].items():
        n = max(1, int(abs(bal) / max_abs * 28)) if bal else 0
        bar = "█" * n
        lines.append(f"{f:22} {_money(bal):>12} {bar}")
    lines.append("")
    for r in s["reconciliation"]["funds"]:
        lines.append(f"[{r['status']}] {r['fund']}: book {r['book_balance']} / bank {r.get('bank_balance') or 'not recorded'} / diff {r.get('difference') or '?'}")
    return "\n".join(lines)


def export_csv(path: str | Path, fund: str | None = None) -> dict[str, Any]:
    data = ledger(fund=fund, limit=5000)["entries"]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["occurred_on", "fund", "direction", "category", "description", "counterparty", "reference",
              "amount", "signed_amount", "running_balance", "source", "notes", "id"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for d in data:
            w.writerow({k: d.get(k, "") for k in fields})
    return {"ok": True, "path": str(path), "count": len(data),
            "note": "Bookkeeper-ready CSV, not a signed statutory account. Review with accountant/director."}


def format_summary(s: dict[str, Any] | None = None) -> str:
    s = s or summary()
    lines = ["RTM bookkeeper: service charge and reserve trust funds", ""]
    for fund, bal in s["ledger_balances"].items():
        lines.append(f"  {fund:22} ledger balance {bal}")
    lines.append("")
    lines.append("Reconciliation:")
    for r in s["reconciliation"]["funds"]:
        lines.append(f"  [{r['status']}] {r['fund']}: book {r['book_balance']} / bank {r.get('bank_balance') or 'not recorded'} / diff {r.get('difference') or '?'}")
    lines.append("")
    lines.append("Weekly questions:")
    for q in s["weekly_prompt"]["questions"]:
        lines.append(f"  - {q}")
    return "\n".join(lines)
