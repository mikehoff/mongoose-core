#!/usr/bin/env python3
"""Leak gate: refuse to publish personal data.

Three layers, all must pass:

1. PRIVATE TERMS. A term list kept OUTSIDE this repo (real names, emails, phone numbers, bank details,
   title numbers, addresses). Path from MONGOOSE_LEAK_TERMS, default ~/.mongoose/rtm-config/leak_terms.txt.
   Any hit fails. The list itself is never committed, so the gate cannot leak what it guards.

2. PATTERNS. Real-looking identifiers that no term list will know about in advance: email addresses
   outside reserved example domains, UK phone numbers outside the Ofcom drama ranges, sort code plus
   account number pairs, HM Land Registry title numbers, API keys and tokens, and runtime files
   (.env, SQLite brains, logs) that must never be tracked. Word, Excel and PowerPoint files are read as
   text too, since templates ship inside the package. Images cannot be read: review any logo or picture
   by eye before release.

3. PERSONAL SIGNALS. A line that talks about a person AND touches body, health, mind, family or routine
   ("he trains at...", "the owner is anxious when...", "M's mum") is the kind of leak a word list cannot
   judge. Each such line must be reviewed once by a human and recorded in .leakgate-allow (path plus a
   hash of the line), so new ones fail until someone looks. Generic feature text passes once allowed.

Usage:
  python tools/leak_gate.py                 # scan every tracked file (git ls-files)
  python tools/leak_gate.py --staged        # scan only staged changes (pre-commit)
  python tools/leak_gate.py --allow-current # record every current personal-signal line as reviewed
Exit code 0 = clean, 1 = leak found.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOW_FILE = ROOT / ".leakgate-allow"
DEFAULT_TERMS = Path.home() / ".mongoose" / "rtm-config" / "leak_terms.txt"

FORBIDDEN_FILES = re.compile(r"(^|/)(\.env(\..+)?|.*\.sqlite3(-wal|-shm)?|.*\.log|leak_terms\.txt)$")
ALLOWED_ENV = {".env.example"}

SAFE_EMAIL_DOMAIN = re.compile(
    r"@(?:[\w-]+\.)*(?:example\.(?:com|org|net)|example|example-[a-z-]+\.(?:com|org|net)|test|invalid|localhost|local|"
    r"c\.us|g\.us|s\.whatsapp\.net|users\.noreply\.github\.com|noreply\.anthropic\.com)$", re.I)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
UK_PHONE = re.compile(r"(?<!\d)(?:\+44\s?|0)7\d{3}\s?\d{3}\s?\d{3}(?!\d)")
DRAMA_PHONE = re.compile(r"7700\s?900|7\d{3}\s?(?:000\s?000|111\s?111|999\s?999|123\s?456)")
SORT_ACCOUNT = re.compile(r"\b\d{2}-\d{2}-\d{2}\b.{0,40}?\b\d{8}\b")
SAFE_SORT = re.compile(r"\b(?:00-00-00|01-02-03|04-05-06|09-01-02|11-22-33|12-34-56)\b")
TITLE_NUMBER = re.compile(r"\b(?:HP|HS|SY|WT|DT|HD|BK|ON)\d{5,6}\b")
SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{30,}|"
                    r"xox[abp]-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
# Fixtures that deliberately exercise the secret detectors use obviously fake bodies.
FAKE_SECRET = re.compile(r"(x{6,}|0{8,}|fake|dummy|example|test)", re.I)

PERSON = re.compile(r"\b(?:M|M's|Alex|owner(?:'s)?|he|his|him|she|her|user's|my|I'm|I am)\b")
PERSONAL = re.compile(
    r"\b(?:gym|workout|training (?:split|session|plan)|squat|deadlift|bench press|protein|calorie|kcal|"
    r"diet|body ?fat|bmi|weighs?|kg\b|sleep(?:s|ing)? (?:at|until|badly|poorly)|insomnia|"
    r"diagnos\w*|adhd|autis\w*|anxiety|anxious|depress\w*|therap\w*|psychiatr\w*|medicat\w*|"
    r"prescri\w*|cardiolog\w*|blood (?:test|pressure)|hospital|clinic|surgery|"
    r"wife|husband|girlfriend|boyfriend|partner's|mum|mom|dad|mother|father|sister|brother|"
    r"daughter|son|grandm\w*|grandf\w*|birthday|salary|debt|mortgage)\b", re.I)

TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".js", ".php", ".html", ".css", ".sh", ".command",
                 ".toml", ".yml", ".yaml", ".cfg", ".ini", ".example", ".csv", ".eml", ".plist", ".xml"}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout


def _files(staged: bool) -> list[str]:
    if staged:
        out = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    else:
        out = _git("ls-files")
    return [f for f in out.splitlines() if f]


def _load_terms() -> list[str]:
    path = Path(os.environ.get("MONGOOSE_LEAK_TERMS", DEFAULT_TERMS)).expanduser()
    if not path.exists():
        return []
    return [t.strip() for t in path.read_text(encoding="utf-8").splitlines() if t.strip() and not t.startswith("#")]


def _office_text(path: Path) -> str:
    """Visible text and metadata of an Office file (a zip of XML), one paragraph per line."""
    import zipfile
    out = []
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.endswith(".xml"):
                    xml = z.read(name).decode("utf-8", "ignore")
                    xml = re.sub(r"</w:p>|</a:p>|<w:br/>", "\n", xml)
                    out.append(re.sub(r"<[^>]+>", "", xml))
    except (zipfile.BadZipFile, OSError):
        return ""
    return "\n".join(out)


def _line_key(path: str, line: str) -> str:
    return path + ":" + hashlib.sha256(line.strip().encode("utf-8")).hexdigest()[:16]


def _load_allow() -> set[str]:
    if not ALLOW_FILE.exists():
        return set()
    return {l.strip() for l in ALLOW_FILE.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")}


def scan(files: list[str], terms: list[str], allow: set[str]) -> tuple[list[str], list[tuple[str, str]]]:
    # "value:<x>" lines in .leakgate-allow exempt one reviewed identifier (a fixture fake like a@b.com,
    # or a public service address) everywhere; anything not listed still fails.
    ok_values = {a[6:].lower() for a in allow if a.startswith("value:")}
    problems: list[str] = []
    signals: list[tuple[str, str]] = []
    # Whole-word match, so a short private name never fires inside an ordinary word that merely starts with it.
    term_re = (re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
                          + r")(?![A-Za-z0-9])", re.I) if terms else None)
    self_path = "tools/leak_gate.py"
    for f in files:
        if FORBIDDEN_FILES.search(f) and Path(f).name not in ALLOWED_ENV:
            problems.append(f"{f}: runtime/secret file must not be tracked")
            continue
        p = ROOT / f
        if p.is_file() and p.suffix.lower() in (".docx", ".dotx", ".xlsx", ".pptx"):
            text = _office_text(p)  # templates ship inside the package: their text must be clean too
        elif not p.is_file() or (p.suffix.lower() not in TEXT_SUFFIXES and p.suffix):
            continue
        else:
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
        for n, line in enumerate(text.splitlines(), 1):
            where = f"{f}:{n}"
            if term_re:
                m = term_re.search(line)
                # A reviewed line may legitimately share a word with a private term (a public figure with
                # the same surname); the per-line allow key covers that one exact line only.
                if m and _line_key(f, line) not in allow:
                    problems.append(f"{where}: private term")  # never echo the term itself
            if f == self_path:
                continue
            for e in EMAIL.findall(line):
                if not SAFE_EMAIL_DOMAIN.search(e) and e.lower() not in ok_values:
                    problems.append(f"{where}: email address {e}")
            for ph in UK_PHONE.findall(line):
                if not DRAMA_PHONE.search(ph) and ph.replace(" ", "") not in ok_values:
                    problems.append(f"{where}: phone number {ph}")
            m = SORT_ACCOUNT.search(line)
            if m and not SAFE_SORT.search(m.group(0)):
                problems.append(f"{where}: sort code and account number")
            if TITLE_NUMBER.search(line):
                problems.append(f"{where}: land registry title number")
            s = SECRET.search(line)
            if s and not FAKE_SECRET.search(s.group(0)):
                problems.append(f"{where}: secret or private key")
            if PERSONAL.search(line) and PERSON.search(line):
                if _line_key(f, line) not in allow:
                    signals.append((f, line))
    return problems, signals


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--allow-current", action="store_true")
    a = ap.parse_args()
    terms = _load_terms()
    files = _files(a.staged)
    problems, signals = scan(files, terms, _load_allow())
    if a.allow_current:
        keys = sorted(_load_allow() | {_line_key(f, l) for f, l in signals})
        ALLOW_FILE.write_text("# Reviewed personal-signal lines (path:sha256-16 of the stripped line).\n"
                              "# Add only after a human has read the line and confirmed it is generic.\n"
                              + "\n".join(keys) + "\n", encoding="utf-8")
        print(f"recorded {len(signals)} reviewed lines in {ALLOW_FILE.name}")
        signals = []
    if not terms:
        print("WARNING: private term list not found; layer 1 skipped "
              f"(set MONGOOSE_LEAK_TERMS or create {DEFAULT_TERMS})", file=sys.stderr)
    for p in problems:
        print("LEAK  " + p)
    for f, l in signals:
        print(f"REVIEW {f}: {l.strip()[:140]}")
    if problems or signals:
        print(f"\nleak gate FAILED: {len(problems)} leak(s), {len(signals)} unreviewed personal line(s). "
              "Fix leaks; for a generic line, review it and run --allow-current.")
        return 1
    print(f"leak gate passed: {len(files)} files, {len(terms)} private terms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
