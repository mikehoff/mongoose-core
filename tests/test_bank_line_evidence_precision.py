"""A bank line's evidence must be ITS document, not every document from the same supplier.

Found on 2026-09-23: filing Watt a Guy invoice INV-0567 (a £116.36 repair, linked to its own payment) made
it appear as evidence on three OLDER Watt a Guy payments (INV0450, INV0469, INV0530). The lookup only ever
guessed, by supplier name and amount, and ignored the link that said which payment the invoice belonged to.

Now: a payment's own linked documents come first; a document linked to a different payment is never
guessed onto another line; and an invoice number in the bank text narrows the guess to that invoice.
"""
from mongoose.db import connect, utc_now_iso


def _doc(filename, doc_type, text):
    from mongoose.services.rtm_docs import list_documents
    list_documents(limit=1)
    with connect() as con:
        cur = con.execute(
            "INSERT INTO rtm_documents(doc_type,filename,stored_path,content_hash,extracted_text,created_at) "
            "VALUES(?,?,?,?,?,?)", (doc_type, filename, "/x/" + filename, filename, text, utc_now_iso()))
        con.commit()
        return cur.lastrowid


def _setup():
    from mongoose.services import rtm_management as M
    M._ensure_tables()
    old = {n: _doc(f"Watt a Guy Invoice INV0{n} .pdf", "invoice", f"Watt A Guy Limited invoice INV-0{n} EML test")
           for n in ("450", "469", "530")}
    new = _doc("Watt a Guy Invoice INV-0567 3 Example light repair.pdf", "invoice",
               "Watt A Guy Limited invoice INV-0567 Total 116.36")
    return M, old, new


def test_linked_document_is_not_guessed_onto_older_payments():
    from mongoose.services.rtm_docs import documents_for_bank_line
    M, old, new = _setup()
    r = M.record_manual_expenditure(amount="116.36", bank_date="2026-09-23", supplier="Watt a Guy Limited",
                                    description="INV-0567 repair", reference="Watt a Guy Limited INV0567",
                                    budget_line="Emergency light testing", document_ids=[new])
    assert r["ok"]
    for n in ("450", "469", "530"):
        ev = [e["doc_id"] for e in documents_for_bank_line("pay_other", None, 5000, f"Watt a Guy Limited INV0{n}")]
        assert new not in ev, f"INV-0567 must not be offered as evidence for INV0{n}"


def test_a_payment_shows_its_own_linked_document_first():
    from mongoose.services.rtm_docs import documents_for_bank_line
    M, old, new = _setup()
    r = M.record_manual_expenditure(amount="116.36", bank_date="2026-09-23", supplier="Watt a Guy Limited",
                                    description="INV-0567 repair", document_ids=[new])
    ev = documents_for_bank_line(r["payable_id"], None, 11636, "Watt a Guy Limited (manual entry)")
    assert [e["doc_id"] for e in ev] == [new]


def test_all_linked_documents_are_shown_not_capped():
    from mongoose.services.rtm_docs import documents_for_bank_line
    M, old, new = _setup()
    docs = [_doc(f"Amazon invoice {i}.pdf", "invoice", f"Amazon order {i}") for i in range(5)]
    r = M.record_manual_expenditure(amount="118.50", bank_date="2026-09-23", supplier="Amazon",
                                    description="Amazon orders", document_ids=docs)
    ev = documents_for_bank_line(r["payable_id"], None, 11850, "Amazon (manual entry)")
    assert sorted(e["doc_id"] for e in ev) == sorted(docs)


def test_invoice_number_in_bank_text_narrows_to_that_invoice():
    from mongoose.services.rtm_docs import documents_for_bank_line
    M, old, new = _setup()
    ev = [e["doc_id"] for e in documents_for_bank_line(None, None, 18299, "Watt a Guy Limited INV0469")]
    assert ev == [old["469"]]


def test_link_is_idempotent_and_follows_the_delete_convention():
    from mongoose.services.rtm_docs import link_document_to_payable
    M, old, new = _setup()
    a = link_document_to_payable(new, "pay_x")
    b = link_document_to_payable(new, "pay_x")
    assert a["ok"] and b.get("already_linked")
    with connect() as con:
        rows = con.execute("SELECT notes, path FROM rtm_mgmt_evidence WHERE target_id='pay_x'").fetchall()
    assert len(rows) == 1 and rows[0][0].endswith(f"rtm_doc={new}") and rows[0][1]


def test_unlinked_supplier_guess_still_works():
    """No links and no invoice number: the old payee/amount guess is unchanged."""
    from mongoose.services.rtm_docs import documents_for_bank_line
    M, old, new = _setup()
    ev = [e["doc_id"] for e in documents_for_bank_line(None, None, 11636, "Watt a Guy Limited")]
    assert ev and ev[0] == new
