"""Guards that keep tests away from real data.

A test fixture that reached the live database could overwrite real records, and a malformed demand
period with a later due date could be mistaken for the current billing cycle. These tests lock in the
two structural defences:

  1. db.connect() refuses to open the live brain tree (~/.mongoose) from a test process.
  2. list_periods() never selects a malformed period label as "current".
"""
import pytest


def test_connect_refuses_live_brain_tree_under_test(tmp_path, monkeypatch):
    import mongoose.db as db
    protected = (tmp_path / "dot-mongoose").resolve()
    protected.mkdir()
    monkeypatch.setattr(db, "_PROTECTED_BRAIN_ROOT", protected)
    monkeypatch.delenv("MONGOOSE_ALLOW_LIVE_TEST_WRITES", raising=False)
    # We are under pytest, so the guard is armed. Opening any path inside the protected tree is refused.
    with pytest.raises(RuntimeError, match="REFUSED"):
        with db.connect(protected / "data" / "mongoose.sqlite3"):
            pass


def test_connect_allows_isolated_tmp_db_under_test(tmp_path):
    import mongoose.db as db
    # A normal isolated tmp path (what the autouse fixture uses) is NOT under ~/.mongoose, so it connects.
    p = tmp_path / "ok.sqlite3"
    with db.connect(p) as con:
        assert con.execute("SELECT 1").fetchone()[0] == 1


def test_connect_live_allowed_with_explicit_override(tmp_path, monkeypatch):
    import mongoose.db as db
    protected = (tmp_path / "dot-mongoose2").resolve()
    protected.mkdir()
    monkeypatch.setattr(db, "_PROTECTED_BRAIN_ROOT", protected)
    monkeypatch.setenv("MONGOOSE_ALLOW_LIVE_TEST_WRITES", "1")
    # The explicit escape hatch lets a deliberate live operation through.
    with db.connect(protected / "brain.sqlite3") as con:
        assert con.execute("SELECT 1").fetchone()[0] == 1


def test_list_periods_never_picks_malformed_current(tmp_path):
    from mongoose.services import rtm_management as M
    M.seed_flats()
    M.upsert_flat(block="3EC", flat_number=95, leaseholder_name="Iris", payment_reference="IRISREF")
    # The real cycle...
    M.create_demand(document_number="26/3ECF95H2", flat="3ECF95", period="2026-H2",
                    amount="100.00", issue_date="2026-07-16", due_date="2026-07-30")
    # ...and a stray malformed period with a LATER due date (would win a naive newest-by-due-date pick).
    M.create_demand(document_number="26/3ECF95X", flat="3ECF95", period="p",
                    amount="100.00", issue_date="2026-07-16", due_date="2026-08-13")
    lp = M.list_periods()
    assert lp["current"] == "2026-H2"                       # NOT "p", despite "p" being due later
    assert "p" in [x["period"] for x in lp["periods"]]      # still listed, just never "current"
