"""The Flat box must hold a flat, and the owner's description must have its own field.

Found on 2026-09-23: the Cockpit upload form offered only "Flat (optional)", so it was used as the
description box. 13 live documents had text like "cctv signs", "stationery" and "AliExpress order" stored
as their flat, which matched no flat and was never shown as what it was.
"""
from mongoose.db import connect
from mongoose.services import rtm_docs as D


def _ingest(**kw):
    args = dict(content=b"%PDF-1.4 test", filename="x.pdf", doc_type="receipt")
    args.update(kw)
    r = D.ingest(**args)
    assert r["ok"], r
    with connect() as con:
        return con.execute("SELECT flat_id, description FROM rtm_documents WHERE id=?", (r["id"],)).fetchone()


def test_text_in_the_flat_box_becomes_the_description():
    flat, desc = _ingest(flat_id="cctv signs")
    assert flat is None and desc == "cctv signs"


def test_a_real_flat_is_kept_and_normalised():
    flat, desc = _ingest(flat_id="flat 3ecf9")
    assert flat == "3ECF9" and not desc


def test_explicit_description_and_flat_together():
    flat, desc = _ingest(flat_id="1ECF1", description="stationery")
    assert flat == "1ECF1" and desc == "stationery"


def test_description_is_listed():
    _ingest(flat_id="AliExpress order")
    docs = D.list_documents(limit=5)
    assert docs[0]["description"] == "AliExpress order" and not docs[0]["flat_id"]


def test_existing_descriptions_are_moved_out_of_flat_on_upgrade(tmp_path, monkeypatch):
    """A database from before the description column: descriptions in flat_id move across once."""
    import sqlite3
    db = tmp_path / "old.sqlite3"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE rtm_documents (id INTEGER PRIMARY KEY AUTOINCREMENT, doc_type TEXT NOT NULL, "
                "filename TEXT NOT NULL, stored_path TEXT NOT NULL, content_hash TEXT, size_bytes INTEGER, "
                "mime TEXT, flat_id TEXT, uploaded_by TEXT, role TEXT, source TEXT, extracted_text TEXT, "
                "note TEXT, created_at TEXT NOT NULL, summary TEXT, thumb_path TEXT, labels TEXT, analysed_at TEXT)")
    for fid in ("stationery", "1ecf3", None):
        con.execute("INSERT INTO rtm_documents(doc_type,filename,stored_path,flat_id,created_at) "
                    "VALUES('receipt','a','/a',?, '2026-09-01')", (fid,))
    con.commit()
    D._ensure_tables(con)
    D._ensure_tables(con)  # idempotent
    rows = con.execute("SELECT flat_id, description FROM rtm_documents ORDER BY id").fetchall()
    assert rows == [(None, "stationery"), ("1ECF3", None), (None, None)]
