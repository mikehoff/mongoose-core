"""Document analysis for RTM documents: thumbnail, image classification, and a short description.

Every ingested RTM document should be legible at a glance. Three cheap, local-first passes give it:
  1. a thumbnail (a ~480px preview), so the Cockpit can show a real image, not a filename;
  2. image-classification labels for photos (Apple Vision VNClassifyImageRequest, on-device), so a
     photographed leak or a receipt snapshot carries machine-readable subject tags; and
  3. a 1 to 3 sentence plain-English description from the local model (with a deterministic fallback),
     so the owner reads what a document IS without opening it.

Everything here mirrors ocr.py: on-device only, no cloud, no API key, no data leaving the Mac. Tool
paths resolve absolutely (/usr/bin/sips, /usr/bin/qlmanage, swiftc) because the launchd PATH is
minimal. Every function is best-effort and NEVER raises: a document that cannot be analysed is still
stored, listed and searchable.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from .ocr import IMAGE_EXTS, _tool_path

# Absolute paths: launchd hands the server a bare PATH, so never rely on `which` for these two.
_SIPS = "/usr/bin/sips"
_QLMANAGE = "/usr/bin/qlmanage"


def _data_root() -> Path:
    base = os.getenv("MONGOOSE_DATA_DIR", "").strip()
    return Path(base) if base else Path(os.path.expanduser("~/.mongoose/data"))


def _thumbs_dir() -> Path:
    d = _data_root() / "rtm_thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _classify_dir() -> Path:
    d = _data_root() / "ocr"  # share the ocr app dir for the compiled Swift helpers
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate_thumbnail(stored_path: str, doc_id: int) -> str | None:
    """Write a ~480px JPEG thumbnail for the document and return its path, or None on any failure.

    Images go straight through sips. PDFs (and anything else with a Quick Look generator) are rendered
    by qlmanage to a PNG, then normalised to JPEG via sips. Never raises."""
    try:
        if not stored_path or not os.path.exists(stored_path):
            return None
        out = _thumbs_dir() / f"{doc_id}.jpg"
        ext = os.path.splitext(stored_path)[1].lower()
        if ext in IMAGE_EXTS:
            if not os.path.exists(_SIPS):
                return None
            proc = subprocess.run([_SIPS, "-Z", "480", stored_path, "--out", str(out)],
                                  capture_output=True, text=True, timeout=60)
            return str(out) if (proc.returncode == 0 and out.exists()) else None
        # PDFs and other Quick Look-able files: render a preview PNG, then convert to JPEG.
        if not os.path.exists(_QLMANAGE):
            return None
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run([_QLMANAGE, "-t", "-s", "480", "-o", tmp, stored_path],
                                  capture_output=True, text=True, timeout=90)
            if proc.returncode != 0:
                return None
            pngs = list(Path(tmp).glob("*.png"))
            if not pngs:
                return None
            src_png = pngs[0]
            if os.path.exists(_SIPS):
                conv = subprocess.run(
                    [_SIPS, "-s", "format", "jpeg", str(src_png), "--out", str(out)],
                    capture_output=True, text=True, timeout=60)
                if conv.returncode == 0 and out.exists():
                    return str(out)
            # sips missing or conversion failed: keep the PNG preview instead of nothing.
            out_png = _thumbs_dir() / f"{doc_id}.png"
            try:
                out_png.write_bytes(src_png.read_bytes())
                return str(out_png)
            except Exception:
                return None
    except Exception:
        return None


# ── image classification via Apple Vision (VNClassifyImageRequest, on-device, no cloud) ──
_CLASSIFY_SRC = r'''import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count >= 2 else { exit(2) }
let path = args[1]
guard let img = NSImage(contentsOfFile: path),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { exit(3) }
let request = VNClassifyImageRequest()
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
try? handler.perform([request])
guard let results = request.results else { exit(0) }
var lines: [String] = []
for obs in results where obs.confidence > 0.1 {
    lines.append("\(obs.identifier)\t\(obs.confidence)")
    if lines.count >= 20 { break }
}
print(lines.joined(separator: "\n"))
'''
_CLASSIFY_HASH = hashlib.sha256(_CLASSIFY_SRC.encode()).hexdigest()[:12]


def _ensure_classify_binary() -> str | None:
    """Compile the Vision classifier helper to a cached binary keyed by source hash. Returns the binary
    path, or None if swiftc is unavailable. Recompiles only when the embedded source changes."""
    d = _classify_dir()
    binp = d / "vision_classify"
    stamp = d / "vision_classify.hash"
    try:
        if binp.exists() and stamp.exists() and stamp.read_text().strip() == _CLASSIFY_HASH:
            return str(binp)
    except Exception:
        pass
    swiftc = _tool_path("swiftc")
    if not swiftc:
        return None
    src = d / "vision_classify.swift"
    try:
        src.write_text(_CLASSIFY_SRC)
        proc = subprocess.run([swiftc, "-O", str(src), "-o", str(binp)],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not binp.exists():
            return None
        stamp.write_text(_CLASSIFY_HASH)
        return str(binp)
    except Exception:
        return None


def classify_image(path: str, timeout: int = 60) -> list[str]:
    """Return up to ~5 Apple Vision classification labels (confidence > ~0.1) for an image, or [] on any
    failure or for a non-image. Runs the cached compiled binary if it built; otherwise interprets the
    Swift source directly. Never raises."""
    try:
        if not path or not os.path.exists(path):
            return []
        if os.path.splitext(path)[1].lower() not in IMAGE_EXTS:
            return []
        binp = _ensure_classify_binary()
        if binp:
            cmd = [binp, path]
        else:
            swift = _tool_path("swift")
            if not swift:
                return []
            src = _classify_dir() / "vision_classify.swift"
            if not src.exists():
                src.write_text(_CLASSIFY_SRC)
            cmd = [swift, str(src), path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            return []
        labels: list[str] = []
        for line in (proc.stdout or "").splitlines():
            parts = line.split("\t")
            ident = parts[0].strip()
            if ident:
                labels.append(ident)
            if len(labels) >= 5:
                break
        return labels
    except Exception:
        return []


def _deterministic_description(doc: dict, labels: list[str]) -> str:
    """A truthful description built only from the fields, used when the local model is unavailable."""
    doc_type = str(doc.get("doc_type") or "document").replace("_", " ")
    filename = str(doc.get("filename") or "").strip()
    flat_id = str(doc.get("flat_id") or "").strip()
    bits = [f"A {doc_type}"]
    if filename:
        bits.append(f"'{filename}'")
    if flat_id:
        bits.append(f"for flat {flat_id}")
    head = " ".join(bits) + "."
    text = (doc.get("extracted_text") or "").strip()
    tail = ""
    if labels:
        tail = " Image labels: " + ", ".join(labels[:5]) + "."
    elif text:
        snippet = " ".join(text.split())[:160]
        if snippet:
            tail = f" Text begins: {snippet}."
    return (head + tail).strip()


def summarise_document(doc: dict, labels: list[str]) -> str:
    """Compose a 1 to 3 sentence plain-English description of a document. Uses the local model with a
    tight prompt; falls back to a deterministic field-based description if the model is unavailable or
    returns nothing. Read-only and honest: it summarises only what is in the fields. Never raises."""
    doc_type = str(doc.get("doc_type") or "document").replace("_", " ")
    filename = str(doc.get("filename") or "").strip()
    flat_id = str(doc.get("flat_id") or "").strip()
    text = (doc.get("extracted_text") or "").strip()[:2000]
    fields = [f"Type: {doc_type}"]
    if filename:
        fields.append(f"Filename: {filename}")
    if flat_id:
        fields.append(f"Flat: {flat_id}")
    if labels:
        fields.append(f"Image labels: {', '.join(labels[:5])}")
    if text:
        fields.append(f"Extracted text:\n{text}")
    user = "\n".join(fields)
    system = ("Describe this document in 1-3 plain sentences: what it is and the key facts. "
              "No preamble.")
    try:
        import asyncio
        from ..llm import ask_ollama
        out = asyncio.run(ask_ollama(user, system=system, role="chat"))
        out = (out or "").strip()
        if out:
            return out
    except Exception:
        pass
    return _deterministic_description(doc, labels)
