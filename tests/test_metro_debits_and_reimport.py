"""The bank register must hold the WHOLE account, once.

Three defects in the Metro statement import path, each covered below:

1. Every non-transfer debit was dropped ("if debit_s and not credit_s: continue"), so the whole spending
   side of the account could vanish while the import still reported success, because the credit matching
   genuinely worked. A fund position derived from the
   register would have shown income and no spending.
2. raw_json was hardcoded "{}", so no row traced back to its statement line. That turned a recoverable
   import mistake into an unrecoverable one: when the dropped debits surfaced there was nothing to
   re-derive from.
3. No row-level duplicate check, so re-importing a statement added every line again.
   Harmless while the register only evidenced payments; not harmless once the book is derived from it.
"""
import pytest

from mongoose.services import rtm_management as M

HEADER = "Date,Details,Transaction Type,In,Out,Balance\n"
# Two identical reserve sweeps on one day are REAL (one per block), so dedup must count, not de-set.
ROWS = (
    "20/07/2026,ANNA SMITH Cleaning,Outward Faster Payment,,288.00,1000.00\n"
    "20/07/2026,Cristian Nistor invoice chain sign,Outward Faster Payment,,21.33,1021.33\n"
    "20/07/2026,DOCMAIL topup,Outward Faster Payment,,5.00,1026.33\n"
    "18/07/2026,EXAMPLE COURT RTM COMPANY LIMITED null,Transfer,,145.23,1031.33\n"
    "18/07/2026,EXAMPLE COURT RTM COMPANY LIMITED null,Transfer,,145.23,1176.56\n"
    "17/07/2026,SOMEONE UNKNOWN,Inward Faster Payment,500.00,,1321.79\n"
    "01/06/2026,OLD REGIME PAYMENT,Outward Faster Payment,,99.00,821.79\n"
)


@pytest.fixture()
def csv_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    M._ensure_tables()
    p = tmp_path / "Transactions_Export.csv"
    p.write_text(HEADER + ROWS)
    return p


def _rows():
    from mongoose.db import connect
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM rtm_mgmt_bank_transactions")]


def test_expenditure_is_no_longer_discarded(csv_file):
    r = M.reconcile_metro_csv(str(csv_file), apply=False)
    assert r["summary"]["expenditure"] == 3, (
        f"expected the 3 post-acquisition payments out, got {r['summary']}")


def test_no_statement_line_is_silently_lost(csv_file):
    """The failure was silent, so the guard is a conservation check: rows in == rows classified."""
    r = M.reconcile_metro_csv(str(csv_file), apply=False)
    assert sum(len(v) for v in r["categories"].values()) == 7


def test_money_out_is_recorded_as_a_debit(csv_file):
    M.reconcile_metro_csv(str(csv_file), apply=True, store_document=False)
    debits = [r for r in _rows() if r["direction"] == "debit"]
    assert debits, "money leaving the account must not be stored as a credit"
    assert any(r["match_status"] == "expenditure" for r in debits)


def test_an_out_sweep_is_a_debit_not_a_credit(csv_file):
    """A transfer OUT is money leaving; storing it as a credit counted a sweep as income."""
    M.reconcile_metro_csv(str(csv_file), apply=True, store_document=False)
    sweeps = [r for r in _rows() if r["amount_pence"] == 14523]
    assert sweeps and all(r["direction"] == "debit" for r in sweeps)


def test_expenditure_never_settles_a_demand(csv_file):
    """A payment out cannot discharge a leaseholder's charge."""
    M.reconcile_metro_csv(str(csv_file), apply=True, store_document=False)
    for r in _rows():
        if r["match_status"] == "expenditure":
            assert not (r["matched_document_number"] or "")
            assert int(r["allocated_pence"] or 0) == 0


def test_the_statement_line_is_retained(csv_file):
    M.reconcile_metro_csv(str(csv_file), apply=True, store_document=False)
    metro = [r for r in _rows() if r["source"] == "metro_csv"]
    assert metro and all((r["raw_json"] or "{}").strip() not in ("", "{}") for r in metro), \
        "every row must carry its source line, or the import cannot be re-derived or audited"


