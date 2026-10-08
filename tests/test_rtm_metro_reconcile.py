"""Safe Metro Bank CSV reconciliation , the double-count-proof, date-gated, transfer-aware layer."""
import csv


def _metro(tmp_path, rows):
    p = tmp_path / "metro.csv"
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Details", "Transaction Type", "In", "Out", "Balance"])
        for r in rows:
            w.writerow(r)
    return p


def test_import_reads_metro_in_out_columns(tmp_path):
    # The #59 fix: Metro's own column names In/Out must be recognised (previously imported 0).
    from mongoose.services import rtm_management as M
    M.seed_flats()
    M.upsert_flat(block="3EC", flat_number=95, leaseholder_name="Iris", payment_reference="IRISREF")
    M.create_demand(document_number="26/3ECF95", flat="3ECF95", period="p", amount="100.00", issue_date="2026-07-16")
    p = _metro(tmp_path, [["2026-07-20", "IRISREF pay", "Inward Payment", "100.00", "", "100.00"]])
    out = M.import_metro_csv(p)
    assert out["imported"] == 1 and out["exact"] == 1


def test_reconcile_confirms_manual_without_double_count(tmp_path):
    from mongoose.services import rtm_management as M
    M.seed_flats()
    M.upsert_flat(block="3EC", flat_number=91, leaseholder_name="Cara", payment_reference="CARAREF")
    M.upsert_flat(block="3EC", flat_number=92, leaseholder_name="Owen", payment_reference="OWENREF")
    M.create_demand(document_number="26/3ECF91", flat="3ECF91", period="p", amount="1075.16", issue_date="2026-07-16")
    M.create_demand(document_number="26/3ECF92", flat="3ECF92", period="p", amount="1075.16", issue_date="2026-07-16")
    # Manually pay the first (creates a manual_entry allocation -> Paid).
    M.record_payment(document_number="26/3ECF91", amount="1075.16")
    statuses = {d["document_number"]: d["status"] for d in M.list_demands()}
    assert statuses["26/3ECF91"] == "Paid"
    paid_alloc_before = M.allocated_to_demand("26/3ECF91")

    p = _metro(tmp_path, [
        ["2026-07-20", "CARAREF service charge", "Inward Payment", "1075.16", "", "x"],   # confirms_manual
        ["2026-07-20", "OWENREF service charge", "Inward Payment", "1075.16", "", "x"],   # allocate (open)
        ["2026-07-16", "EXAMPLE COURT RTM COMPANY LIMITED null", "TRANSFER", "145.23", "", "x"],  # internal
        ["2026-06-10", "CARAREF old regime", "Inward Payment", "500.00", "", "x"],        # pre_acquisition
        ["2026-07-20", "MYSTERY PAYER", "Inward Payment", "77.00", "", "x"],              # unmatched
        ["2026-07-20", "OWENREF refund", "Outward Faster Payment", "", "10.00", "x"],     # debit -> expenditure
    ])

    r = M.reconcile_metro_csv(p, apply=False)
    s = r["summary"]
    assert r["dry_run"] is True
    # expenditure:1 is the £10 refund out. It used to be DROPPED here, and this assertion recorded that
    # as correct. It was not: the register is the only record of the bank, so a discarded debit meant the
    # money had left the account with nothing to show it.
    assert s == {"internal_transfer": 1, "pre_acquisition": 1, "allocate": 1,
                 "confirms_manual": 1, "attribute": 0, "unmatched": 1, "expenditure": 1, "interest": 0}

    ra = M.reconcile_metro_csv(p, apply=True)
    assert ra["applied"]["confirmed"] == 1
    assert ra["applied"]["allocated"] == 1
    # The already-Paid demand must NOT be re-allocated (no double count).
    assert M.allocated_to_demand("26/3ECF91") == paid_alloc_before
    # The open demand is now paid from the bank credit.
    assert {d["document_number"]: d["status"] for d in M.list_demands()}["26/3ECF92"] == "Paid"
    # The confirming bank credit was recorded as EVIDENCE against the paid demand.
    ev = M.list_evidence(target_type="demand", target_id="26/3ECF91")
    assert any(e["document_kind"] == "bank_confirmation" for e in ev)


