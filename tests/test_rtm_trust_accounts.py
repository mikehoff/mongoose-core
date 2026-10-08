"""Acceptance tests for the turnstile account model (Layer 2 sections 1, 7.1, 7.12)."""
from __future__ import annotations

from mongoose.services import rtm_trust as T


def test_three_ledger_accounts_with_correct_trust_tags():
    accts = {a["account"]: a for a in T.accounts()}
    assert set(accts) == {"running", "service_charge_trust", "reserve_fund_trust"}
    # running is the only non-trust, only world-facing account
    assert accts["running"]["trust"] is False
    assert accts["running"]["world_facing"] is True
    assert accts["service_charge_trust"]["trust"] is True
    assert accts["service_charge_trust"]["world_facing"] is False
    assert accts["reserve_fund_trust"]["trust"] is True
    assert accts["reserve_fund_trust"]["world_facing"] is False


def test_only_running_faces_the_outside():
    world_facing = [a["account"] for a in T.accounts() if a["world_facing"]]
    assert world_facing == ["running"]


def test_reserve_trust_alias_resolves_to_existing_fund():
    # The spec calls it "reserve_trust"; the canonical ledger account is the existing
    # "reserve_fund_trust". Both the alias and bare "reserve" must resolve there.
    assert T._account("reserve_trust") == "reserve_fund_trust"
    assert T._account("reserve") == "reserve_fund_trust"
    assert T._account("reserve_fund_trust") == "reserve_fund_trust"
    assert T.is_trust("reserve_trust") is True


def test_running_is_recognised_even_though_trust_ledger_excludes_it():
    assert T._account("running") == "running"
    assert T.is_trust("running") is False
    # the trust-only bookkeeper must NOT know running (no commingling at the data layer)
    from mongoose.services import rtm_bookkeeper as BK
    assert "running" not in BK.FUNDS


def test_trust_balances_never_reported_as_running_or_operating_cash():
    # The running account note must mark it target-zero, and reserve position is reported separately
    # from any operating cash figure. Reserve balance is never labelled available operating cash.
    rec = T.reconcile_weekly(as_of="2026-07-20")
    assert "running" in rec["accounts"]
    assert "reserve" in rec
    # reserve block has its own keys, distinct from running's balance
    assert set(rec["reserve"]) >= {"collected", "transferred", "in_transit", "reserve_trust_balance"}
    # running is explicitly flagged target-zero, not a store of trust money
    assert "target-zero" in rec["accounts"]["running"]["note"].lower()
