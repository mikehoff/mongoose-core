"""IMAP read-safety , Mongoose must NEVER change the read/unread state of the user's mail.

Two guarantees, tested against a mock IMAP server that records the exact commands issued:
  1. The mailbox is opened read-only (EXAMINE, i.e. select(readonly=True)) so the server refuses any
     flag change.
  2. Message bodies are fetched with BODY.PEEK[] (not RFC822 / BODY[]), which does not set \\Seen.
This is the regression guard for "Mongoose marked my unread emails as read."
"""
from __future__ import annotations

import mongoose.services.email_index as EI


class _FakeIMAP:
    """Records select/fetch commands so the test can assert read-safety. Returns one tiny RFC822 msg."""
    RAW = (b"Message-ID: <x@test>\r\nFrom: a@b.c\r\nTo: d@e.f\r\nSubject: hi\r\n"
           b"Date: Mon, 1 Jan 2026 00:00:00 +0000\r\n\r\nbody\r\n")

    def __init__(self):
        self.selects = []     # list of (mailbox, readonly)
        self.fetches = []     # list of fetch command strings

    def login(self, u, p): return ("OK", [b"ok"])
    def select(self, mailbox, readonly=False):
        self.selects.append((mailbox, readonly))
        return ("OK", [b"1"])
    def search(self, charset, *criteria): return ("OK", [b"1"])
    def uid(self, cmd, *args):
        if cmd == "search":
            return ("OK", [b"1"])
        if cmd == "fetch":
            self.fetches.append(args[-1])             # the fetch spec, e.g. "(BODY.PEEK[])"
            return ("OK", [(b"1 (BODY[] {size}", self.RAW)])
        return ("OK", [b""])
    def fetch(self, msg_id, spec):
        self.fetches.append(spec)
        return ("OK", [(b"1 (BODY[] {size}", self.RAW)])
    def logout(self): return ("OK", [b"bye"])
    def noop(self): return ("OK", [b"ok"])


def _wire(monkeypatch, tmp_path, fake):
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "m.sqlite3"))
    # IMAP config comes from env (settings is a frozen dataclass), set before settings loads.
    monkeypatch.setenv("IMAP_HOST", "imap.test")
    monkeypatch.setenv("IMAP_USERNAME", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    monkeypatch.setenv("IMAP_PORT", "993")
    monkeypatch.setenv("IMAP_USE_SSL", "true")
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db
    init_db()
    monkeypatch.setattr(EI, "_imap_client", lambda klass, host, port: fake)


def test_full_sync_is_readonly_and_peeks(monkeypatch, tmp_path):
    fake = _FakeIMAP()
    _wire(monkeypatch, tmp_path, fake)
    EI.sync_imap("INBOX", limit=10)
    # opened read-only
    assert fake.selects, "select was never called"
    assert all(ro is True for (_mbx, ro) in fake.selects), \
        f"mailbox must be opened readonly (EXAMINE), got {fake.selects}"
    # fetched with PEEK, never plain RFC822/BODY[]
    assert fake.fetches, "no fetch happened"
    for spec in fake.fetches:
        sp = spec.decode() if isinstance(spec, (bytes, bytearray)) else str(spec)
        assert "PEEK" in sp.upper(), f"fetch must use BODY.PEEK[], got {sp!r}"
        assert "(RFC822)" not in sp.upper(), f"fetch must not use RFC822 (marks read), got {sp!r}"


def test_incremental_sync_is_readonly_and_peeks(monkeypatch, tmp_path):
    fake = _FakeIMAP()
    _wire(monkeypatch, tmp_path, fake)
    # find the incremental entry point (UID-based) and run it if present
    fn = getattr(EI, "sync_imap_incremental", None) or getattr(EI, "sync_imap_uid", None)
    if fn is None:
        # discover any public sync function that uses uid fetch
        import inspect
        for name, obj in vars(EI).items():
            if name.startswith("sync") and inspect.isfunction(obj) and name != "sync_imap":
                fn = obj; break
    if fn is None:
        return  # only the full path exists; covered by the other test
    try:
        fn("INBOX")
    except TypeError:
        fn()
    for spec in fake.fetches:
        sp = spec.decode() if isinstance(spec, (bytes, bytearray)) else str(spec)
        assert "PEEK" in sp.upper(), f"incremental fetch must use PEEK, got {sp!r}"
    if fake.selects:
        assert all(ro is True for (_m, ro) in fake.selects)
