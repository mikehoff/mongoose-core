"""Tests for the Metro ingest patch: reference matching robustness (the 18-char Open Banking limit +
messy name rendering), the folder-watch ingest, and the Open Banking provider status. This allocates
REAL trust money, so the matcher tests are exhaustive on the realistic name variants and the
false-positive guards."""
from __future__ import annotations

import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    for k in [k for k in os.environ if k.startswith("MONGOOSE_METRO")]:
        os.environ.pop(k)
    from mongoose.db import init_db
    init_db()
    return d


# ---- canonical reference matching ----

def test_canon_ref_collapses_renderings():
    _fresh()
    from mongoose.services.rtm_management import _canon_ref
    for v in ["DE VERMEULEN", "DE-VERMEULEN", "DEVERMEULEN", "de vermeulen"]:
        assert _canon_ref(v) == "devermeulen"
    for v in ["O'BRIEN", "O BRIEN", "OBRIEN"]:
        assert _canon_ref(v) == "obrien"


def test_distinctive_tokens_ignore_particles():
    _fresh()
    from mongoose.services.rtm_management import _distinctive_tokens
    # generic particles alone are NOT distinctive
    assert _distinctive_tokens("DE") == set()
    assert _distinctive_tokens("VAN") == set()
    assert _distinctive_tokens("DE FLAT 6") == set()
    # a real surname is
    assert "vermeulen" in _distinctive_tokens("DE VERMEULEN")


def _setup_register():
    """Real-Register flats + £901 demands so the matcher has outstanding amounts to bind to."""
    from mongoose.services import rtm_management as R
    R.upsert_flat(block="1", flat_number="1", leaseholder_name="De Vermeulen", payment_reference="DE VERMEULEN")
    R.upsert_flat(block="1", flat_number="2", leaseholder_name="O'Brien", payment_reference="O'BRIEN")
    R.upsert_flat(block="1", flat_number="3", leaseholder_name="Whitfield", payment_reference="WHITFIELD")
    R.create_demand(document_number="D1", flat="1ECF1", period="2026-Q1", amount=901, due_date="2026-07-16")
    R.create_demand(document_number="D2", flat="1ECF2", period="2026-Q1", amount=901, due_date="2026-07-16")
    R.create_demand(document_number="D3", flat="1ECF3", period="2026-Q1", amount=901, due_date="2026-07-16")
    return R


def test_de_vermeulen_variants_all_match():
    _fresh()
    R = _setup_register()
    for bank in ["DE VERMEULEN", "DE-VERMEULEN", "N DE VERMEULEN", "DE VERMEULEN RENT", "DEVERMEULEN"]:
        r = R._match_bank_credit(bank, "", 90100)
        assert r["match_status"] == "exact", f"{bank!r} did not match: {r}"
        assert r["flat_id"] == "1ECF1", f"{bank!r} matched wrong flat: {r}"


def test_obrien_variants_all_match():
    _fresh()
    R = _setup_register()
    for bank in ["O'BRIEN", "O BRIEN", "OBRIEN", "O'BRIEN FLAT 2"]:
        r = R._match_bank_credit(bank, "", 90100)
        assert r["match_status"] == "exact", f"{bank!r} did not match: {r}"
        assert r["flat_id"] == "1ECF2", f"{bank!r} matched wrong flat: {r}"


def test_dropped_particle_matches_via_distinctive_token():
    """'VERMEULEN' (payer dropped 'DE') still resolves via the distinctive-token fallback."""
    _fresh()
    R = _setup_register()
    r = R._match_bank_credit("VERMEULEN", "", 90100)
    assert r["match_status"] == "exact"
    assert r["flat_id"] == "1ECF1"


def test_false_positives_never_match_a_leaseholder():
    """Generic particles / words must NEVER pull in a leaseholder , wrong allocation of trust money is
    worse than leaving it in review."""
    _fresh()
    R = _setup_register()
    for bank in ["DE", "DE FLAT 6", "VAN", "PAYMENT", "RENT", "REF"]:
        r = R._match_bank_credit(bank, "", 90100)
        assert r["match_status"] != "exact", f"{bank!r} wrongly matched: {r}"


