"""Trusted-payee bank details registry (invoice-redirection fraud guard).

The risk: a supplier invoice's bank details are presented for payment with no way to tell whether they
are genuine or altered. This registry keeps each payee's details, records the first as 'seen', lets the
owner confirm by phone, and diffs every later invoice: a match to confirmed details is safe; details that DIFFER
from a confirmed record are the strongest redirection-fraud signal. Read/propose only, never moves money.
"""
from mongoose.services import rtm_management as M


def test_extract_bank_details_from_both_layouts():
    for text in (
        "Business Account Name: Lift Inspection Ltd\nAccount Number: 12345678\nSort Code: 01-02-03",
        "Account Name: Lift Inspection Ltd Account Number: 12345678 Sort Code: 01-02-03",
    ):
        bd = M.extract_bank_details(text)
        assert bd["sort_code"] == "01-02-03"
        assert bd["account_number"] == "12345678"
        assert bd["account_name"] == "Lift Inspection Ltd"


def test_payee_key_unifies_email_domain_and_name_variants():
    assert M._payee_key("accounts@liftinspect.example") == "liftinspect.example"
    assert M._payee_key("liftinspect.example") == "liftinspect.example"
    # a bare company name keys to a stable slug (no domain available)
    assert M._payee_key("ISE UK Ltd") == M._payee_key("ise uk limited")


def test_registry_lifecycle_new_then_confirmed_then_changed():
    M._ensure_tables()
    sc, ac = "01-02-03", "12345678"
    # first invoice from a new payee -> unverified
    assert M.check_invoice_bank_details("accounts@liftinspect.example", sc, ac)["verdict"] == "new_payee"
    # auto-ingest records it as 'seen'
    M.record_payee_bank("accounts@liftinspect.example", sc, ac, account_name="Lift Inspection Ltd",
                        source="invoice INV-0033")
    assert M.check_invoice_bank_details("liftinspect.example", sc, ac)["verdict"] == "match_unconfirmed"
    # M confirms by phone
    M.record_payee_bank("accounts@liftinspect.example", sc, ac, confirmed=True, actor="owner")
    assert M.check_invoice_bank_details("accounts@liftinspect.example", sc, ac)["verdict"] == "match_confirmed"
    # a later invoice with DIFFERENT details for the same payee = redirection-fraud signal
    changed = M.check_invoice_bank_details("liftinspect.example", "09-01-02", "11223344")
    assert changed["verdict"] == "changed"
    assert changed["on_file"][0]["sort_code"] == sc and changed["on_file"][0]["account_number"] == ac


def test_assess_auto_flags_task_once_and_on_fraud():
    import importlib.util
    if importlib.util.find_spec("mongoose.services.calendar_tasks") is None:
        return _assert_core_only_notifies()
    from mongoose.db import connect
    M._ensure_tables()
    inv = "Account Number: 12345678\nSort Code: 01-02-03"
    M.assess_invoice_bank_details(inv, "accounts@liftinspect.example")  # new_payee -> one task
    M.assess_invoice_bank_details(inv, "accounts@liftinspect.example")  # match_unconfirmed -> no new task (dedup/no-op)
    with connect() as con:
        n = con.execute("SELECT COUNT(*) FROM tasks WHERE source='rtm_payee_guard'").fetchone()[0]
    assert n == 1
    # confirm, then a redirected invoice for the same payee -> a high-priority fraud task
    M.record_payee_bank("accounts@liftinspect.example", "01-02-03", "12345678", confirmed=True)
    M.assess_invoice_bank_details("Account Number: 11223344\nSort Code: 09-01-02", "accounts@liftinspect.example")
    M.assess_invoice_bank_details("Account Number: 11223344\nSort Code: 09-01-02", "accounts@liftinspect.example")  # dedup
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT priority, title FROM tasks WHERE source='rtm_payee_guard' ORDER BY priority").fetchall()]
    assert len(rows) == 2  # the confirm task + one fraud task, both deduped
    assert any(r["priority"] == "high" and "fraud" in r["title"].lower() for r in rows)


def test_confirm_never_downgrades_and_rejects_malformed():
    M._ensure_tables()
    M.record_payee_bank("acme@x.com", "11-22-33", "99887766", confirmed=True)
    # a subsequent 'seen' touch of the same record must not downgrade it
    M.record_payee_bank("acme@x.com", "11-22-33", "99887766", confirmed=False)
    assert M.check_invoice_bank_details("x.com", "11-22-33", "99887766")["verdict"] == "match_confirmed"
    # malformed sort/account are rejected, not stored
    assert M.record_payee_bank("y.com", "1-2-3", "123")["ok"] is False


def _assert_core_only_notifies():
    """Core alone has no task list: the payee guard must still raise a visible high-urgency notification."""
    from mongoose.db import connect
    M._ensure_tables()
    M.record_payee_bank("accounts@liftinspect.example", "01-02-03", "12345678", confirmed=True)
    M.assess_invoice_bank_details("Account Number: 11223344\nSort Code: 09-01-02", "accounts@liftinspect.example")
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT title, urgency FROM notifications WHERE source='rtm_payee_guard'").fetchall()]
    assert any("SUSPECTED invoice fraud" in r["title"] and r["urgency"] == "high" for r in rows), rows
