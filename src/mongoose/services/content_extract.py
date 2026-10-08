from __future__ import annotations

from pathlib import Path
from typing import Optional

# Content extraction for the file and attachment indexes. Turns the bytes of a
# PDF / Word doc / text file into searchable plain text, so search covers what is
# WRITTEN INSIDE a file, not just its name.
#
# Design rules (from the cross-device research):
#  - Every extractor is OPTIONAL and best-effort. If a library isn't installed or
#    a file won't parse, we return None and the caller still indexes the filename.
#    Nothing here may raise into the indexer.
#  - PDF uses pypdfium2 (Apache/BSD licensed, fast) rather than PyMuPDF (AGPL), so
#    there is no copyleft obligation on this project.
#  - OCR (scanned PDFs / images) is a SEAM: extract_pdf returns little/no text for
#    a scanned PDF, and ocr_image is a hook a macOS caller can fill with Apple
#    Vision (via ocrmac/ocrit). Not wired by default because it's macOS-only.

TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".py", ".js",
                 ".ts", ".html", ".css", ".rst", ".log", ".tsv", ".ini", ".toml"}
PDF_SUFFIXES = {".pdf"}
DOCX_SUFFIXES = {".docx"}

# A page yielding fewer than this many characters is treated as scanned/image,
# so the OCR seam (if wired) should handle it.
_SCANNED_PAGE_CHAR_THRESHOLD = 20

_MAX_CHARS = 200_000  # cap stored text so one huge file can't bloat the index


def _cap(text: str) -> str:
    text = (text or "").strip()
    return text[:_MAX_CHARS]


def extractable(filename: str) -> bool:
    """True if we have any extractor for this file's type."""
    suffix = Path(filename).suffix.lower()
    return suffix in TEXT_SUFFIXES or suffix in PDF_SUFFIXES or suffix in DOCX_SUFFIXES


def extract_text(path: str | Path) -> Optional[str]:
    """Extract searchable text from a file on disk by type. Returns None if the
    type is unsupported, a needed library is missing, or extraction fails."""
    p = Path(path)
    suffix = p.suffix.lower()
    try:
        if suffix in TEXT_SUFFIXES:
            return _cap(p.read_text(encoding="utf-8", errors="replace"))
        if suffix in PDF_SUFFIXES:
            return extract_pdf(p)
        if suffix in DOCX_SUFFIXES:
            return extract_docx(p)
    except Exception:
        return None
    return None


def extract_pdf(path: str | Path) -> Optional[str]:
    """Extract text from a PDF using pypdfium2 (Apache/BSD). Returns None if the
    library is missing or extraction fails. A near-empty result means the PDF is
    likely scanned, the caller may route it to OCR (see ocr_pdf_pages seam)."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception:
        return None
    try:
        parts: list[str] = []
        for i in range(len(pdf)):
            try:
                page = pdf[i]
                textpage = page.get_textpage()
                parts.append(textpage.get_text_range() or "")
                textpage.close()
                page.close()
            except Exception:
                continue
        text = "\n".join(parts).strip()
        return _cap(text) if text else None
    finally:
        try:
            pdf.close()
        except Exception:
            pass


def extract_docx(path: str | Path) -> Optional[str]:
    """Extract text from a .docx using python-docx. Returns None if the library is
    missing or extraction fails. Pulls paragraph text and table cell text."""
    try:
        import docx  # python-docx
    except ImportError:
        return None
    try:
        document = docx.Document(str(path))
    except Exception:
        return None
    parts: list[str] = [para.text for para in document.paragraphs if para.text]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text:
                    parts.append(cell.text)
    text = "\n".join(parts).strip()
    return _cap(text) if text else None


def looks_scanned(text: Optional[str], page_count: int = 1) -> bool:
    """Heuristic: a PDF that yielded almost no text per page is probably scanned,
    so it needs OCR rather than text extraction."""
    if not text:
        return True
    return len(text) < _SCANNED_PAGE_CHAR_THRESHOLD * max(page_count, 1)


def ocr_image(path: str | Path) -> Optional[str]:
    """OCR seam. Not wired by default: OCR is macOS-specific (Apple Vision via
    ocrmac/ocrit) or needs Tesseract. A macOS caller can monkeypatch or replace
    this to enable OCR of scanned PDFs and images. Returns None here so the index
    still records the filename."""
    return None
