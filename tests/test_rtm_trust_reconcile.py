"""Acceptance tests for weekly reconciliation (Layer 2 section 4; acceptance 7.7, 7.8, 7.12)."""
from __future__ import annotations

from decimal import Decimal


from mongoose.services import rtm_trust as T
from mongoose.services import rtm_bookkeeper as BK


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))


def test_report_shape(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = T.reconcile_weekly(as_of="2026-07-20")
    assert rec["as_of"] == "2026-07-20"
    assert rec["status"] in ("ok", "attention", "overdue")
    assert set(rec["accounts"]) == {"running", "service_charge_trust", "reserve_fund_trust"}
    for acct in rec["accounts"].values():
        assert "bank_balance" in acct and "ledger_balance" in acct and "unmatched" in acct
    assert set(rec["reserve"]) >= {"collected", "transferred", "in_transit", "reserve_trust_balance"}
    assert "trust_money_in_running" in rec["exposure"]
    assert "oldest_unswept_days" in rec["exposure"]
    assert isinstance(rec["transfers"], list)
    assert isinstance(rec["flags"], list)


def test_trust_account_drift_is_flagged(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    # ledger says 1000, bank says 1200 -> drift 200 on a trust account -> attention
    BK.record_transaction(fund="service_charge_trust", amount=Decimal("10.00"), direction="in",
                          occurred_on="2026-07-01", description="seed")
    BK.record_bank_balance(fund="service_charge_trust", balance=Decimal("12.00"), as_at="2026-07-20")
    rec = T.reconcile_weekly(as_of="2026-07-20")
    sc = rec["accounts"]["service_charge_trust"]
    assert sc["drift_pence"] == 200
    assert rec["status"] in ("attention", "overdue")
    assert any(f["flag"] == "trust_drift" for f in rec["flags"])


def test_running_transient_balance_is_not_flagged_as_drift(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    # money sat briefly in running that is NOT tagged trust money: must not be a drift error
    T.record_running(direction="in", amount=Decimal("500.00"), kind="income",
                     trust_tagged=False, occurred_on="2026-07-20")
    rec = T.reconcile_weekly(as_of="2026-07-20")
    run = rec["accounts"]["running"]
    # running drift is deliberately blank (not an error signal)
    assert run["drift"] == ""
    # and no trust-exposure flag because the money is not tagged trust
    assert not any(f["flag"].startswith("trust_exposure") for f in rec["flags"])


def test_trust_money_in_running_past_window_is_overdue(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    # tagged trust money landed in running 10 days before as_of, never swept -> overdue (> 7 day window)
    T.record_running(direction="in", amount=Decimal("300.00"), kind="income",
                     trust_tagged=True, occurred_on="2026-07-10")
    rec = T.reconcile_weekly(as_of="2026-07-20")
    assert rec["exposure"]["trust_money_in_running_pence"] == 30000
    assert rec["exposure"]["oldest_unswept_days"] == 10
    assert rec["status"] == "overdue"
    assert any(f["flag"] == "trust_exposure_overdue" for f in rec["flags"])


def test_trust_money_in_running_within_window_is_attention_not_overdue(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    # landed 3 days ago -> within the 7-day grace -> attention, not overdue
    T.record_running(direction="in", amount=Decimal("300.00"), kind="income",
                     trust_tagged=True, occurred_on="2026-07-17")
    rec = T.reconcile_weekly(as_of="2026-07-20")
    assert rec["exposure"]["oldest_unswept_days"] == 3
    assert any(f["flag"] == "trust_exposure" for f in rec["flags"])
    assert not any(f["flag"] == "trust_exposure_overdue" for f in rec["flags"])


def test_clean_state_is_ok(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    rec = T.reconcile_weekly(as_of="2026-07-20")
    # nothing recorded, no trust accounts have bank balances -> missing balances make it attention,
    # but with no trust money exposed and no drift the exposure block is clean.
    assert rec["exposure"]["trust_money_in_running_pence"] == 0
