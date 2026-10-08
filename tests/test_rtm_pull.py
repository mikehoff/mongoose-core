"""Mongoose pulls RTM documents from the WordPress plugin queue (CGNAT-safe, outbound) over the
existing signed portal channel (rtm_portal._wp_call). That channel is mocked so no network and no
plugin are needed; a temp DB + files schema exercises the real ingest."""
import base64

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "t.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    from mongoose.db import connect
    with connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, "
                    "title TEXT, body TEXT, mtime REAL, device TEXT, indexed_at TEXT)")
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS file_fts USING fts5(title, body, path, content='')")
        con.commit()
    import importlib
    import mongoose.services.rtm_docs as rd
    import mongoose.services.rtm_pull as rp
    importlib.reload(rd)
    importlib.reload(rp)
    import mongoose.services.ocr as ocr
    monkeypatch.setattr(ocr, "ocr_image", lambda p, timeout=90: "")
    return rp


def _b64(s):
    return base64.b64encode(s.encode()).decode()


def _install_wp_call(monkeypatch, *, documents, notes=None, sink=None):
    """Patch rtm_portal._wp_call with a fake router. sink records ack ids by route tail."""
    import mongoose.services.rtm_portal as portal

    def fake(method, path, *, json_body=None, timeout=20.0):
        if method == "GET" and "/documents/pending" in path:
            return {"ok": True, "data": {"documents": documents}}
        if method == "GET" and "/documents/notes/pending" in path:
            return {"ok": True, "data": {"notes": notes or []}}
        if method == "POST" and path.endswith("/ack"):
            if sink is not None:
                sink[path.rsplit("/example-court/v1", 1)[-1]] = list(json_body["ids"])
            return {"ok": True, "data": {"consumed": len(json_body["ids"])}}
        return {"ok": False, "status": 404, "error": "route not found"}

    monkeypatch.setattr(portal, "_wp_call", fake)


def test_not_configured_is_a_clean_noop(env, monkeypatch):
    import mongoose.services.rtm_portal as portal
    monkeypatch.setattr(portal, "_wp_call",
                        lambda *a, **k: {"ok": False, "error": "WordPress not configured"})
    r = env.pull_now()
    assert r["ok"] is False and "not configured" in r["error"]


def test_pull_ingests_and_acks(env, monkeypatch):
    rp = env
    sink = {}
    _install_wp_call(monkeypatch, sink=sink, documents=[
        {"id": 11, "doc_type": "invoice", "flat_id": "6", "filename": "inv.txt",
         "content_b64": _b64("Invoice ref ACME-42 for flat 6"), "uploaded_by": "jo",
         "role": "rtm_leaseholder", "note": ""},
    ])
    r = rp.pull_now()
    assert r["ok"] and r["ingested"] == 1 and r["acked"] == 1
    assert sink.get("/documents/ack") == [11]
    from mongoose.services.file_index import search_files
    assert search_files("ACME-42", limit=5)
    from mongoose.services.rtm_docs import list_documents
    docs = list_documents()
    assert docs and docs[0]["role"] == "leaseholder" and docs[0]["source"] == "wordpress"


def test_bad_payload_is_acked_not_looped(env, monkeypatch):
    rp = env
    sink = {}
    _install_wp_call(monkeypatch, sink=sink, documents=[
        {"id": 99, "doc_type": "receipt", "filename": "x", "content_b64": "!!not base64!!"},
    ])
    r = rp.pull_now()
    assert r["ok"] and r["ingested"] == 0 and sink.get("/documents/ack") == [99]


def test_auditor_notes_are_pulled(env, monkeypatch):
    rp = env
    from mongoose.services.rtm_docs import ingest
    doc = ingest(content=b"Invoice. Flat 4.", filename="i.txt", doc_type="invoice", flat_id="4")
    sink = {}
    _install_wp_call(monkeypatch, sink=sink, documents=[], notes=[
        {"id": 5, "doc_ref": doc["id"], "text": "Charge duplicated vs March.",
         "author": "ext", "role": "rtm_auditor"},
    ])
    r = rp.pull_now()
    assert r["notes_added"] == 1 and sink.get("/documents/notes/ack") == [5]
    from mongoose.services.rtm_docs import get_document
    assert any("duplicated" in n["text"] for n in get_document(doc["id"])["notes"])


