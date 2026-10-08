from mongoose.db import init_db
from mongoose.services.email_index import EmailIn, ingest_email, search_email
from mongoose.services.file_index import index_files, search_files


def test_email_fts(tmp_path, monkeypatch):
    db = tmp_path / "test.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    init_db(db)
    ingest_email(EmailIn(subject="Battery order", body="Need AAA batteries and bin bags"))
    results = search_email("batteries")
    assert len(results) == 1
    assert results[0]["subject"] == "Battery order"


def test_file_fts(tmp_path, monkeypatch):
    db = tmp_path / "test.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    init_db(db)
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "note.md").write_text("Mongoose weather cache should know UV index", encoding="utf-8")
    result = index_files(str(folder))
    assert result["indexed"] == 1
    hits = search_files("Mongoose")
    assert len(hits) == 1
