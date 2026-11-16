"""LPE1 stage 2 templates: the mechanics, on a small synthetic form.

The real assets (the requester's Laserform LPE1, the wording, the enclosures) are the owner's data and live in
the data directory; rtm_lpe1_stage2.acceptance_check() proves on the live machine that they reproduce the
Flat 7 release of 23 September 2026. These tests pin the machinery any release depends on: fill and read back,
flatten with no fields left, overlays, "Page N of M" on every page, enclosure page numbers, placeholders that
refuse to leave holes, the form edition check, and the cover email wording.
"""
import hashlib
import json
import shutil
from pathlib import Path

import pypdf
import pytest

from mongoose.services import rtm_lpe1_stage2 as S

HAS_POPPLER = bool(shutil.which("pdftocairo") or Path("/opt/homebrew/bin/pdftocairo").exists())
HAS_SOFFICE = bool(shutil.which("soffice") or Path("/opt/homebrew/bin/soffice").exists())


def _form(path: Path) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=A4)
    c.drawString(50, 800, "Test form page 1")
    c.acroForm.textfield(name="Seller", x=150, y=760, width=200, height=18, fontSize=10, borderWidth=0)
    c.acroForm.radio(name="Q1", value="Yes", selected=False, x=150, y=720, size=12)
    c.acroForm.radio(name="Q1", value="No", selected=False, x=200, y=720, size=12)
    c.acroForm.radio(name="Q2", value="Yes", selected=False, x=150, y=690, size=12)
    c.acroForm.radio(name="Q2", value="No", selected=False, x=200, y=690, size=12)
    c.showPage()
    c.drawString(50, 800, "Test form page 2")
    c.acroForm.textfield(name="Dated", x=150, y=760, width=200, height=18, fontSize=10, borderWidth=0)
    c.showPage()
    c.save()


def _pdf(path: Path, pages: int, label: str) -> None:
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    for i in range(pages):
        c.drawString(72, 700, f"{label} {i + 1}")
        c.showPage()
    c.save()


@pytest.fixture()
def lpe1(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(data))
    monkeypatch.setattr(S, "template_dir", lambda: data / "rtm_templates" / "lpe1")
    d = data / "rtm_templates" / "lpe1"
    for sub in ("forms", "enclosures", "jobs"):
        (d / sub).mkdir(parents=True)
    _form(d / "forms" / "form.pdf")
    _pdf(d / "enclosures" / "budget.pdf", 1, "Budget page")
    _pdf(d / "enclosures" / "articles.pdf", 3, "Articles page")
    sha = hashlib.sha256((d / "forms" / "form.pdf").read_bytes()).hexdigest()
    files = {
        "manifest.json": {"form": {"file": "forms/form.pdf", "sha256": sha}},
        "profile.json": {"company_number": "00000000", "letterhead_address": "1 Test Road, Testtown",
                         "capacity_short": "Management company"},
        "form_answers.json": {"form": "forms/form.pdf", "text": {"Seller": "{{seller}}", "Dated": "{{release_date_long}}"},
                              "choice": {"Q1": "/Yes"}},
        "form_overlays.json": {"overlays": [{"page": 2, "text": "(overlay note)", "x": 150, "y": 740,
                                             "font": "Helvetica", "size": 7}]},
        "enclosures.json": {"items": [
            {"question": "8.4", "file": "enclosures/budget.pdf", "letter": "the budget", "replies": "Budget"},
            {"question": "8.17", "file": "enclosures/articles.pdf", "letter": "the articles", "replies": "Articles"}]},
        "replies.json": {"title": "Form LPE1: Replies", "property_line": "{{property_short}}. Seller: {{seller}}.",
                         "capacity": {"heading": "Capacity.", "text": "{{company}} answers."},
                         "knowledge": "Held on {{release_date_long}}.",
                         "entries": [{"heading": "Enclosures.", "text": "{{enclosures_replies}}"}],
                         "close": "Signed by {{director}}.", "footer": "{{company}}: replies, Flat {{flat_number}}"},
        "letter.json": {"recipient_lines": ["By email: {{recipient_email}}"], "refs": ["Your ref: {{their_ref}}"],
                        "salutation": "Dear Sirs", "heading": ["{{property_full}}"],
                        "paragraphs": [{"text": "We enclose one PDF ({{pack_pages}} pages): {{enclosures_letter}}."},
                                       {"text": "{{to_follow_text}}", "optional": True}],
                        "close": "Yours faithfully", "signature_lines": ["{{director}}"]},
    }
    for name, body in files.items():
        (d / name).write_text(json.dumps(body))
    job = {"flat_id": "1ECF9", "flat_number": "9", "seller": "Ms Test Seller", "property_short": "Flat 9, Test Road",
           "property_full": "Flat 9, Test Road, Testtown", "recipient_email": "conveyancer@example.com",
           "email_salutation": "Ms Smith", "their_ref": "ABC/1", "release_date": "2026-10-01",
           "rtm_acquired": "2026-07-16", "demand_from": "2026-07-01", "demand_to": "2026-12-31"}
    (d / "jobs" / "job.json").write_text(json.dumps(job))
    return d, job


