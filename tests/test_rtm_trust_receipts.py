"""Acceptance tests for receipts (Layer 2 section 2; acceptance 7.4, 7.5)."""
from __future__ import annotations

import uuid
from pathlib import Path


from mongoose.services import rtm_trust as T
from mongoose.services import rtm_management as M
from mongoose.db import connect, utc_now_iso


def _seed_flat_and_demand(db_env, *, fully_paid: bool):
    """Create one flat + one demand; optionally fully allocate it so outstanding hits zero."""
    M.upsert_flat(block="3EC", flat_number="5", leaseholder_name="Hollis",
                  correspondence_address="Flat 5, 3 Example Court, Anytown")
    doc = "26-TO-DEC/3ECF5R0"
    M.create_demand(document_number=doc, flat="3ECF5", period="1 Jul 2026 to 31 Dec 2026",
                    amount=79217, issue_date="2026-07-16", due_date="2026-07-30")
    if fully_paid:
        # Insert a bank transaction + an allocation covering the full demand (precondition for a receipt).
        with connect() as con:
            bt_id = "bt_" + uuid.uuid4().hex[:10]
            con.execute(
                "INSERT INTO rtm_mgmt_bank_transactions(id,bank_date,description,reference,amount_pence,direction,"
                "match_status,allocated_pence,raw_json,source,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (bt_id, "2026-06-26", "HOLLIS service charge", "HOLLIS", 7921700, "in",
                 "matched", 7921700, "{}", "test", utc_now_iso(), utc_now_iso()),
            )
            con.execute(
                "INSERT INTO rtm_mgmt_allocations(id,bank_transaction_id,document_number,flat_id,amount_pence,notes,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                ("al_" + uuid.uuid4().hex[:10], bt_id, doc, "3ECF5", 7921700, "test full payment", utc_now_iso()),
            )
    return doc


def test_receipt_only_on_zero_outstanding(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    doc = _seed_flat_and_demand(monkeypatch, fully_paid=False)
    # partial / unpaid demand: no receipt
    r = T.build_receipt("3ECF5", doc, "HOLLIS", 7921700, "2026-06-26")
    assert r["ok"] is False
    assert r["reason"] == "demand_not_fully_paid"


def test_receipt_generated_when_demand_reaches_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    doc = _seed_flat_and_demand(monkeypatch, fully_paid=True)
    r = T.build_receipt("3ECF5", doc, "HOLLIS", 7921700, "2026-06-26")
    assert r["ok"] is True
    # Receipt numbers are scoped to the document now (external audit, additional findings
    # #3): a second paid demand for the same flat used to INSERT OR REPLACE and silently
    # erase an earlier receipt under the shared RCPT-26-{flat} key.
    assert r["receipt_number"] == f"RCPT-26-3ECF5-{doc}"
    assert r["leaseholder"] == "Hollis"
    assert r["amount"] == "£79,217.00"
    assert r["outstanding"] == "£0.00"
    assert r["document_number"] == doc
    # listed back
    rows = T.list_receipts("3ECF5")
    assert any(x["receipt_number"] == f"RCPT-26-3ECF5-{doc}" for x in rows)




def _make_one_page_pdf(path: Path, text: str) -> None:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    c = canvas.Canvas(str(path), pagesize=A4)
    c.drawString(100, 700, text)
    c.showPage()
    c.save()
