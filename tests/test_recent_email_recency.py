"""recent_email must judge recency by the EMAIL's own date, not when Mongoose indexed it,
and must exclude the owner's own outgoing mail. The live bug: stored dates are mixed ISO /
RFC 2822, so a SQL string comparison against an ISO cutoff passed every row, and a mailbox
re-sync made months-old email show in the daily brief as new ('new mail from A Morgan and
Jo Example' when Jo's mail was months old and M is the owner)."""
import datetime
from email.utils import format_datetime

import mongoose.services.email_index as EI


def _fake_connect(rows):
    class Cur:
        def fetchall(self):
            return rows

    class Con:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            return Cur()

    return lambda: Con()


def _row(rid, sender, received_at, indexed_at):
    return {"id": rid, "source": "imap", "mailbox": "INBOX", "sender": sender,
            "subject": "s", "sent_at": None, "received_at": received_at,
            "privacy": "normal", "indexed_at": indexed_at}


def test_recency_uses_email_date_and_excludes_owner(monkeypatch):
    monkeypatch.setattr(EI, "_owner_email_identifiers", lambda: ["owner@example.com"])
    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat()
    rows = [
        # months-old email (RFC 2822) but freshly indexed -> must NOT be "recent"
        _row(1, "Old One <old@x.com>", "Wed, 6 May 2026 16:02:37 +0000", now_iso),
        # genuinely recent email (RFC 2822) -> must appear
        _row(2, "Fresh <fresh@x.com>", format_datetime(now), now_iso),
        # owner's own outgoing mail, recent -> must NOT appear
        _row(3, "Me <owner@example.com>", format_datetime(now), now_iso),
    ]
    monkeypatch.setattr(EI, "connect", _fake_connect(rows))

    senders = [e["sender"] for e in EI.recent_email(hours=24)]
    assert any("fresh@x.com" in s for s in senders)
    assert not any("old@x.com" in s for s in senders)        # old date wins over fresh index
    assert not any("owner@example.com" in s for s in senders)  # owner-sent excluded


def test_parse_email_dt_handles_both_formats():
    assert EI._parse_email_dt("2026-07-06T09:00:00+00:00").year == 2026
    d = EI._parse_email_dt("Wed, 6 May 2026 16:02:37 +0000")
    assert (d.year, d.month, d.day) == (2026, 5, 6)
    assert EI._parse_email_dt("") is None
    assert EI._parse_email_dt(None) is None
    assert EI._parse_email_dt("not a date at all") is None
