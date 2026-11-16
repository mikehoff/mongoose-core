"""Receipts must be keyed to the DEMAND they pay, not just the flat.

Regression guard for a real defect: receipts were stored under `RCPT-<hardcoded 26>-<flat>` as the
PRIMARY KEY and written with INSERT OR REPLACE, so the next period's receipt for a flat had the SAME
key and SILENTLY DESTROYED the previous period's receipt. Nothing tied a receipt to its demand, so
the portal could only guess by flat. These tests fail if that regresses.
"""
import pathlib
import tempfile


from mongoose.services import rtm_portal as RP


def _dummy_pdf() -> str:
    p = pathlib.Path(tempfile.mkdtemp()) / "receipt.pdf"
    p.write_bytes(b"%PDF-1.4 test receipt")
    return str(p)


def test_receipt_number_is_unique_per_period():
    """A receipt number derives from the demand, so two periods cannot produce the same string."""
    h2 = RP._receipt_no_for("26-TO-DEC/1ECF1R0", "1ECF1")
    h1 = RP._receipt_no_for("27-TO-JUN/1ECF1R0", "1ECF1")
    assert h2 != h1, "two periods must not share a receipt number"
    assert "1ECF1" in h2 and "1ECF1" in h1
    # and it must not be the old hardcoded-year form
    assert h1 != "RCPT-26-1ECF1"


def test_second_period_receipt_does_not_overwrite_the_first(tmp_path, monkeypatch):
    """THE bug: issuing next period's receipt used to replace the previous period's row."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    RP._ensure_portal_tables()
    from mongoose.db import connect

    for doc, period in (("26-TO-DEC/1ECF1R0", "H2 2026"), ("27-TO-JUN/1ECF1R0", "H1 2027")):
        RP.attach_receipt_pdf(flat_id="1ECF1", receipt_no=RP._receipt_no_for(doc, "1ECF1"),
                              path=_dummy_pdf(), period=period, amount="£100.00",
                              received_date="2026-07-01", document_number=doc)
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT receipt_no, document_number FROM portal_receipt_pdfs WHERE flat_id='1ECF1'")]
    assert len(rows) == 2, f"both periods' receipts must survive, got {rows}"
    assert {r["document_number"] for r in rows} == {"26-TO-DEC/1ECF1R0", "27-TO-JUN/1ECF1R0"}


def test_reissuing_the_same_demand_replaces_in_place(tmp_path, monkeypatch):
    """Re-issuing one demand's receipt must update it, never leave two receipts for one demand."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    RP._ensure_portal_tables()
    from mongoose.db import connect

    doc = "26-TO-DEC/1ECF2R0"
    for _ in range(3):
        RP.attach_receipt_pdf(flat_id="1ECF2", receipt_no=RP._receipt_no_for(doc, "1ECF2"),
                              path=_dummy_pdf(), period="H2 2026", amount="£100.00",
                              received_date="2026-07-01", document_number=doc)
    with connect() as con:
        n = con.execute("SELECT COUNT(*) FROM portal_receipt_pdfs WHERE document_number=?", (doc,)).fetchone()[0]
    assert n == 1, "one demand must have exactly one receipt row"


def test_send_receipt_refuses_to_guess_when_a_flat_has_several(tmp_path, monkeypatch):
    """Emailing 'the latest' receipt could send the wrong period to a leaseholder: refuse instead."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    RP._ensure_portal_tables()
    for doc in ("26-TO-DEC/1ECF3R0", "27-TO-JUN/1ECF3R0"):
        RP.attach_receipt_pdf(flat_id="1ECF3", receipt_no=RP._receipt_no_for(doc, "1ECF3"),
                              path=_dummy_pdf(), period="p", amount="£1", received_date="2026-07-01",
                              document_number=doc)
    res = RP.send_receipt_email("1ECF3")  # no receipt named
    assert res.get("ok") is False
    assert "name which to send" in (res.get("error") or "")


def test_backfill_never_guesses_an_ambiguous_receipt(tmp_path, monkeypatch):
    """Historical receipts are only stamped when the flat's demand is unambiguous."""
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    RP._ensure_portal_tables()
    from mongoose.db import connect

    # a receipt with no demand at all -> must be reported ambiguous, never invented
    RP.attach_receipt_pdf(flat_id="9ZZF9", receipt_no="RCPT-LEGACY-9ZZF9", path=_dummy_pdf(),
                          period="p", amount="£1", received_date="2026-07-01")
    with connect() as con:
        con.execute("UPDATE portal_receipt_pdfs SET document_number='' WHERE flat_id='9ZZF9'")
        con.commit()
    out = RP.backfill_receipt_document_numbers(apply=True)
    assert out["would_fill"] == 0
    assert any(a["flat_id"] == "9ZZF9" for a in out["ambiguous"])
