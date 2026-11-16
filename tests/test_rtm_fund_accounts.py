"""Reserve/service fund accounts: internal-only logging + balance reconciliation, and the recon-view
declutter (internal transfers must never appear as assignable expenditure)."""
import csv


def _csv(tmp_path, name, rows):
    p = tmp_path / name
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Details", "Transaction Type", "In", "Out", "Balance"])
        for r in rows:
            w.writerow(r)
    return p


def test_fund_account_classifies_internal_and_interest_only(tmp_path):
    from mongoose.services import rtm_management as M
    M.seed_flats()
    p = _csv(tmp_path, "reserve.csv", [
        ["2026-07-16", "EXAMPLE COURT RTM COMPANY LIMITED null", "TRANSFER", "145.23", "", "145.23"],
        ["2026-07-20", "EXAMPLE COURT RTM COMPANY LIMITED null", "TRANSFER", "", "46.87", "98.36"],
        ["2026-07-31", "", "Credit Interest", "4.69", "", "103.05"],
    ])
    s = M.reconcile_metro_csv(p, apply=False, account="reserve")["summary"]
    assert s["interest"] == 1 and s["internal_transfer"] == 2
    assert s["expenditure"] == 0 and s["allocate"] == 0 and s["confirms_manual"] == 0


def test_fund_reconciliation_matches_bank_stated_balance(tmp_path):
    from mongoose.services import rtm_management as M
    M.seed_flats()
    p = _csv(tmp_path, "svc.csv", [
        ["2026-07-16", "EXAMPLE COURT RTM COMPANY LIMITED null", "TRANSFER", "100.00", "", "100.00"],
        ["2026-07-17", "EXAMPLE COURT RTM COMPANY LIMITED null", "TRANSFER", "25.00", "", "125.00"],
        ["2026-07-18", "EXAMPLE COURT RTM COMPANY LIMITED null", "TRANSFER", "", "5.00", "120.00"],
        ["2026-07-31", "", "Credit Interest", "1.00", "", "121.00"],
    ])
    M.reconcile_metro_csv(p, apply=True, account="service")
    fr = M.fund_reconciliation()["funds"]["service"]
    assert fr["bank_stated_balance"] == "£121.00"
    assert fr["computed_balance"] == "£121.00"
    assert fr["reconciled"] is True


def test_fund_rows_never_touch_collection(tmp_path):
    from mongoose.services import rtm_management as M
    M.seed_flats()
    before = M.collection_position().get("collected")
    p = _csv(tmp_path, "res.csv", [["2026-07-16", "EXAMPLE COURT RTM COMPANY LIMITED null", "TRANSFER", "5000.00", "", "5000.00"]])
    M.reconcile_metro_csv(p, apply=True, account="reserve")
    assert M.collection_position().get("collected") == before


def test_reconciliation_view_excludes_transfers_from_assignable(tmp_path):
    from mongoose.services import rtm_management as M
    M.seed_flats()
    p = _csv(tmp_path, "cur.csv", [
        ["2026-07-20", "Watt a Guy Limited INV1", "Outward Faster Payment", "", "60.00", "0.00"],
        ["2026-07-16", "EXAMPLE COURT RTM COMPANY LIMITED null", "TRANSFER", "", "145.23", "0.00"],
    ])
    M.reconcile_metro_csv(p, apply=True)  # current account (default)
    descs = [e["description"] for e in M.reconciliation_view()["expenditure"]]
    assert any("Watt a Guy" in d for d in descs)
    assert not any("EXAMPLE COURT" in d for d in descs)
