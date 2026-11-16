"""Owner review + approval of leaseholder access requests that could not be auto-verified by email,
plus the DB-backed auto-approve email map. Exercises the real rtm_portal service, WP calls mocked."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ.pop("RTM_PORTAL_FLAT_EMAILS", None)
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def _seed_flat(flat_id="1ECF1", name="Dr Morgan", email="alex@example.com"):
    from mongoose.services import rtm_management as M
    M._ensure_tables()
    from mongoose.db import connect
    with connect() as con:
        con.execute("INSERT INTO rtm_mgmt_flats(flat_id, block, flat_number, leaseholder_name, "
                    "leaseholder_email, payment_reference, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,datetime('now'),datetime('now'))",
                    (flat_id, "1EC", flat_id[-1], name, email, "MORGAN"))
        con.commit()


def test_auto_approve_map_reads_db():
    """Part A: the auto-approve email map comes from rtm_mgmt_flats.leaseholder_email, so it stays in
    sync with the RTM records with no separate list to maintain."""
    _fresh()
    _seed_flat(email="alex@example.com")
    from mongoose.services.rtm_portal import _flat_email_map
    m = _flat_email_map()
    assert m.get("1ECF1") == "alex@example.com"


def test_env_override_wins_over_db():
    _fresh()
    _seed_flat(email="old@example.com")
    os.environ["RTM_PORTAL_FLAT_EMAILS"] = '{"1ECF1": "New@Example.com"}'
    try:
        from mongoose.services.rtm_portal import _flat_email_map
        assert _flat_email_map().get("1ECF1") == "new@example.com"
    finally:
        os.environ.pop("RTM_PORTAL_FLAT_EMAILS", None)


def test_review_record_list_and_approve(monkeypatch):
    """A held request is listed with the on-file identity, approving provisions the account and clears
    it from the pending queue."""
    _fresh()
    _seed_flat(name="Dr Morgan", email="alex@example.com")
    from mongoose.services import rtm_portal as R

    calls = {"provision": 0, "decision": 0}

    def fake_wp_call(method, path, *, json_body=None, timeout=20.0):
        if "portal/provision" in path:
            calls["provision"] += 1
        elif "claim-decision" in path:
            calls["decision"] += 1
        return {"ok": True, "status": 200, "data": {"ok": True}}
    monkeypatch.setattr(R, "_wp_call", fake_wp_call)

    # A request for flat 1ECF1 from a DIFFERENT email than the one on file -> needs review.
    R._record_review_claim(claim_id=7, flat_id="1ECF1", name="Alex M",
                           email="personal@example.com", reason_code="email_unverified",
                           submitted_at="2026-07-11 10:00:00")
    lst = R.list_review_claims()
    assert len(lst) == 1
    assert lst[0]["claim_id"] == 7
    assert lst[0]["on_file_name"] == "Dr Morgan"
    assert lst[0]["on_file_email"] == "alex@example.com"
    assert lst[0]["email"] == "personal@example.com"

    res = R.approve_review_claim(7)
    assert res["ok"] is True and res["flat_id"] == "1ECF1"
    assert calls["provision"] == 1 and calls["decision"] == 1
    # No longer pending; recorded approved.
    assert R.list_review_claims("pending") == []
    assert len(R.list_review_claims("approved")) == 1

    # Approving again is a no-op error, never a double provision.
    assert R.approve_review_claim(7)["ok"] is False
    assert calls["provision"] == 1


def test_review_reject(monkeypatch):
    _fresh()
    _seed_flat()
    from mongoose.services import rtm_portal as R
    monkeypatch.setattr(R, "_wp_call", lambda *a, **k: {"ok": True, "status": 200, "data": {}})
    R._record_review_claim(claim_id=9, flat_id="1ECF1", name="X", email="x@example.com",
                           reason_code="email_unverified", submitted_at="")
    assert R.reject_review_claim(9, "not recognised")["ok"] is True
    assert R.list_review_claims("pending") == []
    assert len(R.list_review_claims("rejected")) == 1
    assert R.reject_review_claim(9)["ok"] is False  # already resolved


def test_approve_unknown_claim_fails(monkeypatch):
    _fresh()
    from mongoose.services import rtm_portal as R
    monkeypatch.setattr(R, "_wp_call", lambda *a, **k: {"ok": True})
    assert R.approve_review_claim(999)["ok"] is False
