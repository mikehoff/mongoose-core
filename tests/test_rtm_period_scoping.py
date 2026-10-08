"""Two live charging periods must stay distinguishable everywhere.

Until now every RTM view showed all demands in one undifferentiated list and blended every period's
outstanding into a single "arrears" figure. Once a second cycle is issued that overstates real debt
(freshly-invoiced charges are not arrears) and interleaves two cycles in one table. These tests pin
the period machinery that fixes it.
"""
import pytest

from mongoose.services import rtm_management as M


def _seed_two_periods(con):
    """A flat with one demand in each of two periods: the earlier part-paid, the later untouched."""
    now = "2026-07-01T00:00:00Z"
    con.execute("INSERT INTO rtm_mgmt_flats(flat_id, block, flat_number, leaseholder_name, "
                "payment_reference, created_at, updated_at) VALUES('1ECF1','1EC',1,'Test Person','TESTREF',?,?)",
                (now, now))
    for doc, period, due in (("26-TO-DEC/1ECF1R0", "2026-H2", "2026-07-30"),
                             ("27-TO-JUN/1ECF1R0", "2027-H1", "2027-01-30")):
        con.execute("INSERT INTO rtm_mgmt_demands(document_number, flat_id, period, issue_date, due_date, "
                    "amount_pence, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (doc, "1ECF1", period, "2026-07-16", due, 100000, "Issued", now, now))
    # £400 paid against the OLDER period only. An allocation must reference a real bank credit
    # (the ledger enforces that with a foreign key), so seed the credit it came from too.
    con.execute("INSERT INTO rtm_mgmt_bank_transactions(id, bank_date, description, reference, "
                "amount_pence, direction, match_status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("BT-TEST-1", "2026-07-20", "TESTREF payment", "TESTREF", 40000, "in", "matched", now, now))
    con.execute("INSERT INTO rtm_mgmt_allocations(id, bank_transaction_id, document_number, flat_id, "
                "amount_pence, created_at) VALUES(?,?,?,?,?,?)",
                ("AL-TEST-1", "BT-TEST-1", "26-TO-DEC/1ECF1R0", "1ECF1", 40000, now))
    con.commit()


@pytest.fixture()
def two_periods(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    M._ensure_tables()
    from mongoose.db import connect
    with connect() as con:
        _seed_two_periods(con)
    return True


def test_periods_are_listed_newest_first_with_the_current_one_named(two_periods):
    p = M.list_periods()
    assert [x["period"] for x in p["periods"]] == ["2027-H1", "2026-H2"], "newest (latest due) first"
    assert p["current"] == "2027-H1", "current = the cycle with the most recent due date"
    assert p["count"] == 2


def test_each_period_reports_its_own_outstanding_not_a_blend(two_periods):
    by = {x["period"]: x for x in M.list_periods()["periods"]}
    # older period: charged 1000, paid 400 -> 600 genuinely in arrears
    assert by["2026-H2"]["outstanding_pence"] == 60000
    # newer period: charged 1000, paid nothing -> not arrears, just issued
    assert by["2027-H1"]["outstanding_pence"] == 100000
    # the blended figure (1600) must never be presented as "arrears"
    assert by["2026-H2"]["outstanding_pence"] + by["2027-H1"]["outstanding_pence"] == 160000


def test_demands_can_be_scoped_to_one_period(two_periods):
    assert len(M.list_demands(period="2026-H2")) == 1
    assert len(M.list_demands(period="2027-H1")) == 1
    assert len(M.list_demands()) == 2, "unfiltered still returns every period"
    only = M.list_demands(period="2027-H1")[0]
    assert only["document_number"] == "27-TO-JUN/1ECF1R0"


def test_period_filter_is_exact_not_a_prefix_match(two_periods):
    """A sloppy LIKE would let '2026' pull in 2026-H2 and mis-scope a director's view."""
    assert M.list_demands(period="2026") == []
    assert M.list_demands(period="") != [], "empty means 'all periods', not 'no periods'"


def test_cancelled_and_superseded_demands_do_not_inflate_a_period(two_periods):
    from mongoose.db import connect
    with connect() as con:
        now = "2026-07-01T00:00:00Z"
        con.execute("INSERT INTO rtm_mgmt_demands(document_number, flat_id, period, issue_date, due_date, "
                    "amount_pence, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("27-TO-JUN/1ECF1R1", "1ECF1", "2027-H1", "2026-07-16", "2027-01-30", 999900,
                     "Superseded", now, now))
        con.commit()
    by = {x["period"]: x for x in M.list_periods()["periods"]}
    assert by["2027-H1"]["outstanding_pence"] == 100000, "a superseded demand must not count as owed"


def test_dashboard_splits_this_period_from_true_arrears(two_periods):
    """A single blended "arrears" figure overstates real debt the moment a new cycle is issued.

    With 2026-H2 part-paid (600 owed) and 2027-H1 freshly issued (1000 owed), a director chasing
    debt must be able to see that only 600 is genuinely overdue , not chase 1600.
    """
    from mongoose.services.rtm_management import fund_position
    d = fund_position()
    assert d["current_period"] == "2027-H1"
    assert d["this_period_outstanding"] == "£1,000.00"
    assert d["brought_forward_outstanding"] == "£600.00", "brought forward is the genuine arrears"
    assert d["arrears_outstanding"] == "£1,600.00", "the blended total stays available as 'owed in total'"
    assert d["brought_forward_is_true_arrears"] is True


def test_portal_lists_each_period_as_its_own_open_demand(two_periods):
    """A flat owing two cycles must show TWO open demands, each with its own document number,
    rather than one merged blob the leaseholder cannot reconcile."""
    from mongoose.services import rtm_management as M
    open_docs = [d["document_number"] for d in M.list_demands(flat="1ECF1")]
    assert len(open_docs) == 2 and len(set(open_docs)) == 2


def test_arrears_excludes_a_demand_that_is_not_due_yet(two_periods):
    """Newly-issued charges are not arrears: arrears is driven by due date, not by being unpaid."""
    from mongoose.services import rtm_management as M
    a = M.arrears(today="2026-09-15")          # after H2's due date, before 2027-H1's
    docs = [i["document_number"] for i in a["items"]]
    assert all("27-TO-JUN" not in d for d in docs), "a future-dated demand must never read as arrears"
