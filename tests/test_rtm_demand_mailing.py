"""Tests for the batch payment-now-due demand mailing (services/rtm_demand_mailing.py).

Covers the spec invariants (BATCH_MAILING_SPEC.md 7): correct cohort + exclusions, dash-free cover,
attachment cross-check, dry-run by default, idempotent no-double-send, failure recorded as failed
(retryable), and one recipient per transport call (never BCC). Uses a tiny seeded temp DB and a real
one-page fixture PDF, so nothing touches the live brain or emails a real leaseholder.
"""
from __future__ import annotations

import os
import tempfile

import pytest


def _make_pdf(path: str, amount: str, ref: str) -> None:
    """A minimal one-page PDF whose text carries the amount and payment reference, so the mailing's
    attachment cross-check has something real to read."""
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(path)
    c.drawString(72, 720, "DEMAND FOR SERVICE CHARGE")
    c.drawString(72, 700, f"Total due {amount}")
    c.drawString(72, 680, f"using payment reference {ref}")
    c.showPage()
    c.save()


@pytest.fixture()
def _seeded(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("MONGOOSE_DB_PATH", os.path.join(d, "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", d)
    from mongoose.services import rtm_management as M
    from mongoose.services import rtm_portal as P
    M._ensure_tables()
    P._ensure_portal_tables()
    from mongoose.db import connect

    # Three flats: one payable with an email (the sole recipient), one paid, one with no email.
    flats = [
        # flat_id, block, num, name, ref, email, excluded
        ("1ECF2", "1EC", 2, "Derek Dixon", "BECKER", "alex@example.com", 0),
        ("1ECF6", "1EC", 6, "Ivan Petrov", "PETROV", "petrov@example.com", 0),   # will be Paid
        ("3ECF1", "3EC", 1, "Maria Marsden", "HALE", "", 0),                  # no email
        ("1ECF0", "1EC", 0, "Test Flat", "TEST", "test@example.com", 1),       # excluded test flat
    ]
    with connect() as con:
        for fid, block, num, name, ref, email, excl in flats:
            con.execute(
                "INSERT INTO rtm_mgmt_flats(flat_id,block,flat_number,leaseholder_name,payment_reference,"
                "leaseholder_email,excluded,greeting_name,correspondence_address,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
                (fid, block, num, name, ref, email, excl, name.split()[0], "1 Somewhere Road"))
        con.commit()

    # Demands: Becker outstanding, Petrov paid, Hale outstanding (but no email), test flat 0.
    def _demand(doc, fid, amount_pence, status):
        with connect() as con:
            con.execute(
                "INSERT INTO rtm_mgmt_demands(document_number,flat_id,leaseholder_name,period,issue_date,"
                "due_date,amount_pence,status,revision,schedule,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
                (doc, fid, "", "2026-H2", "2026-07-16", "2026-07-30", amount_pence, status, 0, "sch1"))
    _demand("26-TO-DEC/1ECF2R0", "1ECF2", 79217, "Issued")
    _demand("26-TO-DEC/1ECF6R0", "1ECF6", 107516, "Paid")
    _demand("26-TO-DEC/3ECF1R0", "3ECF1", 107516, "Issued")

    # A real demand PDF for Becker, registered in portal_demand_pdfs.
    pdf_path = os.path.join(d, "becker.pdf")
    _make_pdf(pdf_path, "£792.17", "BECKER")
    with connect() as con:
        con.execute(
            "INSERT INTO portal_demand_pdfs(document_number,pdf_key,pdf_sha256,pdf_storage_path,created_at) "
            "VALUES(?,?,?,?,datetime('now'))",
            ("26-TO-DEC/1ECF2R0", "key-becker", "sha", pdf_path))
        con.commit()

    from mongoose.services import rtm_demand_mailing as DM
    return DM


def test_cohort_is_only_payable_with_email(_seeded):
    DM = _seeded
    c = DM.resolve_cohort()
    ids = [r["flat_id"] for r in c["recipients"]]
    assert ids == ["1ECF2"]                       # Becker only
    reasons = {e["flat_id"]: e["reason"] for e in c["excluded"]}
    assert "paid" in reasons["1ECF6"].lower() or "no outstanding" in reasons["1ECF6"].lower()
    assert "no email" in reasons["3ECF1"].lower()
    assert "test" in reasons["1ECF0"].lower() or "excluded" in reasons["1ECF0"].lower()


def test_cover_is_dash_free_and_carries_the_facts(_seeded):
    DM = _seeded
    c = DM.resolve_cohort()
    cover = DM.render_cover(c["recipients"][0], c["period_end"])
    assert cover["ok"] and cover["ready_to_send"]
    assert cover["dash_free"] is True
    text = cover["text"]
    from mongoose.services.rtm_templates import BRANDING
    # Bank facts come from the company config (real on the live machine, fictional in a checkout), so
    # assert whatever the loaded config carries rather than a hard-coded sort code / account number.
    for token in ["£792.17", "BECKER", BRANDING["bank_sort_code"], BRANDING["bank_account_number"],
                  "30 July 2026", "31 December 2026",
                  "crossed with this email", "also posted to you", "printed, email, or both"]:
        assert token in text, token
    assert not any(ch in text for ch in "\u2012\u2013\u2014\u2015\u2212")


def test_attachment_cross_check_refuses_wrong_amount(_seeded, monkeypatch):
    DM = _seeded
    r = DM.resolve_cohort()["recipients"][0]
    assert DM.verify_attachment(r)["ok"] is True
    bad = dict(r, amount="£999.99")
    v = DM.verify_attachment(bad)
    assert v["ok"] is False and "cross-check" in v["reason"]


def test_dry_run_sends_nothing(_seeded):
    DM = _seeded
    out = DM.send_batch(confirm=False)
    assert out["sent"] == 0 and out["dry_run"] is True
    assert DM.sent_log() == []


def test_confirm_sends_once_then_is_idempotent(_seeded):
    DM = _seeded
    seen = []

    def capture(bundle, period):
        seen.append(bundle["to"])                 # one recipient per call: never BCC
        assert isinstance(bundle["to"], str) and "," not in bundle["to"]
        return {"ok": True, "message_id": "TEST"}

    r1 = DM.send_batch(confirm=True, transport=capture)
    assert r1["sent"] == 1 and r1["failed"] == 0 and len(seen) == 1
    r2 = DM.send_batch(confirm=True, transport=capture)
    assert r2["sent"] == 0 and r2["skipped"] == 1     # already sent
    assert len(seen) == 1                              # transport not called again


def test_failed_send_is_recorded_failed_and_retryable(_seeded):
    DM = _seeded

    def failing(bundle, period):
        return {"ok": False, "error": "simulated"}

    r = DM.send_batch(confirm=True, transport=failing)
    assert r["sent"] == 0 and r["failed"] == 1
    log = DM.sent_log()
    assert len(log) == 1 and log[0]["status"] == "failed"
    assert DM.already_sent("1ECF2", "26-TO-DEC/1ECF2R0", "2026-H2") is False   # retryable
