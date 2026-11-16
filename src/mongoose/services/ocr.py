"""Local OCR for image documents, via Apple's Vision framework.

RTM evidence is often a photo: a photographed contract, a receipt snapshot, a screenshot of a
complaint. Those carry no extractable text, so before OCR they were searchable only by filename.
This runs the Mac's built-in Vision text recogniser (VNRecognizeTextRequest, accurate mode) entirely
on-device: no cloud, no API key, no data leaving the Mac, which is the only acceptable path for RTM
household data. A tiny Swift helper is compiled once to a cached binary; each OCR call is then ~1s.

Everything here is best-effort: if Swift is unavailable or a run fails, the caller still stores and
indexes the document, just without OCR text. It never raises.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

# Image types worth OCR-ing. HEIC/HEIF included: iPhone photos land as HEIC.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".gif", ".bmp", ".webp"}
PDF_EXTS = {".pdf"}

# The Swift helper: OCR an image OR a (scanned) PDF with Vision accurate text recognition. For a PDF
# it renders each page with PDFKit and OCRs the rendered bitmap, so a photographed/scanned PDF (no
# text layer) still yields text. Prints the recognised lines.
_SWIFT_SRC = r'''import Foundation
import Vision
import AppKit
import PDFKit

func ocrCGImage(_ cg: CGImage) -> [String] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    try? handler.perform([request])
    var lines: [String] = []
    for obs in (request.results ?? []) {
        if let top = obs.topCandidates(1).first { lines.append(top.string) }
    }
    return lines
}

let args = CommandLine.arguments
guard args.count >= 2 else { exit(2) }
let path = args[1]
var out: [String] = []
if path.lowercased().hasSuffix(".pdf") {
    guard let doc = PDFDocument(url: URL(fileURLWithPath: path)) else { exit(3) }
    for i in 0..<doc.pageCount {
        guard let page = doc.page(at: i) else { continue }
        let rect = page.bounds(for: .mediaBox)
        let scale: CGFloat = 2.0
        let w = Int(rect.width * scale), h = Int(rect.height * scale)
        guard w > 0, h > 0,
              let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8,
                                  bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { continue }
        ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
        ctx.scaleBy(x: scale, y: scale)
        page.draw(with: .mediaBox, to: ctx)
        if let cg = ctx.makeImage() { out.append(contentsOf: ocrCGImage(cg)) }
    }
} else {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { exit(3) }
    out = ocrCGImage(cg)
}
print(out.joined(separator: "\n"))
'''
_SRC_HASH = hashlib.sha256(_SWIFT_SRC.encode()).hexdigest()[:12]


def is_image(filename: str) -> bool:
    return os.path.splitext(filename or "")[1].lower() in IMAGE_EXTS


def is_pdf(filename: str) -> bool:
    return os.path.splitext(filename or "")[1].lower() in PDF_EXTS


def ocr_file(path: str, timeout: int = 180) -> str:
    """OCR an image or a scanned PDF (the Swift helper dispatches on the .pdf suffix). Longer default
    timeout than ocr_image because a multi-page PDF renders and recognises each page."""
    return ocr_image(path, timeout=timeout)


def _ocr_dir() -> Path:
    base = os.getenv("MONGOOSE_DATA_DIR", "").strip()
    root = Path(base) if base else Path(os.path.expanduser("~/.mongoose/data"))
    d = root / "ocr"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tool_path(name: str) -> str | None:
    """Resolve swift/swiftc without depending on the launchd PATH. /usr/bin holds the CLT toolchain."""
    from shutil import which
    p = which(name)
    if p:
        return p
    cand = f"/usr/bin/{name}"
    return cand if os.path.exists(cand) else None


def ocr_available() -> bool:
    return _tool_path("swiftc") is not None or _tool_path("swift") is not None


def _ensure_binary() -> str | None:
    """Compile the Swift helper to a cached binary keyed by source hash. Returns the binary path, or
    None if compilation is unavailable. Recompiles only when the embedded source changes."""
    d = _ocr_dir()
    binp = d / "vision_ocr"
    stamp = d / "vision_ocr.hash"
    try:
        if binp.exists() and stamp.exists() and stamp.read_text().strip() == _SRC_HASH:
            return str(binp)
    except Exception:
        pass
    swiftc = _tool_path("swiftc")
    if not swiftc:
        return None
    src = d / "vision_ocr.swift"
    try:
        src.write_text(_SWIFT_SRC)
        proc = subprocess.run([swiftc, "-O", str(src), "-o", str(binp)],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not binp.exists():
            return None
        stamp.write_text(_SRC_HASH)
        return str(binp)
    except Exception:
        return None


def ocr_image(path: str, timeout: int = 90) -> str:
    """Return recognised text from an image file, or '' on any failure. Runs the cached Vision binary
    if it compiled; otherwise falls back to interpreting the Swift source (slower but no build step)."""
    try:
        if not os.path.exists(path):
            return ""
        binp = _ensure_binary()
        if binp:
            cmd = [binp, path]
        else:
            swift = _tool_path("swift")
            if not swift:
                return ""
            src = _ocr_dir() / "vision_ocr.swift"
            if not src.exists():
                src.write_text(_SWIFT_SRC)
            cmd = [swift, str(src), path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            return ""
        return (proc.stdout or "").strip()
    except Exception:
        return ""