def test_each_leaseholder_resolves_uniquely():
    _fresh()
    R = _setup_register()
    assert R._match_bank_credit("DE VERMEULEN", "", 90100)["flat_id"] == "1ECF1"
    assert R._match_bank_credit("O'BRIEN", "", 90100)["flat_id"] == "1ECF2"
    assert R._match_bank_credit("WHITFIELD", "", 90100)["flat_id"] == "1ECF3"


# ---- UK date parsing (Metro exports day-first) ----

def test_parse_date_handles_uk_dayfirst():
    _fresh()
    from mongoose.services.rtm_management import _parse_date
    assert _parse_date("16/07/2026") == "2026-07-16"   # DD/MM/YYYY (Metro)
    assert _parse_date("2026-07-16") == "2026-07-16"   # ISO still works
    assert _parse_date("16 Jul 2026") == "2026-07-16"  # statement style


def test_parse_date_rejects_garbage():
    _fresh()
    from mongoose.services.rtm_management import _parse_date
    import pytest
    with pytest.raises(ValueError):
        _parse_date("not a date")


# ---- demand-side 18-char warning (non-blocking) ----

def test_ref_warnings_flag_long_reference():
    _fresh()
    from mongoose.services.rtm_management import _ref_warnings
    warns = _ref_warnings("THIS REFERENCE IS WAY TOO LONG FOR OPEN BANKING")
    assert warns and any("18" in w for w in warns)


def test_upsert_flat_warns_but_does_not_reject():
    _fresh()
    from mongoose.services import rtm_management as R
    r = R.upsert_flat(block="1", flat_number="9", payment_reference="A VERY LONG PAYMENT REFERENCE INDEED")
    assert r["ok"] is True                       # non-blocking
    assert "reference_warnings" in r


# ---- Open Banking provider status (opt-in, documented) ----

def test_ob_default_is_local_csv_path():
    _fresh()
    from mongoose.services.metro_ob import status
    s = status()
    assert s["ob_enabled"] is False
    assert "local CSV" in s["active_path"]


def test_ob_flips_to_nominated_provider():
    _fresh()
    os.environ["MONGOOSE_METRO_OB_PROVIDER"] = "xero"
    import importlib
    import mongoose.settings
    importlib.reload(mongoose.settings)
    import mongoose.services.metro_ob as M
    importlib.reload(M)
    s = M.status()
    assert s["ob_enabled"] is True
    assert s["provider"] == "xero"
    assert s["reconsent_days"] == 90
    os.environ.pop("MONGOOSE_METRO_OB_PROVIDER", None)


def test_ob_read_only_boundary_stated():
    _fresh()
    from mongoose.services.metro_ob import list_providers
    info = list_providers()
    assert "move money" in info["boundary"]


# ---- folder-watch ingest ----

def test_metro_watch_skips_when_no_dir():
    _fresh()
    from mongoose.services.metro_watch import scan_once
    assert scan_once().get("skipped") == "no_watch_dir_configured"


def test_metro_watch_status_reports_unconfigured():
    _fresh()
    from mongoose.services.metro_watch import status
    s = status()
    assert s["configured"] is False


# ---- legal-structure correctness (an RTM is a limited company, not a sole trader) ----

def test_ob_states_rtm_is_limited_company():
    """The decisive fact for cost: an RTM is limited by guarantee, so sole-trader free tiers don't
    apply. This must be stated, not implied away."""
    _fresh()
    from mongoose.services.metro_ob import list_providers, legal_structure_note
    note = legal_structure_note()
    assert "LIMITED BY GUARANTEE" in note
    assert "sole trader" in note.lower()
    lp = list_providers()
    assert "legal_structure" in lp