def test_reconcile_fund_position_only_moves_by_real_new_allocation(tmp_path):
    from mongoose.services import rtm_management as M
    M.seed_flats()
    M.upsert_flat(block="3EC", flat_number=93, leaseholder_name="Paul", payment_reference="PAULREF")
    M.create_demand(document_number="26/3ECF93", flat="3ECF93", period="p", amount="200.00", issue_date="2026-07-16")
    M.record_payment(document_number="26/3ECF93", amount="200.00")  # manual paid
    before = M.fund_position()["payments_allocated"]
    # Only a confirms_manual + an internal transfer + a pre-acquisition credit: NONE should move money.
    p = _metro(tmp_path, [
        ["2026-07-20", "PAULREF pay", "Inward Payment", "200.00", "", "x"],       # confirms_manual
        ["2026-07-16", "EXAMPLE COURT RTM COMPANY LIMITED", "TRANSFER", "50.00", "", "x"],
        ["2026-06-01", "PAULREF june", "Inward Payment", "200.00", "", "x"],       # pre_acquisition
    ])
    M.reconcile_metro_csv(p, apply=True)
    assert M.fund_position()["payments_allocated"] == before  # unchanged: no second allocation


def test_subaccount_sweeps_tagged_and_audit_reconciles(tmp_path, monkeypatch):
    from mongoose.services import rtm_management as M
    import mongoose.services.rtm_trust as TRUST
    RES, SVC, TOT = 14523, 92993, 107516   # deed-exact split for a 6.1667% flat
    # Deterministic split (a fresh test DB has no apportionment seeded).
    monkeypatch.setattr(TRUST, "subaccount_split",
                        lambda flat_id, **k: {"reserve_pence": RES, "service_pence": SVC, "total_pence": TOT})
    M.seed_flats()
    M.upsert_flat(block="1EC", flat_number=3, leaseholder_name="Nina", payment_reference="MARTAREF")
    amt, res_s, svc_s = f"{TOT/100:.2f}", f"{RES/100:.2f}", f"{SVC/100:.2f}"
    M.create_demand(document_number="26/1ECF3X", flat="1ECF3", period="p", amount=amt, issue_date="2026-07-16")
    M.record_payment(document_number="26/1ECF3X", amount=amt)   # manual paid
    p = _metro(tmp_path, [
        ["2026-07-20", "MARTAREF service charge", "Inward Payment", amt, "", "x"],                 # confirms_manual
        ["2026-07-20", "EXAMPLE COURT RTM COMPANY LIMITED null", "TRANSFER", "", res_s, "x"],       # reserve sweep OUT
        ["2026-07-20", "EXAMPLE COURT RTM COMPANY LIMITED null", "TRANSFER", "", svc_s, "x"],       # service sweep OUT
    ])
    dry = M.reconcile_metro_csv(p, apply=False)
    funds = {t.get("fund") for t in dry["categories"]["internal_transfer"]}
    assert "reserve" in funds and "service" in funds  # sweeps (debits) captured + fund-tagged
    M.reconcile_metro_csv(p, apply=True)
    a = M.subaccount_audit()
    assert a["reserve_fund"]["bank_moved_pence"] == RES
    assert a["service_fund"]["bank_moved_pence"] == SVC


def test_pre_acquisition_valid_payment_settles_effective_acquisition(tmp_path):
    from mongoose.services import rtm_management as M
    from mongoose.db import connect
    import sqlite3
    M.seed_flats()
    M.upsert_flat(block="3EC", flat_number=96, leaseholder_name="Zed", payment_reference="ZEDREF")
    M.create_demand(document_number="26/3ECF96", flat="3ECF96", period="p", amount="500.00", issue_date="2026-07-16")
    # a real, UNPAID leaseholder payment received 10 July (before the 16 Jul acquisition)
    p = _metro(tmp_path, [["2026-07-10", "ZEDREF service charge", "Inward Payment", "500.00", "", "x"]])
    dry = M.reconcile_metro_csv(p, apply=False)
    assert dry["summary"]["allocate"] == 1, "a valid pre-acquisition payment is a credit, not ignored"
    assert dry["categories"]["allocate"][0]["effective_date"] == "2026-07-16"  # effective acquisition
    M.reconcile_metro_csv(p, apply=True)
    assert {d["document_number"]: d["status"] for d in M.list_demands()}["26/3ECF96"] == "Paid"
    with connect() as c:
        c.row_factory = sqlite3.Row
        led = c.execute("SELECT entry_date FROM rtm_mgmt_ledger_entries WHERE document_number='26/3ECF96' "
                        "AND kind='payment'").fetchone()
    assert led["entry_date"] == "2026-07-16", "settled effective the acquisition date, not 10 July"


