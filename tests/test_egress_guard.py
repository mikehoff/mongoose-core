"""The execution-context wall: a test process must NEVER reach a live external system.

This very test runs under pytest, so `pytest` is in sys.modules -> egress_allowed() must be False here, and
every wired surface must refuse, so test fixtures can never reach a live leaseholder portal."""
from mongoose.services.egress_guard import egress_allowed, assert_live_egress, EgressBlocked
import pytest


def test_egress_is_blocked_under_pytest():
    ok, why = egress_allowed("anything")
    assert ok is False           # because pytest is imported into this process
    assert "pytest" in why or "test" in why


def test_assert_raises_under_test():
    with pytest.raises(EgressBlocked):
        assert_live_egress("wp_sync")


def test_wp_call_refuses_in_test_context(monkeypatch):
    # The exact surface that leaked Nina/Nadia/£300: _wp_call must now short-circuit before any network.
    from mongoose.services import rtm_portal as P
    res = P._wp_call("POST", "/wp-json/example-court/v1/sync/snapshot", json_body={"flats": [{"x": 1}]})
    assert res["ok"] is False
    assert "egress" in res["error"].lower()


def test_publish_snapshot_writes_nothing_from_a_test():
    # End to end: even the full publish path is inert under test.
    from mongoose.services import rtm_portal as P
    res = P.publish_portal_snapshot(full=False)
    assert res.get("ok") is False


def test_data_dir_signal_independent_of_pytest(monkeypatch):
    # Belt-and-braces: an ephemeral data dir also blocks, so a non-pytest sandbox is caught too.
    import mongoose.services.egress_guard as G
    monkeypatch.setattr(G, "_under_test", lambda: False)
    monkeypatch.setenv("MONGOOSE_DATA_DIR", "/private/tmp/pytest-of-alex/x/data")
    assert egress_allowed("x")[0] is False
