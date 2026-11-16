from __future__ import annotations

from decimal import Decimal

def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "mongoose.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    from mongoose.db import init_db
    init_db(db)
    return db


def test_rtm_build_source_lists_recent_builds_without_creating_directory(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    build_dir = tmp_path / "builds" / "plugin"
    assert not build_dir.exists()

    from mongoose.services.rtm_build_source import source_of_truth_status

    first = source_of_truth_status()
    assert first["plugin_state"].get("recent_builds") == []
    assert not build_dir.exists(), "read-only source-of-truth status must not create plugin build dirs"

    build_dir.mkdir(parents=True)
    built = build_dir / "example-court-beta.zip"
    built.write_bytes(b"PK\x03\x04dummy")
    second = source_of_truth_status()
    recent = second["plugin_state"].get("recent_builds")
    assert recent and recent[0]["name"] == "example-court-beta.zip"


def test_rtm_transfer_matching_respects_as_of_date(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    from mongoose.services import rtm_trust as T

    T.ingest_feed_line(account="running", occurred_on="2026-07-02", direction="out",
                       amount=Decimal("300.00"), description="sweep to trust")
    T.ingest_feed_line(account="service_charge_trust", occurred_on="2026-07-04", direction="in",
                       amount=Decimal("300.00"), description="future landing")

    assert T.match_transfers(as_of="2026-07-03") == []
    matched = T.match_transfers(as_of="2026-07-04")
    assert len(matched) == 1
