"""Tests for the RTM two-schedule apportionment engine (docs/RTM_APPORTIONMENT_SPEC.md, T1-T4)."""
import os
import random
import tempfile

import pytest


@pytest.fixture()
def _db(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("MONGOOSE_DB_PATH", os.path.join(d, "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", d)
    from mongoose.services import rtm_management as M
    M._ensure_tables()
    from mongoose.services import rtm_apportionment as A
    A.seed_default_schedules()
    return A


def _seed_example_court_budget():
    from mongoose.db import connect
    s1 = [6100, 270, 860, 390, 3640, 960, 5780, 540, 130, 100, 470, 540, 1280, 270, 860, 370, 1120, 160, 1770, 4710]
    s2 = [640, 540, 210, 3660]
    with connect() as con:
        for i, amt in enumerate(s1):
            con.execute("INSERT INTO rtm_mgmt_budget_lines(year,schedule,budget_line,amount_pence,notes,created_at,updated_at) "
                        "VALUES('2027','sch1',?,?,'',datetime('now'),datetime('now'))", (f"line{i}", amt * 100))
        for i, amt in enumerate(s2):
            con.execute("INSERT INTO rtm_mgmt_budget_lines(year,schedule,budget_line,amount_pence,notes,created_at,updated_at) "
                        "VALUES('2027','sch2',?,?,'',datetime('now'),datetime('now'))", (f"p{i}", amt * 100))
        con.commit()


def test_largest_remainder_worked_example(_db):
    A = _db
    ex = A.largest_remainder(100003, {"A": 334, "B": 333, "C": 333})
    assert ex == {"A": 33401, "B": 33301, "C": 33301}
    assert sum(ex.values()) == 100003
    # deterministic
    assert A.largest_remainder(100003, {"A": 334, "B": 333, "C": 333}) == ex


def test_largest_remainder_random_exact_sum(_db):
    A = _db
    rnd = random.Random(1234)
    for _ in range(2000):
        n = rnd.randint(0, 5_000_000)
        k = rnd.randint(1, 20)
        cuts = sorted(rnd.sample(range(1, 1000), k - 1)) if k > 1 else []
        parts = [b - a for a, b in zip([0] + cuts, cuts + [1000])]
        w = {f"f{i}": parts[i] for i in range(k)}
        r = A.largest_remainder(n, w)
        assert sum(r.values()) == n
        assert all(v >= 0 for v in r.values())


def test_example_court_period_amounts(_db):
    A = _db
    _seed_example_court_budget()
    assert A.schedule_period_pence("sch1", "2027") == 1516000
    assert A.schedule_period_pence("sch2", "2027") == 252500
    assert A.schedule_period_pence("sch1", "2027") + A.schedule_period_pence("sch2", "2027") == 1768500


# Sample Example Court deed data (fictional; each 9-flat block sums to 50.0001%, so Schedule 1 carries
# the same 100.0002% rounding drift a real deed set does): (block, flat, sch1 %, sch2 %).
EXAMPLE_COURT = [
    ("1EC", 1, "6.2000", "5.556"), ("1EC", 2, "4.3000", "5.556"), ("1EC", 3, "6.1667", "5.556"),
    ("1EC", 4, "6.2000", "5.556"), ("1EC", 5, "4.3000", "5.556"), ("1EC", 6, "6.1667", "5.556"),
    ("1EC", 7, "6.2000", "5.556"), ("1EC", 8, "4.3000", "5.556"), ("1EC", 9, "6.1667", "5.556"),
    ("3EC", 1, "6.1667", "5.556"), ("3EC", 2, "4.3000", "5.556"), ("3EC", 3, "6.2000", "5.556"),
    ("3EC", 4, "6.1667", "5.556"), ("3EC", 5, "4.3000", "5.556"), ("3EC", 6, "6.2000", "5.556"),
    ("3EC", 7, "6.1667", "5.556"), ("3EC", 8, "4.3000", "5.556"), ("3EC", 9, "6.2000", "5.556"),
]


def _seed_example_court_apportionment(A):
    for block, num, s1, s2 in EXAMPLE_COURT:
        fid = f"{block}F{num}"
        A.set_apportionment(flat_id=fid, schedule="sch1", pct=s1)
        A.set_apportionment(flat_id=fid, schedule="sch2", pct=s2)


def test_pct_to_ppm_exact(_db):
    A = _db
    assert A.pct_to_ppm("6.2000") == 62000
    assert A.pct_to_ppm("4.3000%") == 43000
    assert A.pct_to_ppm("6.1667") == 61667
    assert A.pct_to_ppm("5.556") == 55560


def test_real_deed_data_within_tolerance(_db):
    A = _db
    _seed_example_court_apportionment(A)
    v1 = A.validate_schedule("sch1")
    assert v1["ok"] and v1["participant_count"] == 18
    assert v1["ppm_sum"] == 1_000_002  # 100.0002%, deed rounding, accepted
    v2 = A.validate_schedule("sch2")
    assert v2["ok"] and v2["participant_count"] == 18
    assert v2["ppm_sum"] == 1_000_080  # 100.008%, accepted


def test_real_deed_billing_penny_exact(_db):
    A = _db
    _seed_example_court_budget()
    _seed_example_court_apportionment(A)
    r1 = A.apportion_schedule_period("sch1", "2027")
    assert r1["ok"] and sum(r1["shares"].values()) == 1516000  # penny-exact despite 100.0002% drift
    r2 = A.apportion_schedule_period("sch2", "2027")
    assert r2["ok"] and sum(r2["shares"].values()) == 252500
    # Same-band flats get equal charges (deterministic); Morgan (1ECF1, 6.2000%) ~ £940 for the half-year.
    assert 93900 <= r1["shares"]["1ECF1"] <= 94100
    assert r1["shares"]["1ECF1"] == r1["shares"]["1ECF4"]  # both 6.2000%


def test_missing_flat_refuses_billing(_db):
    A = _db
    _seed_example_court_budget()
    _seed_example_court_apportionment(A)
    # Drop one flat's Schedule 1 share entirely -> sum falls ~6% -> beyond tolerance -> refused.
    A.set_apportionment(flat_id="1ECF1", schedule="sch1", ppm=0)
    v = A.validate_schedule("sch1")
    assert not v["ok"]
    r = A.apportion_schedule_period("sch1", "2027")
    assert not r["ok"] and "invalid" in r["error"]


def _seed_flats_and_apportionment(A):
    from mongoose.services import rtm_management as M
    for block, num, s1, s2 in EXAMPLE_COURT:
        M.upsert_flat(block=block, flat_number=num, leaseholder_name=f"L{block}{num}")
        fid = f"{block}F{num}"
        A.set_apportionment(flat_id=fid, schedule="sch1", pct=s1)
        A.set_apportionment(flat_id=fid, schedule="sch2", pct=s2)


def test_derive_demands_one_per_flat_deed_exact_idempotent(_db):
    A = _db
    from mongoose.services import rtm_management as M
    from mongoose.db import connect
    _seed_example_court_budget()
    _seed_flats_and_apportionment(A)
    d = A.derive_demands(year="2027", period="2026-H2")
    # ONE demand per flat (18), covering both schedules; deed-exact so the building over-recovers by pennies.
    assert d["ok"] and d["created"] == 18 and d["superseded"] == 0
    assert d["demanded_total"] == "£17,685.24"
    assert M.flat_statement("1ECF1")["balance"] == "£1,080.21"  # 6.2000%
    assert M.flat_statement("1ECF3")["balance"] == "£1,075.16"  # 6.1667%
    assert M.flat_statement("1ECF2")["balance"] == "£792.17"    # 4.3000%
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM rtm_mgmt_demands WHERE period='2026-H2' AND status='Issued'").fetchone()[0] == 18
    # Re-run supersedes all 18, never double-charges.
    d2 = A.derive_demands(year="2027", period="2026-H2")
    assert d2["created"] == 18 and d2["superseded"] == 18 and d2["demanded_total"] == "£17,685.24"
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM rtm_mgmt_demands WHERE period='2026-H2' AND status='Issued'").fetchone()[0] == 18


def test_subaccount_split_is_canonical_and_reconciles(_db):
    """GOLDEN reserve/service sub-account split. This pins the exact figures a hand-calc got wrong
    (£146.02 back-solved to the wrong 4710/30317 base) and enforces the reconciliation invariant, so the
    penny drift can never silently reappear. reserve = round-half-up(Sch1 half x 4710/30320); undercroft
    contributes nothing; service = remainder of the whole demand."""
    A = _db
    from mongoose.services import rtm_management as M
    from mongoose.services.rtm_trust import subaccount_split, reserve_fraction
    _seed_example_court_budget()
    _seed_flats_and_apportionment(A)
    A.derive_demands(year="2027", period="2026-H2")
    # The reserve fraction is exactly the reserve line over the Schedule-1 total.
    assert abs(reserve_fraction() - 4710 / 30320) < 1e-12
    # The three bands, pinned to the penny.
    expected = {"1ECF1": ("£1,080.21", "£146.01", "£934.20"),  # 6.2000%
                "1ECF3": ("£1,075.16", "£145.23", "£929.93"),  # 6.1667%
                "1ECF2": ("£792.17", "£101.27", "£690.90")}    # 4.3000%
    for fid, (tot, reserve, service) in expected.items():
        s = subaccount_split(fid)
        assert s["total"] == tot, f"{fid} total {s['total']} != {tot}"
        assert s["subaccount_split"]["reserve_fund_trust"] == reserve, f"{fid} reserve {s['subaccount_split']['reserve_fund_trust']} != {reserve}"
        assert s["subaccount_split"]["service_charge_trust"] == service, f"{fid} service {s['subaccount_split']['service_charge_trust']} != {service}"
    # Reconciliation invariant across every flat: the canonical split total equals the issued demand, and
    # reserve + service never leak a penny.
    for f in M.list_flats():
        fid = f["flat_id"]
        dem = [d for d in M.list_demands(flat=fid) if d.get("amount_pence", 0) > 0]
        if not dem:
            continue
        s = subaccount_split(fid)
        assert s["total"] == dem[0]["amount"], f"{fid} split total {s['total']} != demand {dem[0]['amount']}"
        assert s["reserve_pence"] + s["service_pence"] == s["total_pence"]


def test_reconcile_passes_on_valid_data(_db):
    A = _db
    _seed_example_court_budget()
    _seed_flats_and_apportionment(A)
    A.derive_demands(year="2027", period="2026-H2")
    rec = A.reconcile(year="2027", period="2026-H2", published_period_total_pence=1768500)
    assert rec["ok"] and rec["period_total_pence"] == 1768500 and rec["cross_total_ok"]


def test_credit_note_run_penny_exact_and_ledger(_db):
    A = _db
    from mongoose.services import rtm_management as M
    _seed_example_court_budget()
    _seed_flats_and_apportionment(A)
    A.derive_demands(year="2027", period="2026-H2")
    before = M.flat_statement("1ECF1")["balance_pence"]
    cn = A.credit_note_run(schedule="sch2", year="2027", period="2026-H2", actual_pence=200000,
                           reason="Undercroft underspend")
    # Deed-exact demanded for Schedule 2 is £2,525.22 (18 x £140.29), so the surplus over £2,000 spent
    # is £525.22, not £525.00 (the credit reflects what was actually demanded, not the round budget).
    assert cn["ok"] and cn["pool_pence"] == 52522 and cn["credited_pence"] == 52522
    assert len(cn["lines"]) == 18  # every participating flat credited
    after = M.flat_statement("1ECF1")["balance_pence"]
    assert after < before  # credit reduced the balance
    assert len(A.credit_notes_for_flat("1ECF1")) == 1


def test_credit_note_deficit_gives_nothing(_db):
    A = _db
    _seed_example_court_budget()
    _seed_flats_and_apportionment(A)
    A.derive_demands(year="2027", period="2026-H2")
    # actual exceeds demanded -> no credit (a balancing charge is separate)
    cn = A.credit_note_run(schedule="sch2", year="2027", period="2026-H2", actual_pence=300000)
    assert cn["ok"] and cn["pool_pence"] == 0 and cn["credited_pence"] == 0


def test_credit_note_pdf_rendered(_db):
    A = _db
    _seed_example_court_budget()
    _seed_flats_and_apportionment(A)
    A.derive_demands(year="2027", period="2026-H2")
    cn = A.credit_note_run(schedule="sch2", year="2027", period="2026-H2", actual_pence=200000, reason="x")
    assert cn["lines"] and cn["lines"][0].get("pdf_key")
    p = A._credit_note_dir() / cn["lines"][0]["pdf_key"]
    assert p.exists() and p.read_bytes()[:4] == b"%PDF"
