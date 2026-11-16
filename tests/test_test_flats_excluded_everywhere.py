"""Test/fixture flats must never appear in a count, a total, or a director's list.

Three fixture flats (LONG 1ECF0, MCDONALD 3ECF0, TOBIN 1ECF10) exist so sign-up, invites and the
portal can be exercised without touching a real leaseholder. They are marked excluded=1. list_flats()
honoured that, but anything counting DEMANDS did not (demands carry no flag), so the building read as
21 flats / 21 demands instead of 18 and the owner was left wondering which number was true.
"""
import pytest

from mongoose.services import rtm_management as M


@pytest.fixture()
def mixed_flats(tmp_path, monkeypatch):
    """Two real flats and one test flat, each with a demand."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    M._ensure_tables()
    from mongoose.db import connect
    now = "2026-07-01T00:00:00Z"
    with connect() as con:
        for fid, num, excluded in (("1ECF1", 1, 0), ("1ECF2", 2, 0), ("3ECF0", 0, 1)):
            con.execute("INSERT INTO rtm_mgmt_flats(flat_id, block, flat_number, leaseholder_name, "
                        "payment_reference, excluded, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (fid, fid[:3], num, f"Person {fid}", fid, excluded, now, now))
            con.execute("INSERT INTO rtm_mgmt_demands(document_number, flat_id, period, issue_date, "
                        "due_date, amount_pence, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"26-TO-DEC/{fid}R0", fid, "2026-H2", "2026-07-16", "2026-07-30",
                         100000, "Issued", now, now))
        con.commit()
    return True


def test_flat_list_and_count_exclude_test_flats(mixed_flats):
    flats = M.list_flats()
    assert len(flats) == 2, "the test flat must not be counted as part of the building"
    assert "3ECF0" not in [f["flat_id"] for f in flats]


def test_demand_list_excludes_test_flat_demands(mixed_flats):
    """The bug: demands carry no excluded flag, so the list showed the test flat's demand too."""
    docs = [d["document_number"] for d in M.list_demands(limit=100)]
    assert len(docs) == 2, f"expected 2 real demands, got {docs}"
    assert not any("3ECF0" in d for d in docs)


def test_a_test_flat_is_still_reachable_when_asked_for_explicitly(mixed_flats):
    """Hiding them from lists must not make them unmanageable."""
    assert len(M.list_demands(flat="3ECF0")) == 1


def test_period_counts_exclude_test_flats(mixed_flats):
    p = {x["period"]: x for x in M.list_periods()["periods"]}
    assert p["2026-H2"]["demands"] == 2, "a period must count only real flats"
    assert p["2026-H2"]["outstanding_pence"] == 200000, "and only their money"


def test_money_totals_exclude_test_flats(mixed_flats):
    f = M.fund_position()
    assert f["demands_raised"] == "£2,000.00", "a test flat must not inflate demands raised"


def test_arrears_never_chases_a_test_flat(mixed_flats):
    a = M.arrears(today="2026-09-15")
    assert all("3ECF0" not in i["document_number"] for i in a["items"]), \
        "chasing a fixture leaseholder for money would be a real-world error"


def test_excluded_ids_helper_reports_them(mixed_flats):
    assert M.excluded_flat_ids() == {"3ECF0"}
