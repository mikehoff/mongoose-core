"""A payment out recorded by hand must be counted ONCE when its bank statement is imported.

Found on 2026-09-23: a £118.50 Amazon card charge and an £11.94 AliExpress charge were entered manually
(receipts in hand, September statement not yet exported). The importer's duplicate guard only compared
against earlier metro_csv rows, so the real statement line would have been inserted as a second, unmapped
expenditure of the same money; mapping it would have booked the spend twice against the budget. The
manual row was also invisible on the reconciliation page, which listed metro_csv lines only.

The fix: the importer ADOPTS a matching manual debit (same account, same amount, statement date within
the card posting-lag window, each manual row used once, closest date wins) instead of inserting a copy.
"""
import json

import pytest

from mongoose.services import rtm_management as M

HEADER = "Date,Details,Transaction Type,In,Out,Balance\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    M._ensure_tables()

    def csv(*lines):
        p = tmp_path / f"Transactions_Export_{len(list(tmp_path.glob('*.csv')))}.csv"
        p.write_text(HEADER + "".join(line + "\n" for line in lines))
        return str(p)
    return csv


def _debits(pence):
    from mongoose.db import connect
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM rtm_mgmt_bank_transactions WHERE direction='debit' AND amount_pence=?", (pence,))]


def _payables():
    from mongoose.db import connect
    with connect() as con:
        return con.execute("SELECT COUNT(*) FROM rtm_mgmt_payables").fetchone()[0]


def _amazon(**kw):
    args = dict(amount="118.50", bank_date="2026-09-23", transaction_date="2026-09-21", supplier="Amazon",
                description="Amazon: signs and cameras", reference="Metro card 9864")
    args.update(kw)
    r = M.record_manual_expenditure(**args)
    assert r["ok"], r
    return r


def test_statement_line_adopts_the_manual_debit(env):
    man = _amazon()
    r = M.reconcile_metro_csv(env("23/09/2026,Card Purchase AMAZON.CO.UK*AB12CD,Card Transaction,,118.50,900.00"),
                              apply=True, store_document=False)
    assert r["applied"]["adopted_manual_debits"] == 1
    assert r["applied"]["recorded_expenditure"] == 0
    rows = _debits(11850)
    assert len(rows) == 1, "the same £118.50 must be on the register once, not twice"
    row = rows[0]
    assert row["id"] == man["bank_txn_id"]
    assert row["source"] == "metro_csv"
    assert row["description"] == "Card Purchase AMAZON.CO.UK*AB12CD"
    assert row["match_status"] == "expenditure_mapped"
    assert row["matched_document_number"] == man["payable_id"]
    raw = json.loads(row["raw_json"])
    assert raw["out"] == "118.50" and raw["_manual_entry"]["bank_date"] == "2026-09-23"


def test_reimport_after_adoption_adds_nothing(env):
    _amazon()
    path = env("23/09/2026,Card Purchase AMAZON,Card Transaction,,118.50,900.00")
    M.reconcile_metro_csv(path, apply=True, store_document=False)
    r = M.reconcile_metro_csv(path, apply=True, store_document=False)
    assert r["applied"]["skipped_already_recorded"] == 1
    assert len(_debits(11850)) == 1


def test_no_second_payable_so_budget_counts_it_once(env):
    _amazon()
    before = _payables()
    M.reconcile_metro_csv(env("23/09/2026,Card Purchase AMAZON,Card Transaction,,118.50,900.00"),
                          apply=True, store_document=False)
    assert _payables() == before
    view = M.reconciliation_view("2026")
    lines = [e for e in view["expenditure"] if e["amount_pence"] == 11850]
    assert len(lines) == 1 and lines[0]["mapped"] and not lines[0]["awaiting_bank_line"]


def test_dry_run_flags_the_pairing_and_writes_nothing(env):
    man = _amazon()
    r = M.reconcile_metro_csv(env("23/09/2026,Card Purchase AMAZON,Card Transaction,,118.50,900.00"))
    assert r["dry_run"]
    assert r["categories"]["expenditure"][0]["confirms_manual_debit"] == man["bank_txn_id"]
    assert _debits(11850)[0]["source"] == "manual_entry"


def test_manual_entry_on_the_card_date_still_matches_the_later_posting(env):
    """The owner may type the card date rather than the date the bank shows."""
    _amazon(bank_date="2026-09-21", transaction_date=None)
    M.reconcile_metro_csv(env("25/09/2026,Card Purchase AMAZON,Card Transaction,,118.50,900.00"),
                          apply=True, store_document=False)
    assert len(_debits(11850)) == 1


def test_outside_the_window_is_a_different_payment(env):
    _amazon(bank_date="2026-08-20", transaction_date=None)
    M.reconcile_metro_csv(env("23/09/2026,Card Purchase AMAZON,Card Transaction,,118.50,900.00"),
                          apply=True, store_document=False)
    rows = _debits(11850)
    assert sorted(r["source"] for r in rows) == ["manual_entry", "metro_csv"]


def test_different_amount_is_never_adopted(env):
    _amazon()
    M.reconcile_metro_csv(env("23/09/2026,Card Purchase AMAZON,Card Transaction,,118.51,900.00"),
                          apply=True, store_document=False)
    assert _debits(11850)[0]["source"] == "manual_entry"
    assert len(_debits(11851)) == 1


def test_each_manual_debit_is_used_once_closest_date_wins(env):
    far = _amazon(bank_date="2026-09-15", transaction_date=None)
    near = _amazon(bank_date="2026-09-22", transaction_date=None)
    M.reconcile_metro_csv(env("23/09/2026,Card Purchase AMAZON,Card Transaction,,118.50,900.00"),
                          apply=True, store_document=False)
    by_id = {r["id"]: r for r in _debits(11850)}
    assert len(by_id) == 2
    assert by_id[near["bank_txn_id"]]["source"] == "metro_csv"
    assert by_id[far["bank_txn_id"]]["source"] == "manual_entry"


def test_two_identical_statement_lines_adopt_two_manual_entries(env):
    a = _amazon(bank_date="2026-09-22", transaction_date=None)
    b = _amazon(bank_date="2026-09-23", transaction_date=None)
    M.reconcile_metro_csv(env("23/09/2026,Card Purchase AMAZON,Card Transaction,,118.50,900.00",
                              "23/09/2026,Card Purchase AMAZON,Card Transaction,,118.50,781.50"),
                          apply=True, store_document=False)
    rows = _debits(11850)
    assert len(rows) == 2 and all(r["source"] == "metro_csv" for r in rows)
    assert {r["id"] for r in rows} == {a["bank_txn_id"], b["bank_txn_id"]}


def test_manual_debit_is_visible_before_the_statement_arrives(env):
    _amazon()
    lines = [e for e in M.reconciliation_view("2026")["expenditure"] if e["amount_pence"] == 11850]
    assert len(lines) == 1 and lines[0]["awaiting_bank_line"] and lines[0]["mapped"]
    assert lines[0]["budget_line"] == "Sundries"


def test_record_manual_expenditure_refuses_nonsense(env):
    assert not M.record_manual_expenditure(amount="0", bank_date="2026-09-23", supplier="X", description="X")["ok"]
    assert not M.record_manual_expenditure(amount="5", bank_date="2026-09-23", supplier="X", description="X",
                                           account="reserve")["ok"]
