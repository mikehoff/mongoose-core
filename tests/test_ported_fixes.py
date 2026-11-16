import os
import time

from mongoose.db import init_db, redact_audit_payload
from mongoose.services.file_index import index_files, search_files



def test_changed_file_reindexes_without_crash(tmp_path, monkeypatch):
    db = tmp_path / "t.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    init_db(db)
    folder = tmp_path / "docs"; folder.mkdir()
    p = folder / "note.md"
    p.write_text("alpha original", encoding="utf-8")
    index_files(str(folder))
    time.sleep(0.02)
    p.write_text("beta rewritten", encoding="utf-8")
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 50))
    index_files(str(folder))  # must not raise
    assert len(search_files("beta")) == 1
    assert len(search_files("alpha")) == 0  # stale content removed from FTS


def test_high_privacy_audit_payload_redacted(tmp_path, monkeypatch):
    db = tmp_path / "t.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    init_db(db)
    out = redact_audit_payload({"title": "private medical", "body": "x"}, privacy="high")
    assert out["title"] == "[redacted:privacy]"
    assert out["body"] == "[redacted:privacy]"


def test_low_privacy_keeps_values_but_redacts_secret_keys():
    out = redact_audit_payload({"entity": "light.kitchen", "ha_token": "abc"}, privacy="low")
    assert out["entity"] == "light.kitchen"
    assert out["ha_token"] == "[redacted]"
