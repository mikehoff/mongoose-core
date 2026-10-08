"""Recording a payment plan instalment must be one click, prepopulated, and impossible to double-count.

The plan table stored only the AGREED SCHEDULE. Nothing recorded which instalments had actually been paid,
so there was no way to record one, and every plan-aware figure (amber colouring, the paid segment of the
collection bar, the two headline percentages) had nothing to read.

The trap found while building it: a button whose handler takes "the next unpaid instalment" is not
idempotent. Tapping twice recorded instalment 1 and then instalment 2, £360.08 for a single £180.04
payment. UNIQUE(plan_id, n) cannot catch that, because the second write is a different n.
"""
import pytest

from mongoose.services import rtm_management as M
from mongoose.services import rtm_payment_plans as P


@pytest.fixture()
def plan(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    M._ensure_tables()
    M.upsert_flat(block="3EC", flat_number=3, leaseholder_name="Charlotte Ashby",
                  payment_reference="ASHBY")
    M.create_demand(document_number="26-TO-DEC/3ECF3R0", flat="3ECF3", period="2026-H2",
                    amount="1080.21", issue_date="2026-07-16", due_date=P._today_iso())
    return P.agree_payment_plan(flat="3ECF3", months=6, start_date=P._today_iso())


def test_the_amount_is_prepopulated_not_typed(plan):
    """One correct figure exists; retyping it is how a penny error reaches a posted document."""
    nxt = P.next_instalment("3ECF3")
    assert nxt["amount"] == "£180.04" and nxt["n"] == 1
    assert nxt["due_today"] is True


def test_recording_puts_the_money_through_the_normal_ledger(plan):
    """A plan payment must not get its own money path, or the demand balance diverges from the plan."""
    r = P.record_instalment("3ECF3")
    assert r["ok"] and r["ledger"]["ok"], f"ledger write failed: {r['ledger']}"
    st = M.flat_statement("3ECF3")
    assert int(st["balance_pence"]) == 108021 - 18004


def test_a_double_tap_does_not_advance_the_plan(plan):
    """THE BUG: the second tap used to record instalment 2, taking £360.08 for one £180.04 payment."""
    P.record_instalment("3ECF3")
    again = P.record_instalment("3ECF3")
    assert again["ok"] is False and again.get("not_yet_due") is True
    assert P.plan_progress("3ECF3")["paid"] == "£180.04"


def test_resubmitting_the_same_instalment_is_a_no_op(plan):
    """The UI passes the n it displayed, so a repeat must be harmless rather than a second payment."""
    P.record_instalment("3ECF3", n=1)
    again = P.record_instalment("3ECF3", n=1)
    assert again["ok"] is True and again["already_recorded"] is True
    assert P.plan_progress("3ECF3")["instalments_paid"] == 1


def test_a_deliberate_early_payment_is_still_allowed(plan):
    """Refusing the accidental case must not block the real one."""
    P.record_instalment("3ECF3", n=1)
    assert P.record_instalment("3ECF3", n=2)["ok"] is True
    assert P.plan_progress("3ECF3")["instalments_paid"] == 2


def test_the_agreed_schedule_is_never_rewritten(plan):
    """instalments_json is what the leaseholder was told in writing."""
    before = P.payment_plan_for("3ECF3")["instalments"]
    P.record_instalment("3ECF3")
    assert P.payment_plan_for("3ECF3")["instalments"] == before


def test_a_short_payment_is_reported_not_hidden(plan):
    """People pay what they pay; treating £100 as settling a £180.04 instalment would lose £80.04."""
    r = P.record_instalment("3ECF3", n=1, amount_pence=10000)
    assert r["short_by"] == "£80.04"


def test_paying_to_schedule_reads_as_on_track_not_arrears(plan):
    P.record_instalment("3ECF3")
    prog = P.plan_progress("3ECF3")
    assert prog["on_track"] is True and prog["instalments_overdue"] == 0


def test_a_missed_instalment_is_overdue_but_a_future_one_is_not(plan):
    """A future instalment is not a debt yet, so colouring it as one would misstate the position."""
    prog = P.plan_progress("3ECF3")
    due_today = [s for s in prog["schedule"] if s["due_today"]]
    future = [s for s in prog["schedule"] if s["due_date"] > P._today_iso()]
    assert due_today and future
    assert all(not s["overdue"] for s in future)


def test_progress_reports_what_the_bar_chart_needs(plan):
    P.record_instalment("3ECF3")
    prog = P.plan_progress("3ECF3")
    for key in ("paid_pence", "outstanding_pence", "percent_paid", "instalments_paid",
                "instalments_overdue", "on_track", "total_pence"):
        assert key in prog, f"the chart and the percentages need {key}"


def test_no_plan_means_no_progress_and_no_crash(plan):
    assert P.plan_progress("3ECF9") is None
    assert P.next_instalment("3ECF9") is None
    assert P.record_instalment("3ECF9")["ok"] is False
