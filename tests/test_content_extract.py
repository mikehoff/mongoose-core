"""Tests for content extraction: search INSIDE files and attachments, not just
by name. The extractors are optional, so these also assert graceful degradation."""
import io
from email.message import EmailMessage

import pytest

from mongoose.db import init_db
from mongoose.services import content_extract as ce


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "data" / "mongoose.sqlite3"
    p.parent.mkdir(parents=True)
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(p))
    init_db(p)
    return p


def _has(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


# --- extractor unit behaviour ------------------------------------------------
def test_extractable_types():
    assert ce.extractable("a.pdf")
    assert ce.extractable("b.docx")
    assert ce.extractable("c.md")
    assert not ce.extractable("d.png")
    assert not ce.extractable("e.zip")


def test_extract_text_file(tmp_path):
    f = tmp_path / "n.md"
    f.write_text("the leaseholder service charge was unreasonable")
    assert "leaseholder" in ce.extract_text(f)


def test_extract_unsupported_returns_none(tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(b"\x89PNG")
    assert ce.extract_text(f) is None


@pytest.mark.skipif(not _has("docx"), reason="python-docx not installed")
def test_extract_docx(tmp_path):
    import docx
    d = docx.Document()
    d.add_paragraph("Tribunal hearing bundle")
    t = d.add_table(rows=1, cols=1)
    t.rows[0].cells[0].text = "Corona Energy standing charge"
    path = tmp_path / "b.docx"
    d.save(str(path))
    text = ce.extract_text(path)
    assert "Tribunal" in text and "Corona Energy" in text


@pytest.mark.skipif(not _has("pypdfium2"), reason="pypdfium2 not installed")
@pytest.mark.skipif(not _has("reportlab"), reason="reportlab needed to build a test pdf")
def test_extract_pdf(tmp_path):
    from reportlab.pdfgen import canvas
    path = tmp_path / "r.pdf"
    c = canvas.Canvas(str(path))
    c.drawString(72, 720, "EICR inspection report with blank inspector name")
    c.save()
    text = ce.extract_text(path)
    assert "EICR" in text


def test_looks_scanned_heuristic():
    assert ce.looks_scanned("", 1) is True
    assert ce.looks_scanned("x", 1) is True            # too little text
    assert ce.looks_scanned("y" * 100, 1) is False     # plenty


def test_ocr_seam_returns_none_by_default(tmp_path):
    # the OCR hook is unwired by default so the index still records the filename
    assert ce.ocr_image(tmp_path / "scan.png") is None


# --- content search in the attachment index ---------------------------------
@pytest.mark.skipif(not (_has("pypdfium2") and _has("reportlab")),
                    reason="need pypdfium2 + reportlab")
def test_attachment_content_is_searchable(db):
    from reportlab.pdfgen import canvas
    from mongoose.services.attachments import (
        extract_attachments_from_message, index_attachment, search_attachments)
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "The Corona Energy standing charge was four times market rate")
    c.save()
    msg = EmailMessage()
    msg["From"] = "legal@lawfirm.example"
    msg["Subject"] = "Bundle"
    msg["Date"] = "Mon, 14 Jun 2026 10:00:00 +0000"
    msg["Message-ID"] = "<x@k.com>"
    msg.set_content("attached")
    msg.add_attachment(buf.getvalue(), maintype="application", subtype="pdf",
                       filename="evidence.pdf")

    for att in extract_attachments_from_message(msg):
        index_attachment(source="imap", source_message_id=msg["Message-ID"],
                         filename=att["filename"], mime_type=att["mime_type"],
                         size_bytes=att["size_bytes"], sender=msg["From"],
                         subject=msg["Subject"], email_date=msg["Date"],
                         content_text=att.get("content_text"))
    # found by a phrase INSIDE the pdf, not its name
    hits = search_attachments("standing charge")
    assert any(h["filename"] == "evidence.pdf" and h["content_indexed"] for h in hits)


# --- file index records filename even when content extraction is empty -------
def test_file_index_records_known_type_with_empty_content(db, tmp_path, monkeypatch):
    # a .docx that fails to parse should still be findable by filename
    from mongoose.services import file_index
    monkeypatch.setattr(file_index, "_read_text", lambda p: None if p.suffix == ".docx" else "x")
    f = tmp_path / "Important_Contract.docx"
    f.write_bytes(b"not really a docx")
    file_index.index_files(str(tmp_path), device="phone")
    # findable by the name even though content was empty
    assert any("Important_Contract" in r["title"] for r in file_index.search_files("Contract"))
