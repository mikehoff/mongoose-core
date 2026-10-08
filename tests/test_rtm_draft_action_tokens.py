import asyncio
import inspect


def _fresh(tmp_path, monkeypatch):
    db = tmp_path / "mongoose.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_API_TOKEN", "master-secret-should-not-leave")
    from mongoose.db import init_db
    init_db(db)


def test_rtm_action_tokens_are_scoped_single_use(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.services import rtm_intake as R
    token = R._mint_draft_action_token("draft-1", action="approve")
    assert R.consume_draft_action_token(token, "draft-1", "approve") is True
    assert R.consume_draft_action_token(token, "draft-1", "approve") is False
    cancel = R._mint_draft_action_token("draft-2", action="cancel")
    assert R.consume_draft_action_token(cancel, "draft-2", "approve") is False
    assert R.consume_draft_action_token(cancel, "draft-x", "cancel") is False
    assert R.consume_draft_action_token(cancel, "draft-2", "cancel") is True


def test_rtm_push_draft_approval_does_not_embed_master_token(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from mongoose.services import rtm_intake as R
    captured = {}

    async def fake_send_to_phone(title, text, priority="default", tags=None, actions=None, **kwargs):
        captured["actions"] = actions or []
        return {"sent": True, "actions": actions or []}

    monkeypatch.setattr("mongoose.services.notifications.send_to_phone", fake_send_to_phone)
    res = R.intake_email(sender="Leaseholder <a@example.com>", subject="Door", body="communal door broken")
    draft = R.generate_draft(res["intake_id"])
    pushed = asyncio.run(R.push_draft_approval(draft["draft_id"]))
    assert pushed["sent"] is True
    actions = captured["actions"]
    assert actions and len(actions) == 2
    assert "X-Mongoose-Token" not in str(actions)
    assert "master-secret-should-not-leave" not in str(actions)
    assert all("X-RTM-Draft-Action-Token" in a.get("headers", {}) for a in actions)


def test_rtm_routes_accept_scoped_action_token(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    from mongoose.core_app import app
    from mongoose.services import rtm_intake as R
    res = R.intake_email(sender="Leaseholder <a@example.com>", subject="Door", body="communal door broken")
    draft = R.generate_draft(res["intake_id"])
    token = R._mint_draft_action_token(draft["draft_id"], action="cancel")
    c = TestClient(app)
    r = c.post(f"/rtm/drafts/{draft['draft_id']}/cancel", headers={"X-RTM-Draft-Action-Token": token})
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    replay = c.post(f"/rtm/drafts/{draft['draft_id']}/cancel", headers={"X-RTM-Draft-Action-Token": token})
    assert replay.status_code == 403


def test_rtm_push_source_does_not_reference_x_mongoose_token():
    from mongoose.services import rtm_intake as R
    src = inspect.getsource(R.push_draft_approval)
    assert "X-Mongoose-Token" not in src
    assert "MONGOOSE_API_TOKEN" not in src
    assert "X-RTM-Draft-Action-Token" in src
