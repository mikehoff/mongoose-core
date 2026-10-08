"""documents_for_bank_line correlates a reconciled bank line to its filed document, precisely (a
distinctive amount or a payee token), without false-linking on small round numbers."""
from mongoose.db import connect, utc_now_iso


def _doc(filename, doc_type, text, flat=None):
    from mongoose.services.rtm_docs import list_documents
    list_documents(limit=1)  # ensures the rtm_documents / rtm_doc_links tables exist
    with connect() as con:
        con.execute("INSERT INTO rtm_documents(doc_type,filename,stored_path,content_hash,extracted_text,flat_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?)", (doc_type, filename, "/x/" + filename, filename, text, flat, utc_now_iso()))
        con.commit()


def test_matches_by_distinctive_amount_and_payee(tmp_path):
    from mongoose.services import rtm_management as M
    from mongoose.services.rtm_docs import documents_for_bank_line
    M.seed_flats()
    _doc("Acme Insurance INV9.pdf", "invoice", "Acme Insurance Ltd  Total 128.93")
    _doc("Bob Cleaning.pdf", "invoice", "Bob cleaning services 50.00")
    ev = documents_for_bank_line(None, None, 12893, "Alex Morgan Acme insurance")
    assert any("Acme" in e["filename"] for e in ev)
    assert not any("Bob" in e["filename"] for e in ev)


def test_no_false_positive_on_small_round_amount_with_no_payee(tmp_path):
    from mongoose.services import rtm_management as M
    from mongoose.services.rtm_docs import documents_for_bank_line
    M.seed_flats()
    _doc("Random receipt.pdf", "receipt", "order 50 items, ref 3, and 60 units total 999.00")
    # a £3 line with an empty description must NOT match a doc that merely contains '3'
    assert documents_for_bank_line(None, None, 300, "") == []


def test_matches_payee_token_in_filename(tmp_path):
    from mongoose.services import rtm_management as M
    from mongoose.services.rtm_docs import documents_for_bank_line
    M.seed_flats()
    _doc("ScottishPower supply confirmation.eml", "contract", "welcome business customer direct debit 44 monthly")
    ev = documents_for_bank_line(None, None, 4445, "SCOTTISHPOWER")
    assert any("ScottishPower" in e["filename"] for e in ev)
