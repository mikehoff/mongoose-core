"""Search coverage: a voicemail email that was never promoted into the canonical voicemail table must
still surface when searching voicemails, deduped against emails already promoted."""
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
    from mongoose.db import init_db
    init_db()


def _ingest(**kw):
    from mongoose.services.email_index import ingest_email, EmailIn
    from mongoose.db import utc_now_iso
    kw.setdefault("received_at", utc_now_iso())
    return ingest_email(EmailIn(**kw))


def test_recogniser_flags_only_voicemail_like_email():
    from mongoose.services import voicemail as VM
    assert VM._email_looks_like_voicemail("voicemail@voip.example", "New message in mailbox", "x") is True
    assert VM._email_looks_like_voicemail("noreply@acme.com", "You have a new voicemail", "listen") is True
    assert VM._email_looks_like_voicemail("pa@x.com", "Missed a call from a leaseholder", "") is True
    assert VM._email_looks_like_voicemail("billing@acme.com", "Service charge invoice", "pay please") is False
    assert VM._email_looks_like_voicemail("jordan@home.com", "dinner?", "pizza tonight") is False


def test_unpromoted_voicemail_email_surfaces_and_invoice_does_not():
    _fresh()
    from mongoose.services import voicemail as VM
    _ingest(source="imap", source_message_id="vm-1", mailbox="INBOX", sender="noreply@acme-voice.com",
            subject="New voicemail about Example Court",
            body="You have a new voicemail message regarding Example Court. Tap to listen.")
    _ingest(source="imap", source_message_id="inv-1", mailbox="INBOX", sender="billing@acme.com",
            subject="Example Court service charge invoice",
            body="Please find your Example Court service charge invoice attached.")
    res = VM.search_voicemails("example court")
    subs = [r.get("subject") for r in res]
    assert any("voicemail" in (s or "").lower() for s in subs)
    assert not any("invoice" in (s or "").lower() for s in subs)
    # fallback rows are clearly marked so callers never mistake them for canonical voicemails
    vm = [r for r in res if "voicemail" in (r.get("subject") or "").lower()][0]
    assert vm["origin"] == "email" and vm["transcript_status"] == "email_only" and vm["id"] is None


def test_fallback_can_be_disabled():
    _fresh()
    from mongoose.services import voicemail as VM
    _ingest(source="imap", source_message_id="vm-2", mailbox="INBOX", sender="noreply@voice.com",
            subject="New voicemail about Example Court", body="new voicemail message, tap to listen")
    assert VM.search_voicemails("example court", include_email_fallback=False) == []


def test_promoted_email_is_not_double_listed_by_fallback():
    _fresh()
    from mongoose.services import voicemail as VM
    from mongoose.db import connect, utc_now_iso
    r = _ingest(source="imap", source_message_id="vm-3", mailbox="INBOX", sender="noreply@voice.com",
                subject="New voicemail about Example Court", body="new voicemail message, tap to listen")
    email_id = r["id"]
    # present before promotion
    before = [x for x in VM.search_voicemails("example court") if x.get("origin") == "email"]
    assert any(x["email_id"] == email_id for x in before)
    # promote: a canonical voicemail row linked to that email
    with connect() as con:
        con.execute(
            "INSERT INTO voicemail_messages (email_id, source, provider, subject, transcript_status, source_refs, indexed_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (email_id, "imap", "acme", "New voicemail about Example Court", "done", "[]", utc_now_iso()),
        )
        con.commit()
    after_ids = [x.get("email_id") for x in VM.search_voicemails("example court") if x.get("origin") == "email"]
    assert email_id not in after_ids
