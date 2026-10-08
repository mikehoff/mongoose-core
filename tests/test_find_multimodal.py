"""Unified search now spans email + files + attachments + voicemail + notifications, plus export."""
import datetime
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def _plant_notification(body):
    from mongoose.services.notifications import store_notification, NotificationIn
    store_notification(NotificationIn(source="t", app="Phone", title="note", body=body, privacy="normal"))


def _plant_voicemail(transcript):
    from mongoose.db import connect
    with connect() as con:
        cur = con.execute(
            "INSERT INTO voicemail_messages(source, provider, subject, notice_text, transcript, "
            "caller_number, privacy, indexed_at) VALUES ('t','voip_provider','VM','',?,?, 'normal', ?)",
            (transcript, "07700900123", datetime.datetime.now().isoformat()))
        vid = cur.lastrowid
        con.execute(
            "INSERT INTO voicemail_fts(rowid, provider, mailbox_label, account_number, caller_number, "
            "subject, notice_text, transcript, audio_filename) VALUES (?,?,?,?,?,?,?,?,?)",
            (vid, "voip_provider", "", "", "07700900123", "VM", "", transcript, ""))


def test_default_search_includes_voicemail_and_notifications():
    _fresh()
    from mongoose.services.find import find
    res = find("anything")
    assert "voicemail" in res["searched"]
    assert "notification" in res["searched"]


def test_voicemail_surfaces_in_unified_search():
    _fresh()
    _plant_voicemail("the boiler is making a noise please call")
    from mongoose.services.find import find
    res = find("boiler")
    kinds = {r["kind"] for r in res["results"]}
    assert "voicemail" in kinds


def test_notification_surfaces_in_unified_search():
    _fresh()
    _plant_notification("boiler engineer called about the leak")
    from mongoose.services.find import find
    res = find("boiler")
    kinds = {r["kind"] for r in res["results"]}
    assert "notification" in kinds


def test_indexed_counts_include_new_sources():
    _fresh()
    _plant_voicemail("x")
    _plant_notification("y")
    from mongoose.services.find import find
    res = find("nomatch_xyz")
    assert res["indexed_counts"]["voicemail"] == 1
    assert res["indexed_counts"]["notifications"] == 1


def test_export_markdown_groups_by_kind():
    _fresh()
    _plant_voicemail("boiler noise")
    _plant_notification("boiler leak")
    from mongoose.services.find import find_export
    exp = find_export("boiler", fmt="markdown")
    assert exp["ok"]
    assert exp["format"] == "markdown"
    assert "## Voicemails" in exp["content"]
    assert "## Notifications" in exp["content"]
    assert exp["count"] == 2


def test_export_json_is_parseable():
    _fresh()
    _plant_notification("invoice attached")
    from mongoose.services.find import find_export
    import json
    exp = find_export("invoice", fmt="json")
    parsed = json.loads(exp["content"])
    assert parsed["count"] >= 1


def test_export_preserves_privacy_masking():
    """A private item appears in the export (so it's complete) but its snippet stays masked."""
    _fresh()
    from mongoose.services.notifications import store_notification, NotificationIn
    store_notification(NotificationIn(source="t", app="Bank", title="secret balance",
                                      body="your balance is 12345", privacy="secret"))
    from mongoose.services.find import find_export
    exp = find_export("balance", fmt="markdown")
    # The private body must NOT appear verbatim in the export
    assert "12345" not in exp["content"]


def test_kinds_filter_still_restricts():
    _fresh()
    _plant_voicemail("boiler")
    _plant_notification("boiler")
    from mongoose.services.find import find
    res = find("boiler", kinds=("notification",))
    kinds = {r["kind"] for r in res["results"]}
    assert kinds <= {"notification"}
    assert "voicemail" not in kinds
