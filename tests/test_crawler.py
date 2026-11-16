"""Tests for Crawler v1: the backfill/freshness split, resumable file cursor,
cursor-based IMAP freshness (mocked, no live server), load controls, and status."""
import os

import pytest

from mongoose.db import init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


# --- resumable file walk -----------------------------------------------------
def test_resumable_walk_covers_all_in_chunks(db, tmp_path):
    from mongoose.services.file_index import index_files_resumable, search_files
    d = tmp_path / "tree"
    d.mkdir()
    for i in range(7):
        (d / f"file_{i}.md").write_text(f"content number {i}")
    cursor = ""
    scanned_total = 0
    chunks = 0
    while True:
        r = index_files_resumable(str(d), after_path=cursor, limit=3)
        scanned_total += r["scanned"]
        cursor = r["cursor"]
        chunks += 1
        if not r["more"]:
            break
        assert chunks < 10
    assert scanned_total == 7           # every file covered exactly once
    assert chunks == 3                   # 3 + 3 + 1
    assert all(search_files(f"number {i}") for i in range(7))


def test_resumable_walk_cursor_resumes_not_restarts(db, tmp_path):
    from mongoose.services.file_index import index_files_resumable
    d = tmp_path / "tree"
    d.mkdir()
    for i in range(5):
        (d / f"f{i}.md").write_text("x")
    first = index_files_resumable(str(d), after_path="", limit=2)
    second = index_files_resumable(str(d), after_path=first["cursor"], limit=2)
    # second chunk starts strictly after the first's cursor (no overlap)
    assert second["cursor"] > first["cursor"]


# --- crawler load controls ---------------------------------------------------
def test_crawler_respects_item_budget(db, tmp_path, monkeypatch):
    from mongoose.services import crawler as cr
    monkeypatch.setattr(cr, "_disk_ok", lambda: True)  # don't depend on sandbox disk
    monkeypatch.setenv("MONGOOSE_CRAWLER_MAX_FILES_PER_RUN", "2")
    d = tmp_path / "tree"
    d.mkdir()
    for i in range(5):
        (d / f"f{i}.md").write_text("x")
    r1 = cr.crawl_files(str(d), device="mac")
    assert r1["scanned"] == 2 and r1["more_remaining"] is True  # bounded
    # resumes and eventually completes
    runs = 1
    res = r1
    while res["more_remaining"] and runs < 10:
        res = cr.crawl_files(str(d), device="mac")
        runs += 1
    assert not res["more_remaining"]
    assert runs == 3   # 2 + 2 + 1


def test_crawler_skips_on_low_disk(db, tmp_path, monkeypatch):
    from mongoose.services import crawler as cr
    monkeypatch.setattr(cr, "_free_disk_gb", lambda path=".": 0.1)
    monkeypatch.setenv("MONGOOSE_CRAWLER_MIN_FREE_DISK_GB", "5")
    d = tmp_path / "tree"
    d.mkdir()
    (d / "a.md").write_text("x")
    cr.register_source(f"files:{os.path.realpath(str(d))}", "files")
    r = cr.crawl_files(str(d), device="mac")
    # low disk is signalled by status, and the shape stays consistent (scanned present, zeroed)
    assert r.get("status") == "skipped_low_disk"
    assert r.get("scanned") == 0


# --- cursor-based IMAP freshness (mocked) ------------------------------------
def test_imap_incremental_uses_uid_cursor(db, monkeypatch):
    """The efficiency fix: fetch only UIDs greater than the cursor, not the last
    50 every time. Mocked IMAP server, no network."""
    import mongoose.services.email_index as ei

    # a fake mailbox with 3 messages, UIDs 100, 101, 102
    msgs = {
        b"100": b"From: a@x\r\nSubject: first\r\nMessage-ID: <100>\r\n\r\nbody one",
        b"101": b"From: b@x\r\nSubject: second\r\nMessage-ID: <101>\r\n\r\nbody two",
        b"102": b"From: c@x\r\nSubject: third\r\nMessage-ID: <102>\r\n\r\nbody three",
    }

    class FakeIMAP:
        def __init__(self, host, port): self._searched_from = None; self.fetch_specs = []
        def login(self, u, p): return ("OK", [])
        def select(self, mb, readonly=False): return ("OK", [b"3"])   # real imaplib accepts readonly
        def status(self, mb, what): return ("OK", [b"INBOX (UIDVALIDITY 42)"])
        def uid(self, cmd, *args):
            if cmd == "search":
                # args like (None, "UID 101:*") -> return uids >= the low bound
                crit = args[-1]
                low = int(crit.split()[1].split(":")[0])
                hits = [u for u in (b"100", b"101", b"102") if int(u) >= low]
                return ("OK", [b" ".join(hits)])
            if cmd == "fetch":
                uid = args[0]
                self.fetch_specs.append(args[-1])   # record the fetch spec for the PEEK assertion
                return ("OK", [(b"x", msgs[uid])])
            return ("OK", [b""])
        def logout(self): return ("OK", [])

    monkeypatch.setattr(ei.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(ei.imaplib, "IMAP4", FakeIMAP)

    class S:
        imap_host = "x"; imap_username = "u"; imap_password = "p"
        imap_port = 993; imap_use_ssl = True
    monkeypatch.setattr(ei, "get_settings", lambda: S())

    # first pass from cursor 0: gets all three, highest uid 102
    r1 = ei.sync_imap_incremental("INBOX", last_uid=0)
    assert r1["ok"] and r1["count"] == 3 and r1["highest_uid"] == 102

    # second pass from cursor 102: gets nothing new (UID 102:* returns only 102,
    # which is filtered out as not > 102)
    r2 = ei.sync_imap_incremental("INBOX", last_uid=102)
    assert r2["ok"] and r2["count"] == 0 and r2["highest_uid"] == 102


def test_crawl_imap_fresh_persists_cursor(db, monkeypatch):
    from mongoose.services import crawler as cr
    import mongoose.services.email_index as ei
    monkeypatch.setattr(cr, "_disk_ok", lambda: True)  # don't depend on sandbox disk
    calls = {"last_uids": []}

    def fake_incremental(mailbox="INBOX", last_uid=0, max_messages=250):
        calls["last_uids"].append(last_uid)
        # first call: 2 new ending at uid 5; second call: nothing new
        if last_uid == 0:
            return {"ok": True, "count": 2, "highest_uid": 5, "more": False}
        return {"ok": True, "count": 0, "highest_uid": last_uid, "more": False}

    monkeypatch.setattr(ei, "sync_imap_incremental", fake_incremental)

    cr.crawl_imap_fresh("INBOX")
    src = cr.get_source("imap:INBOX")
    assert src["fresh_cursor"] == "5"        # cursor persisted

    cr.crawl_imap_fresh("INBOX")
    # second run started from the persisted cursor, not 0
    assert calls["last_uids"] == [0, 5]


# --- status ------------------------------------------------------------------
def test_crawler_status_reports_progress(db, tmp_path):
    from mongoose.services import crawler as cr
    d = tmp_path / "tree"
    d.mkdir()
    (d / "a.md").write_text("hello")
    cr.crawl_files(str(d), device="mac")
    rows = cr.crawler_status()
    assert rows and any("scanned" in r["summary"] for r in rows)
    assert any(r["kind"] == "files" for r in rows)
