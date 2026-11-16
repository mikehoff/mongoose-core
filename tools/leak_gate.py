#!/usr/bin/env python3
"""Leak gate: refuse to publish personal data.

Three layers, all must pass:

1. PRIVATE TERMS. Two term lists kept OUTSIDE this repo, so the gate cannot leak what it guards:
   a. identity terms (real names, emails, phone numbers, bank details, title numbers, addresses), path
      from MONGOOSE_LEAK_TERMS, default ~/.mongoose/rtm-config/leak_terms.txt;
   b. health and life terms (the owner's own body, food, mood, physio, medication and hobby words, and
      any stand-in words a scrub put in their place), path from MONGOOSE_HEALTH_TERMS, default
      ~/.mongoose/rtm-config/health_terms.txt.
   Any hit fails. Matching ignores case and spelling of separators: spaces, hyphens, underscores, slashes
   and dots are interchangeable or may be left out, so a term "elbow over knee" also catches
   elbow-over-knee, elbow_over_knee and elbowOverKnee. A term ending in * matches any word ending
   (frobnicat* catches frobnication and frobnicated). Swapping one word of a phrase for another is not a
   fix: move the owner's wording into the private profile and ship a neutral default.

2. PATTERNS. Real-looking identifiers that no term list will know about in advance: email addresses
   outside reserved example domains, UK phone numbers outside the Ofcom drama ranges, sort code plus
   account number pairs, HM Land Registry title numbers, API keys and tokens, and runtime files
   (.env, SQLite brains, logs) that must never be tracked. Word, Excel and PowerPoint files are read as
   text too, since templates ship inside the package. Images cannot be read: review any logo or picture
   by eye before release.

3. PERSONAL SIGNALS. A line that talks about a person AND touches body, food, health, mind, family,
   work, hobbies or routine ("he trains at...", "the owner is anxious when...", "M's mum", "his hobbies")
   is the kind of leak a word list cannot judge. Each such line must be reviewed once by a human and
   recorded in .leakgate-allow (path plus a hash of the line), so new ones fail until someone looks.
   Generic feature text passes once allowed.

Usage:
  python tools/leak_gate.py                 # scan every tracked file (git ls-files)
  python tools/leak_gate.py --staged        # scan only staged changes (pre-commit)
  python tools/leak_gate.py --allow-current # record every current personal-signal line as reviewed
  python tools/leak_gate.py --allow-line src/x.py:12  # record one reviewed line that shares a private
                                            # word (never bulk: each is named and read on its own)
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
DEFAULT_HEALTH_TERMS = Path.home() / ".mongoose" / "rtm-config" / "health_terms.txt"

FORBIDDEN_FILES = re.compile(
    r"(^|/)(\.env(\..+)?|.*\.sqlite3(-wal|-shm)?|.*\.log|leak_terms\.txt|health_terms\.txt)$")
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

PERSON = re.compile(r"\b(?:M|M's|Alex|owner(?:'s)?|he|his|him|himself|she|her|user's|my|I'm|I am)\b")
PERSONAL = re.compile(
    r"\b(?:gym|workout|training (?:split|session|plan)|squat|deadlift|bench press|protein|calorie|kcal|"
    r"diet|body ?fat|bmi|weighs?|kg\b|sleep(?:s|ing)? (?:at|until|badly|poorly)|insomnia|"
    r"diagnos\w*|adhd|autis\w*|anxiety|anxious|depress\w*|therap\w*|psychiatr\w*|medicat\w*|"
    r"prescri\w*|cardiolog\w*|blood (?:test|pressure)|hospital|clinic|surgery|"
    r"physio\w*|posture|mobility|rehab\w*|injur\w*|sore(?:ness)?|pain|aches?|stiff(?:ness)?|"
    r"muscles?|joints?|hamstrings?|quads?|delts?|hips?|spine|swim\w*|yoga|"
    r"stomach|gut|nause\w*|digest\w*|allerg\w*|intoleran\w*|"
    r"supplements?|vitamins?|hormones?|dosage|symptoms?|headaches?|"
    r"lonel\w*|grie(?:f|v\w*)|trauma\w*|self[ -]?worth|"
    r"overwhelm\w*|panic\w*|journal(?:s|ing|led)?|"
    r"relationship|dating|divorce|break ?up|hobb(?:y|ies)|day job|employer|"
    r"wife|husband|girlfriend|boyfriend|partner's|mum|mom|dad|mother|father|sister|brother|"
    r"daughter|son|grandm\w*|grandf\w*|birthday|salary|debt|mortgage)\b", re.I)

# Spaces, hyphens, underscores, slashes and dots in a private term may be written any other way or left
# out, so a term cannot slip through as elbow-over-knee, elbow_over_knee or elbowOverKnee.
_TERM_SEP = r"[\W_]*"

# Look-alike letters and invisible characters are folded before the term lists are checked, so a private
# name cannot slip through spelled with Cyrillic letters or with a zero-width space inside it.
_FOLD = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "А": "A", "В": "B",
    "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C",
    "Т": "T", "Х": "X", "І": "I", "Ј": "J", "Ѕ": "S",
    "​": None, "‌": None, "‍": None, "⁠": None, "﻿": None, "­": None,
})

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


def _load_terms(env: str = "MONGOOSE_LEAK_TERMS", default: Path = DEFAULT_TERMS) -> list[str]:
    path = Path(os.environ.get(env, default)).expanduser()
    if not path.exists():
        return []
    return [t.strip() for t in path.read_text(encoding="utf-8").splitlines() if t.strip() and not t.startswith("#")]


def _term_pattern(term: str) -> str:
    """One private term as a regex body: separators are free (see _TERM_SEP), a trailing * takes any word ending."""
    wild = term.endswith("*")
    chunks = [c for c in re.split(r"[\W_]+", term.rstrip("*")) if c]
    if not chunks:
        return ""
    return _TERM_SEP.join(re.escape(c) for c in chunks) + (r"[A-Za-z0-9]*" if wild else "")


def _terms_regex(terms: list[str]) -> re.Pattern[str] | None:
    # Whole-word match, so a short private name never fires inside an ordinary word that merely starts with it.
    pats = sorted({p for p in (_term_pattern(t) for t in terms) if p}, key=len, reverse=True)
    if not pats:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(pats) + r")(?![A-Za-z0-9])", re.I)


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


def scan(files: list[str], terms: list[str], allow: set[str],
         health_terms: list[str] | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    # "value:<x>" lines in .leakgate-allow exempt one reviewed identifier (a fixture fake like a@b.com,
    # or a public service address) everywhere; anything not listed still fails.
    ok_values = {a[6:].lower() for a in allow if a.startswith("value:")}
    problems: list[str] = []
    signals: list[tuple[str, str]] = []
    term_re = _terms_regex(terms)
    health_re = _terms_regex(health_terms or [])
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
            folded = line.translate(_FOLD)
            if term_re:
                m = term_re.search(line) or term_re.search(folded)
                # A reviewed line may legitimately share a word with a private term (a public figure with
                # the same surname); the per-line allow key covers that one exact line only.
                if m and _line_key(f, line) not in allow:
                    problems.append(f"{where}: private term")  # never echo the term itself
            if (health_re and (health_re.search(line) or health_re.search(folded))
                    and _line_key(f, line) not in allow):
                problems.append(f"{where}: private health or life term")  # never echo the term itself
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
    ap.add_argument("--allow-line", action="append", default=[], metavar="FILE:LINE",
                    help="record one reviewed line that shares a word with a private term (repeatable)")
    a = ap.parse_args()
    terms = _load_terms()
    health_terms = _load_terms("MONGOOSE_HEALTH_TERMS", DEFAULT_HEALTH_TERMS)
    if a.allow_line:
        # Private-term hits are never bulk-allowed: each one is named, read and recorded on its own.
        keys = set(_load_allow())
        for spec in a.allow_line:
            f, _, n = spec.rpartition(":")
            lines = (ROOT / f).read_text(encoding="utf-8").splitlines()
            line = lines[int(n) - 1]
            keys.add(_line_key(f, line))
            print(f"recorded {f}:{n} as reviewed")
        ALLOW_FILE.write_text("# Reviewed personal-signal lines (path:sha256-16 of the stripped line).\n"
                              "# Add only after a human has read the line and confirmed it is generic.\n"
                              + "\n".join(sorted(keys)) + "\n", encoding="utf-8")
    files = _files(a.staged)
    problems, signals = scan(files, terms, _load_allow(), health_terms)
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
    if not health_terms:
        print("WARNING: private health term list not found; layer 1b skipped "
              f"(set MONGOOSE_HEALTH_TERMS or create {DEFAULT_HEALTH_TERMS})", file=sys.stderr)
    for p in problems:
        print("LEAK  " + p)
    for f, l in signals:
        print(f"REVIEW {f}: {l.strip()[:140]}")
    if problems or signals:
        print(f"\nleak gate FAILED: {len(problems)} leak(s), {len(signals)} unreviewed personal line(s). "
              "Fix leaks; for a generic line, review it and run --allow-current.")
        return 1
    print(f"leak gate passed: {len(files)} files, {len(terms)} private terms, {len(health_terms)} health terms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
