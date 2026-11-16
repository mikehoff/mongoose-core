"""Content search must rank by relevance (bm25), not just recency.

Regression guard for the "never quite delightful" search audit: email/attachment/voicemail/file
search were ordering by date DESC, so the most relevant hit could be buried under newer, weaker ones.
They now order by bm25() with date as a tiebreaker. This test seeds a clearly-most-relevant but
OLDEST email and asserts it ranks first.
"""
from __future__ import annotations


from mongoose.services import email_index as EI
from mongoose.services.email_index import EmailIn


def test_email_search_ranks_relevance_over_recency(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "s.sqlite3"))
    # most relevant (lift invoice all over subject+body) but indexed FIRST (oldest)
    EI.ingest_email(EmailIn(source="test", mailbox="INBOX", sender="otis@example.com",
                   subject="Lift maintenance invoice from Otis",
                   body="Invoice for the lift. Lift engineering inspection. Lift invoice attached.",
                   sent_at="2026-01-01", received_at="2026-01-01"))
    # weak, newer matches indexed AFTER
    EI.ingest_email(EmailIn(source="test", mailbox="INBOX", sender="x@example.com",
                   subject="Re: building update",
                   body="Minor note, the lift was mentioned once in passing.",
                   sent_at="2026-06-01", received_at="2026-06-01"))
    EI.ingest_email(EmailIn(source="test", mailbox="INBOX", sender="news@example.com",
                   subject="Newsletter",
                   body="General news, a lift invoice link buried at the bottom somewhere.",
                   sent_at="2026-06-28", received_at="2026-06-28"))
    results = EI.search_email("lift invoice", limit=10)
    assert results, "search returned nothing"
    # the genuinely most relevant email must be first, despite being the oldest
    assert "Lift maintenance invoice" in results[0]["subject"], \
        f"relevance ranking failed; top result was {results[0]['subject']!r}"
