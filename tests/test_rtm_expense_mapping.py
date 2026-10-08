"""Expenditure -> budget mapping: keyword + learned rules, Paid-payable creation, and the Sundries cap."""
import csv


def _metro(tmp_path, rows):
    p = tmp_path / "metro.csv"
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Details", "Transaction Type", "In", "Out", "Balance"])
        for r in rows:
            w.writerow(r)
    return p


def _exp_ids(M):
    from mongoose.db import connect
    with connect() as con:
        return {dict(r)["description"]: dict(r)["id"] for r in con.execute(
            "SELECT id, description FROM rtm_mgmt_bank_transactions WHERE match_status='expenditure'").fetchall()}


def test_expense_mapping_sundries_cap_and_learning(tmp_path):
    from mongoose.services import rtm_management as M
    M.seed_flats()
    M.set_budget_line(year="2027", schedule="sch1", budget_line="Cleaning", amount="3640.00")
    M.set_budget_line(year="2027", schedule="sch1", budget_line="Sundries", amount="0.00",
                      warn_threshold_pence=45000)

    # Two expenditure lines via the reconciler.
    p = _metro(tmp_path, [
        ["2026-08-20", "Jane Cleaner DEEP CLEAN TILES", "Outward Faster Payment", "", "288.00", "x"],
        ["2026-08-20", "Card Purchase ENTERPRISE RENT A CAR", "Card Transaction", "", "60.00", "x"],
    ])
    M.reconcile_metro_csv(p, apply=True, store_document=False)
    ids = _exp_ids(M)
    assert len(ids) == 2

    # Suggestion: cleaning by keyword; enterprise defaults to Sundries.
    assert M.suggest_budget_line("Jane Cleaner DEEP CLEAN TILES")["budget_line"] == "Cleaning"
    assert M.suggest_budget_line("Card Purchase ENTERPRISE RENT A CAR")["budget_line"] == "Sundries"

    clean_id = ids["Jane Cleaner DEEP CLEAN TILES"]
    ent_id = ids["Card Purchase ENTERPRISE RENT A CAR"]
    M.map_expenditure(clean_id, "sch1", "Cleaning")
    M.map_expenditure(ent_id, "sch1", "Sundries")   # learns 'enterprise' -> Sundries

    ba = {l["budget_line"]: l for l in M.budget_actuals(year="2027")["lines"]}
    assert ba["Cleaning"]["spent_pence"] == 28800
    assert ba["Sundries"]["spent_pence"] == 6000
    assert ba["Sundries"]["warn"] is False          # £60 < £450

    # A Paid payable + supplier_payment ledger entry were created for the mapping.
    from mongoose.db import connect
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM rtm_mgmt_payables WHERE status='Paid'").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM rtm_mgmt_ledger_entries WHERE kind='supplier_payment'").fetchone()[0] == 2

    # Learned: a later Enterprise line auto-suggests Sundries.
    assert M.suggest_budget_line("ENTERPRISE RENT A CAR hire again")["source"] == "learned"
    assert M.suggest_budget_line("ENTERPRISE RENT A CAR hire again")["budget_line"] == "Sundries"

    # Re-mapping the same line is a no-op.
    again = M.map_expenditure(ent_id, "sch1", "Sundries")
    assert again.get("already_mapped") is True

    # Push Sundries over the £450 cap -> warn True.
    p2 = _metro(tmp_path, [["2026-08-21", "Card Purchase ENTERPRISE big hire", "Card Transaction", "", "500.00", "x"]])
    M.reconcile_metro_csv(p2, apply=True, store_document=False)
    big_id = {d: i for d, i in _exp_ids(M).items() if "big hire" in d}["Card Purchase ENTERPRISE big hire"]
    M.map_expenditure(big_id, "sch1", "Sundries")
    ba2 = {l["budget_line"]: l for l in M.budget_actuals(year="2027")["lines"]}
    assert ba2["Sundries"]["spent_pence"] == 56000
    assert ba2["Sundries"]["warn"] is True
