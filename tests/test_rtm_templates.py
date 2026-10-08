"""Unified RTM templating: the branded pack is the single source of truth and cannot silently drift.

Locks in:
  1. Every merge tag in each live .docx is declared in FIELDS, and every declared scalar is actually in
     the .docx: a swapped or edited template that adds/loses a field fails here, not in front of a
     leaseholder.
  2. Branding is single-source: no stale gmail address, the canonical contact is info@example-court.org.
  3. Cover emails render by substitution, and a blank recipient is a DRAFT (never auto-sent).
  4. validate_context catches missing/empty/unknown fields and malformed loops.
  5. The context builders map a real rtm_trust receipt row onto the receipt template correctly.
  6. A real .docx actually renders (docxtpl) with no unresolved tags left behind.
"""
import re
import zipfile

import pytest

from mongoose.services import rtm_templates as T

_TAG = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")
_LOOP = re.compile(r"\{%(?:tr|p)\s+for\s+\w+\s+in\s+([a-zA-Z0-9_]+)\s*%\}")


def _docx_tags(name: str):
    path = T._DOCX_DIR / T.FIELDS[name]["file"]
    xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8", "replace")
    tags = set(_TAG.findall(xml))
    loops = set(_LOOP.findall(xml))
    # loop-internal tags (item.amount, para) are not top-level scalars
    scalars = {t for t in tags if "." not in t and t != "para"}
    return scalars, loops


@pytest.mark.parametrize("name", list(T.FIELDS))
def test_docx_tags_match_the_field_contract(name):
    scalars, loops = _docx_tags(name)
    declared_scalars = set(T.FIELDS[name]["scalars"])
    declared_loops = set(T.FIELDS[name]["loops"])
    assert scalars == declared_scalars, (
        f"{name}: docx scalar tags != FIELDS. only-in-docx={scalars - declared_scalars}, "
        f"only-in-FIELDS={declared_scalars - scalars}")
    assert loops == declared_loops, f"{name}: loop vars {loops} != declared {declared_loops}"


def test_branding_is_single_source_and_current():
    assert T.BRANDING["email"] == "info@example-court.org"
    # no branding VALUE (or shipped email template) may carry the old gmail address; a doc comment
    # explaining the migration is fine, so check the data, not the source text.
    assert not any("gmail" in str(v).lower() for v in T.BRANDING.values())
    # The LPE1 stage 2 email is the owner's approved wording, which signs off with name and title only (the
    # company address is on the attached letter), so it carries no address line. It must still never carry
    # the old gmail address.
    no_address_line = {"cover_email_lpe1_stage2"}
    for name, spec in T.EMAIL_TEMPLATES.items():
        body = (T._EMAIL_DIR / spec["file"]).read_text(encoding="utf-8").lower()
        assert "gmail" not in body
        if name not in no_address_line:
            assert "info@example-court.org" in body


def test_premises_and_address_helpers():
    assert T.premises("3EC", 9) == "Flat 9, 3 Example Court, Anytown EX1 2YN"
    assert T.premises("1EC", 3) == "Flat 3, 1 Example Court, Anytown EX1 1AB"
    assert T.split_address("3 Sample Lane, Anytown EX2 2BB") == ("3 Sample Lane", "Anytown EX2 2BB")
    assert T.first_name_from("Wendy Whitfield") == "Wendy"
    assert T.first_name_from("Brett and Karen Jones") == "Brett and Karen Jones"


def test_cover_email_renders_and_blank_recipient_is_a_draft():
    ctx = T.receipt_email_context(
        {"leaseholder": "Wendy Whitfield", "amount": "£1,080.21", "received_date": "3 July 2026"},
        recipient_email="", flat_number=9, road="3 Example Court")
    r = T.render_email("cover_email_receipt", ctx)
    assert r["ok"] and r["ready_to_send"] is False           # no email address => draft
    assert "[recipient_email]" in r["text"] and "£1,080.21" in r["text"] and "Dear Wendy" in r["text"]
    # with a real address it is send-ready and carries no unfilled brackets
    ctx["recipient_email"] = "susan@example.com"
    r2 = T.render_email("cover_email_receipt", ctx)
    assert r2["ready_to_send"] is True and "[" not in r2["text"]


