"""The total shown on a document card must be the amount actually payable.

Found on 2026-09-23 on real filed documents, with no test covering any of it:
- Stairs Nosing order #1319 (£40.65) showed £1.24: "TOTAL SAVINGS £1.24" was read as the total, because
  the scan extracted as a column of labels then a column of amounts, so "Total" had no amount beside it.
- An Amazon order summary (Grand Total £31.80) showed £28.16, the net "Total before VAT": same column
  layout, the OCR read two £ signs as €, and the amount pattern crossed a line break and lost £33.79.
- An AliExpress screenshot showed £1, from "Total for 1 items: £ 5.12".
The texts below are the real extracted texts, trimmed.
"""
from mongoose.services.rtm_docs import _key_facts

STAIRS = """Stairs Nosing UK
Order #1319
5 Equal & Unequal Edge Protector Light Grey PVC Plastic Corner Angle Trim
£4.98/ea
Subtotal • 5 items
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
Payment
Mastercard • 9864 (
£40.65 GBP • 23 Sept"""

AMAZON = """Order Summary
Payment method
Metro Bank Business Debit Card •... 9864
Order Summary
Items) Subtotal:
Postage & Packing:
Total before VAT:
Estimated VAT:
Total:
Promotion Applied:
Grand Total:
Delivered 20 September
KIBBEH 3mm Stainless Steel Security Chain with Carabiners
£10.99
Back to top
£26.50
£1.66
£28.16
€5.63
£33.79
-£1.99
€31.80"""

ALIEXPRESS = """433.92mhz Rolling Code Garage Door Gate
£ 5.45
£ 1 coupon if delayed
Total for 1 items: £ 5.12
48leds solar street wall lights
£ 6.82
£ 1 coupon if delayed
Total for 1 items: £ 6.82"""


def test_savings_line_is_never_the_total():
    assert _key_facts(STAIRS)["total"] == "£40.65"


def test_column_layout_pairs_grand_total_with_its_amount():
    assert _key_facts(AMAZON)["total"] == "£31.80"


def test_several_orders_on_one_page_total_their_sum():
    """Two AliExpress orders paid in one checkout: £5.12 + £6.82 is £11.94, the amount actually charged.
    Showing only the larger order (£6.82) was wrong."""
    f = _key_facts(ALIEXPRESS)
    assert f["total"] == "£11.94" and f["orders"] == "2"


def test_item_count_is_not_read_as_the_total():
    assert _key_facts("Total for 1 items: £ 5.12")["total"] == "£5.12"


def test_paid_invoice_shows_its_total_not_the_zero_balance():
    t = "Invoice INV-9\nTotal £128.93\nAmount paid £128.93\nAmount due £0.00"
    assert _key_facts(t)["total"] == "£128.93"


def test_gross_beats_net_and_vat_line_is_not_a_total():
    t = "Subtotal 96.97\nTotal VAT 20% 19.39\nTotal 116.36\nAmount due £116.36"
    assert _key_facts(t)["total"] == "£116.36"


def test_payable_label_beats_a_larger_plain_total_after_a_promotion():
    t = "Total: £33.79\nPromotion Applied: -£1.99\nGrand Total: £31.80"
    assert _key_facts(t)["total"] == "£31.80"


def test_amount_pattern_does_not_cross_a_line_break():
    t = "Estimated VAT 5.63\n£33.79"
    assert _key_facts(t)["total"] == "£33.79"


def test_bank_details_still_found():
    t = "Pay via bank transfer Sort code: 04-05-06 Acc: 23456789\nTotal 116.36"
    f = _key_facts(t)
    assert f["sort_code"] == "04-05-06" and f["account"] == "23456789" and f["total"] == "£116.36"
