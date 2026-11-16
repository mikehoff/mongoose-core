"""Payment on account: advance payment for a not-yet-demanded budget, and overpayment excess.

The owner's rule is that any money beyond the current demand is ALWAYS a payment on account (there are no
refunds): a standing credit carried forward and applied to the next demand raised. Two walls blocked this
before — record_payment refused when there was no open demand, and capped at the outstanding so an
overpayment's excess was silently discarded. These tests pin the new behaviour and, crucially, that a
prepaid flat is never chased or counted as arrears.
"""
import pytest

from mongoose.services import rtm_management as M


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    M._ensure_tables()
    M.upsert_flat(block="3EC", flat_number=3, leaseholder_name="Charlotte Ashby", payment_reference="ASHBY")
    return tmp_path


def _bal(fid):
    return M.flat_statement(fid)["balance_pence"]


def test_overpayment_keeps_the_excess_as_credit(db):
    M.create_demand(document_number="D1", flat="3ECF3", period="2026-H2", amount="1500.00",
                    issue_date="2026-07-16", due_date="2026-08-13")
    r = M.record_payment(document_number="D1", amount="2000.00")
    assert r["ok"]
    assert M.refresh_demand_status("D1") == "Paid"
    st = M.flat_statement("3ECF3")
    assert st["position"] == "credit"
    assert st["credit_pence"] == 50000, st        # £500 carried
    assert _bal("3ECF3") == -50000                # nothing lost: 1500 debit - 2000 credit
    assert M.available_on_account_credit("3ECF3") == 50000


def test_advance_payment_with_no_demand_records(db):
    # Previously a hard error ("no open demand for flat").
    r = M.record_payment(flat="3ECF3", amount="900.00")
    assert r["ok"], r
    assert r["position"] == "credit"
    st = M.flat_statement("3ECF3")
    assert st["credit_pence"] == 90000
    assert M.available_on_account_credit("3ECF3") == 90000
    # exactly one on_account ledger credit with a blank document_number
    on_acct = [e for e in st["entries"] if e["kind"] == "on_account" and e["credit_pence"] == 90000]
    assert len(on_acct) == 1
    assert (on_acct[0]["document_number"] or "") == ""


def test_next_demand_auto_consumes_credit(db):
    M.record_payment(flat="3ECF3", amount="900.00")
    assert _bal("3ECF3") == -90000
    r = M.create_demand(document_number="D2", flat="3ECF3", period="2026-H2", amount="900.00",
                        issue_date="2026-07-16", due_date="2026-08-13")
    assert M.allocated_to_demand("D2") == 90000
    assert M.refresh_demand_status("D2") == "Paid"
    assert _bal("3ECF3") == 0                       # -900 credit + 900 demand = clear
    # applied via a credit-application row, NOT a second ledger credit
    assert r["on_account_applied"] is not None


def test_partial_and_over_cases(db):
    # credit < demand -> Part-paid, flat becomes a debit of the shortfall
    M.record_payment(flat="3ECF3", amount="900.00")
    M.create_demand(document_number="D3", flat="3ECF3", period="2026-H2", amount="1500.00",
                    issue_date="2026-07-16", due_date="2026-08-13")
    assert M.refresh_demand_status("D3") == "Part-paid"
    assert _bal("3ECF3") == 60000                   # owes £600

    # a bigger prepayment than the next demand -> demand Paid, surplus stays as credit
    M.upsert_flat(block="3EC", flat_number=5, leaseholder_name="Brett Hollis", payment_reference="HOLLIS")
    M.record_payment(flat="3ECF5", amount="2000.00")
    M.create_demand(document_number="D4", flat="3ECF5", period="2026-H2", amount="1500.00",
                    issue_date="2026-07-16", due_date="2026-08-13")
    assert M.refresh_demand_status("D4") == "Paid"
    assert M.available_on_account_credit("3ECF5") == 50000   # £500 carried to the next demand


def test_prepaid_flat_is_not_overdue(db):
    # A demand fully covered by prepayment must read Paid, never Overdue, even past its due date.
    M.record_payment(flat="3ECF3", amount="900.00")
    M.create_demand(document_number="D5", flat="3ECF3", period="2026-H2", amount="900.00",
                    issue_date="2026-01-01", due_date="2026-01-28")
    assert M.refresh_demand_status("D5", today="2026-09-01") == "Paid"


def test_balance_is_continuous_across_auto_apply(db):
    M.record_payment(flat="3ECF3", amount="900.00")
    before = _bal("3ECF3")
    M.create_demand(document_number="D6", flat="3ECF3", period="2026-H2", amount="900.00",
                    issue_date="2026-07-16", due_date="2026-08-13")
    after = _bal("3ECF3")
    # balance moved by exactly the demand debit (+900), and by nothing the auto-apply did
    assert after - before == 90000


def test_reassign_to_fund_keeps_balance(db):
    M.record_payment(flat="3ECF3", amount="900.00")
    before = _bal("3ECF3")
    r = M.assign_on_account_to_fund(flat="3ECF3", fund_schedule="reserve", amount="300.00")
    assert r["ok"], r
    assert _bal("3ECF3") == before                  # reassignment is net-zero to the balance
    assert M.available_on_account_credit("3ECF3") == 90000  # still all available, just partly designated
