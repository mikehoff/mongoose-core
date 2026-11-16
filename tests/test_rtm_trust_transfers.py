"""Acceptance tests for transfer monitoring (Layer 2 section 5; acceptance 7.9, 7.10, 7.11, 7.13)."""
from __future__ import annotations

from decimal import Decimal


from mongoose.services import rtm_trust as T


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))


def test_matched_sweep_pairs_debit_and_credit(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    # a sweep running -> service_charge_trust: debit on running, matching credit on trust within window
    T.ingest_feed_line(account="running", occurred_on="2026-07-02", direction="out",
                       amount=Decimal("300.00"), description="sweep to trust")
    T.ingest_feed_line(account="service_charge_trust", occurred_on="2026-07-03", direction="in",
                       amount=Decimal("300.00"), description="from running")
    matched = T.match_transfers(as_of="2026-07-10")
    assert len(matched) == 1
    m = matched[0]
    assert m["from"] == "running" and m["to"] == "service_charge_trust"
    assert m["amount_pence"] == 30000


def test_lone_debit_is_transfer_incomplete_not_a_match(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    # a debit with no matching credit -> transfer_incomplete, never auto-resolved
    T.ingest_feed_line(account="running", occurred_on="2026-07-02", direction="out",
                       amount=Decimal("300.00"), description="sweep out, no landing")
    matched = T.match_transfers(as_of="2026-07-10")
    assert matched == []
    status = T.transfer_status(as_of="2026-07-10")
    assert any(f["flag"] == "transfer_incomplete" for f in status["flags"])


def test_reserve_raid_is_flagged(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    # reserve_fund_trust -> running sweep, then an OPERATING-cost supplier payment (not works)
    T.ingest_feed_line(account="reserve_fund_trust", occurred_on="2026-07-02", direction="out",
                       amount=Decimal("500.00"), description="reserve to running")
    T.ingest_feed_line(account="running", occurred_on="2026-07-02", direction="in",
                       amount=Decimal("500.00"), description="from reserve")
    # operating-cost payment from running shortly after
    T.record_running(direction="out", amount=Decimal("500.00"), kind="supplier_payment",
                     occurred_on="2026-07-04", counterparty="CleanCo",
                     notes="operating: cleaning")
    status = T.transfer_status(as_of="2026-07-10")
    raids = [f for f in status["flags"] if f["flag"] == "reserve_raid"]
    assert raids, "reserve raid should be flagged when reserve money funds an operating cost"


def test_reserve_funded_works_is_not_a_raid(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    # reserve -> running, then a WORKS payment: legitimate, no raid flag
    T.ingest_feed_line(account="reserve_fund_trust", occurred_on="2026-07-02", direction="out",
                       amount=Decimal("500.00"), description="reserve to running")
    T.ingest_feed_line(account="running", occurred_on="2026-07-02", direction="in",
                       amount=Decimal("500.00"), description="from reserve")
    T.record_running(direction="out", amount=Decimal("500.00"), kind="supplier_payment",
                     occurred_on="2026-07-04", counterparty="RoofWorks Ltd",
                     notes="works: cyclical roof repair")
    status = T.transfer_status(as_of="2026-07-10")
    assert not any(f["flag"] == "reserve_raid" for f in status["flags"])


def test_unfunded_supplier_payment_is_flagged(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    # a supplier paid from running with no matching inbound trust->running sweep
    T.record_running(direction="out", amount=Decimal("250.00"), kind="supplier_payment",
                     occurred_on="2026-07-04", counterparty="ElecCo", notes="operating: electricity")
    status = T.transfer_status(as_of="2026-07-10")
    assert any(f["flag"] == "unfunded_supplier_payment" for f in status["flags"])


def test_expected_protect_sweep_when_trust_money_exposed(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    T.record_running(direction="in", amount=Decimal("300.00"), kind="income",
                     trust_tagged=True, occurred_on="2026-07-10")
    exp = T.expected_transfers(as_of="2026-07-12")
    protect = [e for e in exp if e["kind"] == "protect"]
    assert protect and protect[0]["from"] == "running" and protect[0]["to"] == "service_charge_trust"


def test_module_moves_no_money(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    guarantee = T.moves_no_money()
    assert guarantee["moves_money"] is False
    assert "never moves money" in guarantee["statement"].lower()


def test_no_function_issues_a_bank_instruction():
    """Acceptance 7.13: nothing in the module initiates, confirms, or simulates a bank instruction.

    The module's vocabulary is record / reconcile / report / flag / render. There must be no
    transfer-execution verbs (execute_transfer, send_payment, initiate, pay_supplier that pays, etc.).
    """
    import inspect
    from mongoose.services import rtm_trust as T
    src = inspect.getsource(T)
    banned = ["def execute_transfer", "def send_payment", "def initiate_payment",
              "def make_payment", "def transfer_funds(", "def pay(", "bank_api", "payment_gateway",
              "faster_payment", "bacs_submit"]
    hits = [b for b in banned if b in src]
    assert not hits, f"money-moving capability found: {hits}"
    # the public functions are all record/report shaped
    public = [n for n, _ in inspect.getmembers(T, inspect.isfunction) if not n.startswith("_")]
    assert "moves_no_money" in public