def test_crunch_plus_is_strong_packaged_candidate():
    """The current playbook treats Crunch Plus as the strongest low-cost packaged Metro test."""
    _fresh()
    from mongoose.services.metro_ob import RECOGNISED_PROVIDERS, list_providers
    assert "crunch" in RECOGNISED_PROVIDERS
    crunch = RECOGNISED_PROVIDERS["crunch"]
    assert "£10" in crunch["cost"]
    assert "both" in crunch["best_for"].lower()
    assert "crunch" in list_providers()["cheapest_strong_packaged_candidate"]


def test_report_products_are_all_in_playbook():
    _fresh()
    from mongoose.services.metro_ob import RECOGNISED_PROVIDERS
    expected = {"xero", "crunch", "plaid", "pandle", "gocardless", "moneyhub", "yapily", "clearbooks", "truelayer", "finverse"}
    assert expected.issubset(set(RECOGNISED_PROVIDERS))


def test_finverse_requires_written_confirmation():
    _fresh()
    from mongoose.services.metro_ob import RECOGNISED_PROVIDERS
    assert "written" in RECOGNISED_PROVIDERS["finverse"]["note"].lower() or "written" in RECOGNISED_PROVIDERS["finverse"]["expectation"].lower()


def test_pandle_is_low_cost_screening_not_verified_metro_match():
    _fresh()
    from mongoose.services.metro_ob import RECOGNISED_PROVIDERS
    p = RECOGNISED_PROVIDERS["pandle"]
    assert "£5" in p["cost"]
    assert "live" in p["expectation"].lower() or "search" in p["expectation"].lower()


# ---- HARDENING: reference survivability across provider export column names ----