def test_the_bank_running_balance_survives(csv_file):
    """Balance is the bank's own figure and therefore the independent side of any reconciliation."""
    import json
    M.reconcile_metro_csv(str(csv_file), apply=True, store_document=False)
    raws = [json.loads(r["raw_json"]) for r in _rows() if r["source"] == "metro_csv"]
    assert any("balance" in x for x in raws)


def test_pre_acquisition_spending_stays_out_of_the_books(csv_file):
    M.reconcile_metro_csv(str(csv_file), apply=True, store_document=False)
    old = [r for r in _rows() if r["amount_pence"] == 9900]
    assert old and all(r["match_status"] == "pre_acquisition" for r in old)


def test_reimporting_the_same_statement_changes_nothing(csv_file):
    M.reconcile_metro_csv(str(csv_file), apply=True, store_document=False)
    first = len(_rows())
    again = M.reconcile_metro_csv(str(csv_file), apply=True, store_document=False)
    assert len(_rows()) == first, "a re-import must be a no-op, not a second copy of the account"
    assert again["applied"]["skipped_already_recorded"] == 7


def test_a_genuine_repeated_line_is_still_recorded_twice(csv_file):
    """Counted dedup, not set dedup: two identical sweeps on one day are two real movements."""
    M.reconcile_metro_csv(str(csv_file), apply=True, store_document=False)
    assert len([r for r in _rows() if r["amount_pence"] == 14523]) == 2


def test_owner_can_explain_a_line_and_it_leaves_the_queue(csv_file):
    M.reconcile_metro_csv(str(csv_file), apply=True, store_document=False)
    queue = M.unexplained_bank_lines()
    assert queue, "post-acquisition expenditure should start unexplained"
    target = next(r for r in queue if r["amount_pence"] == 500)
    M.explain_bank_transaction(target["id"], "Docmail postage account topup")
    assert not any(r["id"] == target["id"] for r in M.unexplained_bank_lines())


def test_an_explanation_cannot_alter_what_the_bank_said(csv_file):
    M.reconcile_metro_csv(str(csv_file), apply=True, store_document=False)
    tx = next(r for r in _rows() if r["match_status"] == "expenditure")
    before = {k: tx[k] for k in ("bank_date", "amount_pence", "direction", "match_status")}
    M.explain_bank_transaction(tx["id"], "some explanation")
    after = next(r for r in _rows() if r["id"] == tx["id"])
    assert {k: after[k] for k in before} == before


def test_an_explanation_is_attributed_and_timestamped(csv_file):
    M.reconcile_metro_csv(str(csv_file), apply=True, store_document=False)
    tx = next(r for r in _rows() if r["match_status"] == "expenditure")
    M.explain_bank_transaction(tx["id"], "Docmail topup", by="owner")
    after = next(r for r in _rows() if r["id"] == tx["id"])
    assert after["explained_by"] == "owner" and after["explained_at"]


def test_withdrawing_an_explanation_returns_the_line_to_the_queue(csv_file):
    """A mistaken explanation must not silently clear a line for ever."""
    M.reconcile_metro_csv(str(csv_file), apply=True, store_document=False)
    tx = next(r for r in _rows() if r["match_status"] == "expenditure")
    M.explain_bank_transaction(tx["id"], "wrong")
    M.explain_bank_transaction(tx["id"], "")
    assert any(r["id"] == tx["id"] for r in M.unexplained_bank_lines())


def test_the_notes_columns_exist_on_a_fresh_install(csv_file):
    """A column that lives only in a live-DB ALTER is a column a fresh install does not have."""
    from mongoose.db import connect
    with connect() as con:
        cols = {c[1] for c in con.execute("PRAGMA table_info(rtm_mgmt_bank_transactions)")}
    assert {"notes", "explained_by", "explained_at"} <= cols


def test_classifier_says_unclassified_rather_than_guessing():
    assert M._classify_debit("ANNA SMITH Cleaning") == "communal cleaning"
    assert M._classify_debit("DOCMAIL topup") == "Docmail postage account topup"
    assert M._classify_debit("XYZ9931 QQ") == "unclassified payment out", \
        "a wrong category on a real payment is worse than an honest unknown"