def test_validate_context_flags_missing_empty_unknown_and_loops():
    v = T.validate_context("payment_receipt", {"receipt_no": "RCPT-26-3ECF9", "leaseholder_name": ""})
    assert "issue_date" in v["missing"] and "leaseholder_name" in v["empty"] and v["ok"] is False
    v2 = T.validate_context("branded_letter", {
        "letter_date": "x", "leaseholder_name": "x", "addr_line_1": "x", "addr_line_2": "x",
        "greeting_name": "x", "sign_off": "x", "paragraphs": "not a list", "typo": 1})
    assert "typo" in v2["unknown"] and any("paragraphs" in p for p in v2["loop_problems"])


def test_receipt_context_maps_a_real_receipt_row():
    receipt = {  # shape of rtm_trust.build_receipt output
        "receipt_number": "RCPT-26-3ECF9-26-TO-DEC/3ECF9R0", "flat_id": "3ECF9",
        "document_number": "26-TO-DEC/3ECF9R0", "payment_ref": "RAW", "amount": "£1,080.21",
        "received_date": "3 July 2026", "issue_date": "3 July 2026", "method": "Bank transfer",
        "period": "the half-year ending 31 December 2026", "leaseholder": "Wendy Whitfield",
        "address": "3 Sample Lane, Anytown EX2 2BB",
        "premises": "Flat 9, 3 Example Court, Anytown EX1 2YN", "outstanding": "£0.00"}
    ctx = T.receipt_context(receipt)
    assert T.validate_context("payment_receipt", ctx)["ok"], T.validate_context("payment_receipt", ctx)
    assert ctx["amount_received"] == "£1,080.21" and ctx["doc_number"] == "26-TO-DEC/3ECF9R0"
    assert ctx["addr_line_1"] == "3 Sample Lane" and ctx["outstanding"] == "£0.00"


def test_download_token_mint_resolve_and_expiry(tmp_path, monkeypatch):
    f = tmp_path / "doc.docx"
    f.write_bytes(b"PK\x03\x04 fake docx")
    tok = T.mint_download(f, "Receipt_3ECF9.docx", ttl_s=3600)
    got = T.resolve_download(tok)
    assert got and got["filename"] == "Receipt_3ECF9.docx" and got["path"] == str(f)
    assert T.resolve_download("nonsense-token") is None
    # expired token resolves to None
    tok2 = T.mint_download(f, "x.docx", ttl_s=0)
    import time as _t
    _t.sleep(0.01)
    assert T.resolve_download(tok2) is None
    # a token whose file has vanished resolves to None
    f.unlink()
    assert T.resolve_download(tok) is None


def test_generate_document_reports_no_receipt_cleanly(monkeypatch):
    # with no receipts, generate must fail honestly (not crash, not fake a doc)
    import mongoose.services.rtm_trust as RT
    monkeypatch.setattr(RT, "list_receipts", lambda flat_id=None: [])
    r = T.generate_document("payment_receipt", flat_id="3ECF9")
    assert r["ok"] is False and r["reason"] == "no_receipt"


def test_chat_handler_asks_for_flat_and_routes():
    from mongoose.services.rtm_document_chat import maybe_handle_rtm_document, _parse_flat
    assert _parse_flat("receipt for 3ECF9") == "3ECF9"
    # no flat and no known name => a clarifying question, not a crash
    r = maybe_handle_rtm_document("generate a receipt")
    assert r and "which flat" in r["reply"].lower() and r["source"] == "rtm_document"


def test_docx_actually_renders_with_no_unresolved_tags(tmp_path):
    if not T.docx_available():
        pytest.skip("docxtpl not installed")
    receipt = {
        "receipt_number": "RCPT-26-3ECF9", "document_number": "26-TO-DEC/3ECF9R0", "payment_ref": "RAW",
        "amount": "£1,080.21", "received_date": "3 July 2026", "issue_date": "3 July 2026",
        "method": "Bank transfer", "period": "the half-year ending 31 December 2026",
        "leaseholder": "Wendy Whitfield", "address": "3 Sample Lane, Anytown EX2 2BB",
        "premises": "Flat 9, 3 Example Court, Anytown EX1 2YN", "outstanding": "£0.00"}
    out = tmp_path / "receipt.docx"
    r = T.render_docx("payment_receipt", T.receipt_context(receipt), out_path=out)
    assert r["ok"], r
    xml = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8", "replace")
    assert "{{" not in xml and "{%" not in xml, "rendered docx still has unresolved template tags"
    assert "Wendy Whitfield" in xml and "1,080.21" in xml
