"""A document's total is confirmed, not guessed: by its own arithmetic, by the payment it is filed against,
or by the owner. What cannot be confirmed is marked, never presented as fact.

Real layouts from 2026-09-23 that each defeated a single reading rule: a column-extracted order page with
"TOTAL SAVINGS £1.24" as the only same-line total; an Amazon summary with € misread for £ and a promotion
making the plain Total larger than the Grand Total; an AliExpress page of two orders; a PDF holding two
Amazon invoices; a solar certificate whose "Total Installed Capacity (kW): 3.74" is not money.
"""
from decimal import Decimal

from mongoose.services import doc_totals as T

STAIRS = """Subtotal • 5 items
Order discount
• WELCOME
Shipping
Total
Including £0.00 in taxes
• TOTAL SAVINGS £1.24
£24.90
£24.90
-£1.24
£16.99
GBP £40.65
Mastercard • 9864 (
£40.65 GBP • 23 Sept"""

AMAZON = """Order Summary
Items) Subtotal:
Postage & Packing:
Total before VAT:
Estimated VAT:
Total:
Promotion Applied:
Grand Total:
Delivered 20 September
£2.81
KIBBEH 3mm Stainless Steel Security Chain
£10.99
€3.60
Back to top
£26.50
£1.66
£28.16
€5.63
£33.79
-£1.99
€31.80"""

ALIEXPRESS = """£ 5.45
£ 1 coupon if delayed
Total for 1 items: £ 5.12
£ 6.82
£ 1 coupon if delayed
Total for 1 items: £ 6.82"""

TWO_INVOICES = """Invoice total £6.29
Invoice # GB6AJWTF9AEUD
Total payable £6.29
Total £5.24 £1.05
Invoice total £11.98
Invoice # GB6AJWTFDAEUD
Total payable £11.98
Total £9.98 £2.00"""


def _t(text, **kw):
    r = T.resolve_total(text, **kw)
    return T.format_total(r), r["confidence"]


def test_column_order_page_is_proved_by_its_own_amounts():
    assert _t(STAIRS) == ("£40.65", "verified")


def test_amazon_summary_adds_up_to_the_grand_total():
    total, conf = _t(AMAZON)
    assert (total, conf) == ("£31.80", "verified")
    assert "£26.50 subtotal" in T.compute_total(AMAZON)["basis"]


def test_several_orders_sum():
    assert _t(ALIEXPRESS)[0] == "£11.94"


def test_several_invoices_in_one_file_sum():
    assert _t(TWO_INVOICES) == ("£18.27", "verified")


def test_vat_invoice_arithmetic():
    assert _t("Subtotal 96.97\nTotal VAT 20% 19.39\nTotal 116.36\nAmount due £116.36") == ("£116.36", "verified")


def test_measurement_is_not_money():
    text = "Estimated Annual Generation (kWh): 2887.00\nTotal Installed Capacity (kW): 3.74"
    assert T.compute_total(text)["total"] is None


def test_whole_pounds_are_read():
    assert _t("£50\nSUB-TOTAL") == ("£50.00", "unverified")


def test_quantity_times_price_proves_it():
    assert _t("£15.16 (4 × £3.79)") == ("£15.16", "verified")


def test_unproven_reading_is_not_called_verified():
    assert _t("Invoice\nTotal 21.33") == ("£21.33", "likely")


def test_matching_payment_verifies():
    assert _t("Invoice\nTotal 21.33", ledger_pence=2133) == ("£21.33", "verified")


def test_disagreeing_payment_is_flagged():
    total, conf = _t("Invoice\nTotal 21.33", ledger_pence=2500)
    assert (total, conf) == ("£21.33", "conflict")


def test_shared_payment_is_not_checked_alone():
    assert _t("Invoice\nTotal 21.33", ledger_pence=11850, ledger_shared=True) == ("£21.33", "likely")


def test_owner_figure_is_final():
    assert _t(STAIRS, override="41.00") == ("£41.00", "confirmed")


def test_money_parse_is_exact():
    assert T.compute_total("£0.10\n£0.20\n£0.30")["total"] == Decimal("0.30")


# -- the card controls and the shared-payment check, through the real document store --

def _doc(text, doc_type="invoice"):
    from mongoose.db import connect, utc_now_iso
    from mongoose.services.rtm_docs import list_documents
    list_documents(limit=1)
    with connect() as con:
        cur = con.execute("INSERT INTO rtm_documents(doc_type,filename,stored_path,extracted_text,created_at) "
                          "VALUES(?,?,?,?,?)", (doc_type, "d.pdf", "/x/d.pdf", text, utc_now_iso()))
        con.commit()
        return cur.lastrowid


def _facts(did):
    from mongoose.services.rtm_docs import list_documents
    return next(d for d in list_documents(limit=500) if d["id"] == did)["key_facts"]


def test_owner_can_set_and_clear_a_total():
    from mongoose.services.rtm_docs import set_document_total
    did = _doc("£50\nSUB-TOTAL")
    assert _facts(did)["total_confidence"] == "unverified"
    assert set_document_total(did, "£50.00")["ok"]
    assert (_facts(did)["total"], _facts(did)["total_confidence"]) == ("£50.00", "confirmed")
    assert set_document_total(did, "")["ok"]
    assert _facts(did)["total_confidence"] == "unverified"
    assert not set_document_total(did, "abc")["ok"] and not set_document_total(did, "0")["ok"]


def test_owner_can_edit_the_description():
    from mongoose.services.rtm_docs import list_documents, set_document_description
    did = _doc("x")
    assert set_document_description(did, "  cctv   signs ")["description"] == "cctv signs"
    assert next(d for d in list_documents(limit=500) if d["id"] == did)["description"] == "cctv signs"


def test_documents_sharing_one_payment_are_checked_together():
    from mongoose.services import rtm_management as M
    M._ensure_tables()
    a, b = _doc("Invoice\nTotal 12.99"), _doc("Invoice\nTotal 21.86")
    ok = M.record_manual_expenditure(amount="34.85", bank_date="2026-09-23", supplier="Amazon",
                                     description="two orders", document_ids=[a, b])
    assert ok["ok"]
    assert _facts(a)["total_confidence"] == "verified" and _facts(b)["total_confidence"] == "verified"
    c, d = _doc("Invoice\nTotal 10.00"), _doc("Invoice\nTotal 5.00")
    M.record_manual_expenditure(amount="20.00", bank_date="2026-09-23", supplier="Shop",
                                description="mismatch", document_ids=[c, d])
    assert _facts(c)["total_confidence"] == "conflict"


def test_guesses_are_hidden_on_contracts_but_shown_on_invoices():
    lease = _doc("Premium £140,000 payable on completion, ground rent £250", doc_type="other_evidence")
    bill = _doc("Paid £12.50 thanks", doc_type="receipt")
    assert "total" not in _facts(lease)
    assert _facts(bill)["total"] == "£12.50"