def _import_one_row(cols, rowvals):
    """Write a one-row CSV with the given headers and import it; return the import summary."""
    import csv as _csv
    from mongoose.services import rtm_management as R
    d = tempfile.mkdtemp()
    p = os.path.join(d, "export.csv")
    with open(p, "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerow(rowvals)
    return R.import_metro_csv(p)


def test_reference_survives_in_particulars_column():
    """Xero-style 'Particulars' column carrying the reference must still auto-match."""
    _fresh(); _setup_register()
    r = _import_one_row(["Date", "Particulars", "Amount"],
                        {"Date": "2026-06-01", "Particulars": "WHITFIELD", "Amount": "901.00"})
    assert (r.get("exact", 0) + r.get("partial", 0)) == 1, r


def test_reference_survives_in_memo_and_remittance_columns():
    for col in ["Memo", "Remittance Information", "Notes", "Analysis Code"]:
        _fresh(); _setup_register()
        r = _import_one_row(["Date", col, "Amount"],
                            {"Date": "2026-06-01", col: "WHITFIELD service charge", "Amount": "901.00"})
        assert (r.get("exact", 0) + r.get("partial", 0)) == 1, f"{col}: {r}"


def test_reference_survives_in_unexpected_column():
    """A reference dumped into a column the importer never heard of is still found via the free-text
    blob fed to the matcher."""
    _fresh(); _setup_register()
    r = _import_one_row(["Date", "SomeWeirdColumn", "Amount"],
                        {"Date": "2026-06-01", "SomeWeirdColumn": "WHITFIELD", "Amount": "901.00"})
    assert (r.get("exact", 0) + r.get("partial", 0)) == 1, r


def test_reference_split_across_two_columns():
    """Reference in one column, payer detail in another , the combined blob still matches."""
    _fresh(); _setup_register()
    r = _import_one_row(["Date", "Reference", "Memo", "Amount"],
                        {"Date": "2026-06-01", "Reference": "FPS CREDIT", "Memo": "WHITFIELD", "Amount": "901.00"})
    assert (r.get("exact", 0) + r.get("partial", 0)) == 1, r


# ---- HARDENING: matching quality (truncation + one dropped token) ----

def test_reverse_containment_recovers_truncated_leading_fragment():
    """An OB-truncated payment that arrives as only the leading fragment of a longer stored reference is
    recovered by reverse canonical containment."""
    _fresh()
    from mongoose.services import rtm_management as R
    R.upsert_flat(block="1", flat_number="4", leaseholder_name="Longname", payment_reference="DEVERMEULEN1ECF4")
    R.create_demand(document_number="D4", flat="1ECF4", period="2026-Q1", amount=901, due_date="2026-07-16")
    # bank delivered only the first 9 canonical chars
    r = _import_one_row(["Date", "name", "Amount"],
                        {"Date": "2026-06-01", "name": "DEVERMEUL", "Amount": "901.00"})
    assert (r.get("exact", 0) + r.get("partial", 0)) == 1, r


def test_one_dropped_token_still_matches_multi_token_ref():
    """A two-distinctive-token reference still matches when the payer typed only one of the two
    distinctive tokens (tolerate one missing), while a single-token ref never falls through to this."""
    _fresh()
    from mongoose.services import rtm_management as R
    R.upsert_flat(block="1", flat_number="5", leaseholder_name="Two Names", payment_reference="WHITFIELD VERMEULEN")
    R.create_demand(document_number="D5", flat="1ECF5", period="2026-Q1", amount=901, due_date="2026-07-16")
    r = _import_one_row(["Date", "Reference", "Amount"],
                        {"Date": "2026-06-01", "Reference": "WHITFIELD", "Amount": "901.00"})
    assert (r.get("exact", 0) + r.get("partial", 0)) == 1, r


# ---- HARDENING: the safety guards must still hold ----

def test_numeric_cells_never_create_a_match():
    """A balance/amount number equal to a flat's reference digits must NOT auto-match (numbers are
    excluded from the free-text blob)."""
    _fresh()
    from mongoose.services import rtm_management as R
    R.upsert_flat(block="1", flat_number="6", leaseholder_name="Numeric", payment_reference="6041")
    R.create_demand(document_number="D6", flat="1ECF6", period="2026-Q1", amount=901, due_date="2026-07-16")
    # 6041 appears only as a Balance number, never as a reference
    r = _import_one_row(["Date", "Description", "Amount", "Balance"],
                        {"Date": "2026-06-01", "Description": "bank transfer", "Amount": "901.00", "Balance": "6041"})
    assert r.get("unmatched", 0) == 1 and (r.get("exact", 0) + r.get("partial", 0)) == 0, r


def test_random_payer_with_no_reference_stays_unmatched():
    _fresh(); _setup_register()
    r = _import_one_row(["Date", "name", "Amount"],
                        {"Date": "2026-06-01", "name": "FPS CREDIT J BLOGGS", "Amount": "901.00"})
    assert r.get("unmatched", 0) == 1 and (r.get("exact", 0) + r.get("partial", 0)) == 0, r


def test_reverse_containment_never_misbinds_shared_fragments():
    """The loosened reverse-containment match must NOT mis-allocate when two flats share a leading
    fragment: an ambiguous fragment stays unmatched, a short fragment (below the 5-char floor) never
    matches, and only a fragment unique to ONE flat binds."""
    _fresh()
    from mongoose.services import rtm_management as R
    R.upsert_flat(block="1", flat_number="1", leaseholder_name="A", payment_reference="SMITHSON1ECF1")
    R.upsert_flat(block="1", flat_number="2", leaseholder_name="B", payment_reference="SMITHFIELD1ECF2")
    R.create_demand(document_number="D1", flat="1ECF1", period="2026", amount=901, due_date="2026-07-16")
    R.create_demand(document_number="D2", flat="1ECF2", period="2026", amount=901, due_date="2026-07-16")

    def status(needle):
        return R._match_bank_credit("", needle, 90100, extra=needle)["match_status"]

    assert status("SMITH") == "unmatched"      # ambiguous: in both -> manual review
    assert status("SMIT") == "unmatched"       # below the 5-char fragment floor
    assert status("JOHNSON") == "unmatched"    # unrelated
    m = R._match_bank_credit("", "SMITHSON", 90100, extra="SMITHSON")
    assert m["flat_id"] == "1ECF1"             # unique fragment binds correctly