def test_enclosure_pages_follow_the_form_and_replies(lpe1):
    d, job = lpe1
    ctx = S.build_context(job)
    assert ctx["form_pages"] == 2 and ctx["replies_page"] == 3
    assert ctx["pack_pages"] == 7  # 2 form + 1 replies + 1 budget + 3 articles
    assert ctx["enclosures_letter"] == "the budget (8.4, page 4) and the articles (8.17, pages 5 to 7)"
    assert ctx["enclosures_replies"] == "Budget (8.4), page 4; Articles (8.17), pages 5 to 7."
    assert ctx["release_date_long"] == "1 October 2026"
    assert (ctx["demand_from_dd"], ctx["demand_from_mm"], ctx["demand_from_yyyy"]) == ("01", "07", "2026")


def test_a_missing_value_stops_rendering_and_names_it():
    with pytest.raises(S.TemplateError, match="seller"):
        S.fill("Seller: {{seller}}", {"seller": "  "})


def test_only_the_requesters_form_edition_is_filled(lpe1, tmp_path):
    d, job = lpe1
    m = json.loads((d / "manifest.json").read_text())
    m["form"]["sha256"] = "0" * 64
    (d / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(S.TemplateError, match="edition"):
        S._fill_form(S.build_context(job), tmp_path / "filled.pdf")


def test_form_is_filled_and_read_back(lpe1, tmp_path):
    d, job = lpe1
    out = tmp_path / "filled.pdf"
    r = S._fill_form(S.build_context(job), out)
    assert r == {"fields": 2, "choices": 1}
    f = pypdf.PdfReader(str(out)).get_fields()
    assert f["Seller"]["/V"] == "Ms Test Seller" and f["Q1"]["/V"] == "/Yes"
    assert str(f["Q2"].get("/V") or "/Off") in ("/Off", "")


@pytest.mark.skipif(not HAS_POPPLER, reason="poppler (pdftocairo) not installed")
def test_flattened_form_has_no_fields_and_carries_the_overlay(lpe1, tmp_path):
    d, job = lpe1
    filled, flat, final = tmp_path / "f.pdf", tmp_path / "flat.pdf", tmp_path / "final.pdf"
    S._fill_form(S.build_context(job), filled)
    S._flatten(filled, flat)
    assert not pypdf.PdfReader(str(flat)).get_fields()
    S._overlay(flat, json.loads((d / "form_overlays.json").read_text())["overlays"], final)
    text = [p.extract_text() for p in pypdf.PdfReader(str(final)).pages]
    assert "Ms Test Seller" in text[0] and "(overlay note)" in text[1] and "1 October 2026" in text[1]


def test_every_page_is_stamped_page_n_of_m_right_aligned(tmp_path):
    from reportlab.pdfbase.pdfmetrics import stringWidth
    src, out = tmp_path / "in.pdf", tmp_path / "out.pdf"
    _pdf(src, 3, "p")
    assert S.stamp_pages(src, out) == 3
    import pdfplumber
    with pdfplumber.open(str(out)) as pdf:
        for i, page in enumerate(pdf.pages):
            cs = [c for c in page.chars if c["fontname"] == "Helvetica" and c["size"] == 9]
            label = "".join(c["text"] for c in cs)
            assert label == f"Page {i + 1} of 3"
            assert abs(cs[-1]["x1"] - (float(page.width) - 20)) < 0.2
            assert abs(cs[0]["matrix"][5] - 12) < 0.01
            assert abs((cs[-1]["x1"] - cs[0]["x0"]) - stringWidth(label, "Helvetica", 9)) < 0.2


def test_cover_email_is_the_approved_wording():
    from mongoose.services.rtm_templates import render_email
    r = render_email("cover_email_lpe1_stage2", {"recipient_email": "a@example.com", "email_salutation": "Ms Example",
                                                 "property_short": "Flat 7, 1 Example Road, Anytown",
                                                 "their_ref": "REF/1"})
    assert r["ok"] and r["ready_to_send"]
    body = r["text"]
    assert "Dear Ms Example,\n\nThank you for your client's payment. Please find attached our letter and the completed " \
           "Form LPE1, with our replies and the enclosures, in one PDF.\n\nKind regards," in body
    assert "\u2014" not in body and "\u2013" not in body


@pytest.mark.skipif(not (HAS_POPPLER and HAS_SOFFICE), reason="needs poppler and LibreOffice")
def test_whole_release_renders_signed_by_nobody(lpe1, tmp_path):
    pytest.importorskip("docx", reason="needs python-docx (the content extra)")
    d, job = lpe1
    r = S.render_job("job.json", tmp_path / "out")
    pack = pypdf.PdfReader(r["pack_pdf"])
    assert r["pack_pages"] == len(pack.pages) == 7
    text = [p.extract_text() or "" for p in pack.pages]
    assert "Page 3 of 7" in text[2] and "Enclosures.  Budget (8.4), page 4; Articles (8.17), pages 5 to 7." in \
        " ".join(text[2].split("\n")).replace("  ", "  ")
    assert "Articles page 3" in text[6]
    letter = pypdf.PdfReader(r["letter_pdf"]).pages[0].extract_text()
    assert "We enclose one PDF (7 pages)" in letter
    assert not pypdf.PdfReader(r["letter_pdf"]).pages[0].images  # the director signs; nothing is pasted in
    for t in text + [letter, r["email"]["text"]]:
        assert "\u2014" not in t and "\u2013" not in t
    assert r["to_sign"][0].endswith("page 2")