def test_encrypted_payload_is_decrypted_then_ingested(env, monkeypatch):
    rp = env
    sink = {}
    import mongoose.services.rtm_portal as portal
    # the plugin sends a sealed-box ciphertext; Mongoose decrypts locally with the portal keypair
    monkeypatch.setattr(portal, "decrypt_claim_payload", lambda enc: {"ok": True, "claim": {
        "filename": "sealed.txt", "mime": "text/plain",
        "content_b64": _b64("Sealed receipt ref SEAL-7 for flat 1ECF3"), "note": "via encrypted"}})
    _install_wp_call(monkeypatch, sink=sink, documents=[
        {"id": 21, "doc_type": "receipt", "flat_id": "1ECF3", "encrypted_b64": "CIPHERTEXT"},
    ])
    r = rp.pull_now()
    assert r["ok"] and r["ingested"] == 1 and sink.get("/documents/ack") == [21]
    from mongoose.services.file_index import search_files
    assert search_files("SEAL-7", limit=5), "decrypted content must reach unified search"


def test_undecryptable_payload_is_acked_not_looped(env, monkeypatch):
    rp = env
    sink = {}
    import mongoose.services.rtm_portal as portal
    monkeypatch.setattr(portal, "decrypt_claim_payload", lambda enc: {"ok": False, "error": "decryption failed"})
    _install_wp_call(monkeypatch, sink=sink, documents=[
        {"id": 22, "doc_type": "receipt", "flat_id": "1ECF3", "encrypted_b64": "GARBAGE"},
    ])
    r = rp.pull_now()
    assert r["ok"] and r["ingested"] == 0 and sink.get("/documents/ack") == [22]


def test_role_mapping(env):
    m = env.map_role
    assert m("rtm_director") == "director"
    assert m("administrator") == "admin"
    assert m("subscriber") == "leaseholder"
    assert m("rtm_auditor") == "auditor"
    assert m("something_unknown") == "leaseholder"


def test_role_mapping_contract_is_locked(env):
    """The plugin->RTM role seam has no registry, so the contract must be pinned: every role slug the
    plugin can emit must map to a role Mongoose actually knows, and every mapping target must be a real
    ROLE (so a rename on either side fails here instead of silently downgrading everyone)."""
    from mongoose.services.rtm_docs import ROLES
    valid = set(ROLES)
    # The four short RTM roles the plugin now stamps directly (pc_rtm_short_role), plus the raw rtm_*
    # slugs and the WordPress fallbacks it historically sent.
    plugin_emits = [
        "director", "admin", "auditor", "leaseholder",
        "rtm_director", "rtm_admin", "rtm_auditor", "rtm_leaseholder",
        "administrator", "subscriber",
    ]
    for slug in plugin_emits:
        assert env.map_role(slug) in valid, f"{slug} maps outside ROLES"
    # Nothing in the map can point at a role Mongoose does not recognise.
    for target in env._WP_ROLE_MAP.values():
        assert target in valid, f"map target {target} is not a real ROLE"
    # Unknown/empty always degrades to the least-privileged role, never crashes.
    assert env.map_role("") == "leaseholder"
    assert env.map_role("president_of_the_moon") == "leaseholder"


def test_highest_account_role_picks_strongest(env, tmp_path, monkeypatch):
    """A flat's reported role is the strongest among its linked accounts; no accounts -> "" (not
    onboarded); a role-less account normalises to leaseholder, never crashes."""
    from mongoose.services.rtm_members import _highest_account_role
    assert _highest_account_role([]) == ""
    assert _highest_account_role([{"username": "a", "role": "leaseholder"},
                                  {"username": "b", "role": "director"}]) == "director"
    assert _highest_account_role([{"username": "a", "role": "rtm_admin"},
                                  {"username": "b", "role": "director"}]) == "admin"
    assert _highest_account_role([{"username": "a"}]) == "leaseholder"
