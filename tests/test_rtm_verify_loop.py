"""The RTM publish/verify loop: Mongoose knows its LOCAL build, what it DEPLOYED, and what the live
site RUNS, and an active heartbeat closes the loop instead of waiting for a webhook. These tests pin
the honest-degradation behaviour (no WordPress -> a recorded heartbeat error, never an exception) and
the build-hash verdict (a same-version-different-build case is caught, an exact match stays green). DB
isolation is automatic via conftest."""
from __future__ import annotations


def test_local_build_info_is_graceful_without_source(monkeypatch):
    monkeypatch.delenv("MONGOOSE_PLUGIN_DIR", raising=False)
    monkeypatch.setenv("MONGOOSE_PLUGIN_DIR", "/nonexistent/mongoose-plugin-xyz")
    from mongoose.services import rtm_deploy_state as D
    info = D.local_build_info()
    assert info == {"version": None, "content_hash": None}


def test_verify_live_degrades_honestly_without_wordpress(monkeypatch):
    for k in ("WP_URL", "WP_USERNAME", "WP_APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    from mongoose.services import rtm_deploy_state as D
    res = D.verify_live()
    assert res["ok"] is False and res["reason"] == "wp_not_configured"
    s = D.sync_status()
    assert s["heartbeat_last_attempt_at"] is not None
    assert s["heartbeat_last_ok_at"] is None
    assert "not configured" in s["heartbeat_error"]


def test_exact_build_match_is_green(monkeypatch):
    from mongoose.services import rtm_deploy_state as D
    D.record_deployed(version="2.4.0", content_hash="deadbeefdeadbeef")
    D.record_live_confirmation(version="2.4.0", content_hash="deadbeefdeadbeef")
    s = D.sync_status()
    assert s["level"] == "green"
    assert s["build_mismatch"] is False
    assert s["deployed_hash"] == "deadbeefdeadbeef" and s["live_hash"] == "deadbeefdeadbeef"
    assert "example-court.org" in s["friendly_line"]


def test_same_version_different_build_is_caught(monkeypatch):
    from mongoose.services import rtm_deploy_state as D
    D.record_deployed(version="2.4.0", content_hash="aaaaaaaaaaaaaaaa")
    D.record_live_confirmation(version="2.4.0", content_hash="bbbbbbbbbbbbbbbb")
    s = D.sync_status()
    # versions agree but the live build hash differs: not a clean green
    assert s["level"] == "amber"
    assert s["build_mismatch"] is True


def test_version_only_history_keeps_its_verdict(monkeypatch):
    """A deploy/confirm pair with no hashes must behave exactly as before (green on version match)."""
    from mongoose.services import rtm_deploy_state as D
    D.record_deployed(version="9.9.9")
    D.record_live_confirmation(version="9.9.9")
    s = D.sync_status()
    assert s["level"] == "green"
    assert s["build_mismatch"] is False


def test_heartbeat_success_is_recorded(monkeypatch):
    from mongoose.services import rtm_deploy_state as D
    D._record_heartbeat(ok=True)
    s = D.sync_status()
    assert s["heartbeat_last_ok_at"] is not None
    assert s["heartbeat_error"] == ""
