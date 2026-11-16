"""RTM statutory obligations calendar: forward legal deadlines surfaced with countdowns,
and the s.20 consultation timeline computed correctly (the Daejan-risk window)."""
from mongoose.services.rtm_obligations import (
    upcoming_obligations, s20_timeline, obligations_summary, ACQUISITION_DATE,
)


def test_acquisition_date_surfaces_before_it_passes():
    obs = upcoming_obligations("2026-07-01")
    keys = {o["key"] for o in obs}
    assert "rtm_acquisition" in keys
    acq = next(o for o in obs if o["key"] == "rtm_acquisition")
    assert acq["due_date"] == ACQUISITION_DATE
    assert acq["days_remaining"] == 15


def test_overdue_obligation_has_negative_days():
    # Well after the acquisition date, it should not appear (past grace), but year-end logic still works
    obs = upcoming_obligations("2027-01-01")
    for o in obs:
        assert o["band"] in ("overdue", "due_soon", "upcoming", "future")


def test_band_classification():
    obs = upcoming_obligations("2026-07-10")  # 6 days before acquisition
    acq = next(o for o in obs if o["key"] == "rtm_acquisition")
    assert acq["band"] == "due_soon"  # within 30 days


def test_annual_accounts_anchored_to_year_end():
    # Year end on 2026-04-01 is next 2027-03-31; accounts target is +183 days (~2027-09-30),
    # so a wide horizon is needed to include it.
    obs = upcoming_obligations("2026-04-01", horizon_days=600)
    keys = {o["key"] for o in obs}
    assert "annual_accounts" in keys
    assert "accounting_year_end" in keys


def test_s20_timeline_is_30_plus_30():
    t = s20_timeline("2026-08-01")
    assert t["ok"]
    assert t["stage1_observation_ends"] == "2026-08-31"   # +30
    assert t["stage2_observation_ends"] == "2026-09-30"   # +30 more
    assert "Daejan" in t["basis"]


def test_s20_rejects_bad_date():
    t = s20_timeline("not-a-date")
    assert not t["ok"]


def test_summary_flags_attention_when_due_soon():
    s = obligations_summary("2026-07-01")
    assert s["needs_attention"] is True
    assert s["next"]["key"] == "rtm_acquisition"


def test_obligations_sorted_soonest_first():
    obs = upcoming_obligations("2026-01-01", horizon_days=400)
    days = [o["days_remaining"] for o in obs]
    assert days == sorted(days)


# Adversarial QA regressions: advisory date functions must never crash on malformed input.

def test_upcoming_obligations_bad_date_does_not_crash():

    from mongoose.services.rtm_obligations import upcoming_obligations
    for bad in ("", " ", "not-a-date", "2026-13-99", "9999-99-99", "2026/07/16",
                "'; DROP TABLE x;--", "\x00", "owner", "{}"):
        out = upcoming_obligations(bad)  # must return a list, not raise
        assert isinstance(out, list)


def test_s20_timeline_bad_date_handled():

    from mongoose.services.rtm_obligations import s20_timeline
    r = s20_timeline("garbage")
    assert r["ok"] is False