def test_manual_pre_acquisition_paid_is_resettled_effective_acquisition(tmp_path):
    # The gap M caught: a demand marked Paid manually with a real PRE-acquisition receipt date must be
    # re-dated to the acquisition date (valid credit, settled effective acquisition), balances untouched.
    from mongoose.services import rtm_management as M
    from mongoose.db import connect
    import sqlite3
    M.seed_flats()
    M.upsert_flat(block="1EC", flat_number=4, leaseholder_name="Nadia", payment_reference="NADIAREF")
    M.create_demand(document_number="26/1ECF4Z", flat="1ECF4", period="p", amount="300.00", issue_date="2026-07-16")
    M.record_payment(document_number="26/1ECF4Z", amount="300.00", received_date="2026-06-20")  # real early receipt
    before = M.fund_position()["payments_allocated"]
    res = M.settle_effective_acquisition("2026-07-16")
    assert res["corrected"] >= 1
    with connect() as c:
        c.row_factory = sqlite3.Row
        led = c.execute("SELECT entry_date, immutable_note FROM rtm_mgmt_ledger_entries "
                        "WHERE document_number='26/1ECF4Z' AND kind='payment'").fetchone()
    assert led["entry_date"] == "2026-07-16"
    assert "received 2026-06-20" in led["immutable_note"]        # true receipt kept in the audit note
    assert M.fund_position()["payments_allocated"] == before     # balances untouched
    assert M.settle_effective_acquisition("2026-07-16")["corrected"] == 0  # idempotent


def test_reconcile_confirms_instalment_and_on_account_and_attributes_new(tmp_path):
    """The broadened matcher: a bank credit that matches a flat by reference must CONFIRM a recorded
    instalment, a recorded on-account payment, or a set of manual entries summing to it (never a second
    allocation); and a genuinely-new reference-matched credit must be ATTRIBUTED (allocated + on account)."""
    from mongoose.services import rtm_management as M
    M.seed_flats()
    M.upsert_flat(block="3EC", flat_number=41, leaseholder_name="Ashby", payment_reference="ASHBY")
    M.upsert_flat(block="3EC", flat_number=46, leaseholder_name="De Vermeulen", payment_reference="DEVERMEULEN")
    M.upsert_flat(block="3EC", flat_number=48, leaseholder_name="Hale", payment_reference="HALE")
    M.upsert_flat(block="3EC", flat_number=49, leaseholder_name="Newpayer", payment_reference="NEWREF")
    M.create_demand(document_number="26/3ECF41", flat="3ECF41", period="p", amount="654.23", issue_date="2026-07-16")
    M.create_demand(document_number="26/3ECF49", flat="3ECF49", period="p", amount="100.00", issue_date="2026-07-16")

    # Ashby: a part-payment (instalment) recorded by hand against her open demand.
    M.record_payment(document_number="26/3ECF41", amount="180.04", received_date="2026-08-20")
    # De Vermeulen: an on-account payment recorded by hand (no open demand).
    M.record_on_account_credit(flat="3ECF46", amount="180.04", received_date="2026-08-20")
    # Hale: two £179.20 instalments recorded on account (sum £358.40).
    M.record_on_account_credit(flat="3ECF48", amount="179.20", received_date="2026-08-20")
    M.record_on_account_credit(flat="3ECF48", amount="179.20", received_date="2026-08-20")

    p = _metro(tmp_path, [
        ["2026-08-20", "ASHBY CL /ADGD ASHBY", "Inward Payment", "180.04", "", "x"],   # confirms instalment
        ["2026-08-20", "EXAMPLE PROPERTY DEVERMEULEN", "Inward Payment", "180.04", "", "x"],    # confirms on-account
        ["2026-08-20", "K Hale HALE", "Inward Payment", "358.40", "", "x"],               # confirms sum of two
        ["2026-08-20", "NEWREF service charge", "Inward Payment", "250.00", "", "x"],        # attribute (new)
    ])
    r = M.reconcile_metro_csv(p, apply=False)
    s = r["summary"]
    assert s["confirms_manual"] == 3, s
    assert s["attribute"] == 1, s
    assert s["unmatched"] == 0, s

    # Balances before apply.
    al_bal = M.flat_statement("3ECF41")["balance_pence"]
    ap = M.reconcile_metro_csv(p, apply=True)
    # Confirmations add bank evidence, never a second allocation: Ashby's balance is unchanged.
    assert M.flat_statement("3ECF41")["balance_pence"] == al_bal
    assert ap["applied"]["confirmed"] == 3
    # Attribute applied £250 to the new flat: £100 clears its demand, £150 carried on account.
    assert {d["document_number"]: d["status"] for d in M.list_demands()}["26/3ECF49"] == "Paid"
    assert M.available_on_account_credit("3ECF49") == 15000
