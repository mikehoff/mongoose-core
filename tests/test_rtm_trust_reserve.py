"""Acceptance tests for reserve maths (Layer 2 section 3; acceptance 7.2, 7.3, 7.6)."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from mongoose.services import rtm_trust as T
from mongoose.services import rtm_example_court as PC


def test_reserve_fraction_is_reserve_over_sch1_total():
    # 4710 / 30320 = 0.15534 from the live budget (sch1 total includes the reserve line).
    frac = T.reserve_fraction()
    assert round(frac, 5) == 0.15534
    reserve, sch1 = T._budget_reserve_and_sch1()
    assert reserve == 4710
    assert sch1 == 30320
    assert abs(frac - reserve / sch1) < 1e-9


def test_changing_the_budget_line_changes_the_fraction(monkeypatch):
    # If the reserve budget line changes, the fraction must follow.
    original = PC.EXAMPLE_COURT_BUDGET
    bumped = []
    for (label, schedule, amount) in original:
        if "reserve" in label.lower():
            bumped.append((label, schedule, 9420.0))  # double the reserve line
        else:
            bumped.append((label, schedule, amount))
    monkeypatch.setattr(PC, "EXAMPLE_COURT_BUDGET", bumped)
    reserve, sch1 = T._budget_reserve_and_sch1()
    assert reserve == 9420
    # sch1 total rises by the same 4710, so new total = 35030
    assert sch1 == 30320 + 4710
    assert round(T.reserve_fraction(), 5) == round(9420 / 35030, 5)


def test_small_band_half_year_reserve_is_101_27():
    # A small-band flat (4.3000%) reserves £101.27 for the half-year on its Schedule 1 allocation.
    sch1_half_pence = int(round(30320 / 2 * 100))
    sch1_alloc = int(round(0.043 * sch1_half_pence))
    portion = T.reserve_portion("1ECF5", sch1_alloc)
    assert T._money(portion) == "£101.27"


def test_all_flats_fully_paid_reserve_sums_to_half_year_reserve():
    # All 18 flats fully paid for the half-year should reserve reserve_annual/2 = £2,355 to the penny.
    # Each flat's Schedule 1 half-year allocation = its share of the sch1 half-year budget.
    # Sum of reserve portions over all flats must equal 235500 pence.
    sch1_half_pence = int(round(30320 / 2 * 100))
    total_half_demand = sum(Decimal(a) for (_b, _n, _s, _r, a, _note) in PC.EXAMPLE_COURT_FLATS)
    total_reserve = 0
    for (_b, _n, _s, _r, amt, _note) in PC.EXAMPLE_COURT_FLATS:
        share = Decimal(amt) / total_half_demand
        flat_sch1_alloc = int((Decimal(sch1_half_pence) * share).quantize(Decimal("1"), ROUND_HALF_UP))
        total_reserve += T.reserve_portion("x", flat_sch1_alloc)
    # Allow at most a couple of pence of rounding noise across 18 roundings, but the target is 2355.00.
    assert abs(total_reserve - 235500) <= 2


def test_schedule_2_contributes_zero_reserve():
    # The reserve portion is computed from the Schedule 1 allocation only. A pure Schedule 2
    # allocation passed as the sch1 part is, by construction, zero; callers must pass sch1-only.
    assert T.reserve_portion("x", 0) == 0


def test_reserve_in_transit_rises_on_collection_and_falls_on_matched_transfer(tmp_path, monkeypatch):
    db = tmp_path / "t.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    # collect a reserve slice
    T.record_reserve_collected(flat_id="1ECF5", sch1_allocation_pence=65190, document_number="D1",
                               occurred_on="2026-07-01")
    pos1 = T.reserve_position()
    assert pos1["in_transit_pence"] > 0
    collected = pos1["collected_pence"]
    # a confirmed transfer landing in reserve reduces in_transit
    T.record_reserve_transferred(amount=Decimal(collected) / 100, occurred_on="2026-07-03")
    pos2 = T.reserve_position()
    assert pos2["transferred_pence"] == collected
    assert pos2["in_transit_pence"] == 0
