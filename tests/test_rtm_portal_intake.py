"""RTM portal issue intake — exercises the REAL rtm_intake.intake_email, no mocks.
Catches the 'stub written against an imagined signature' class of bug."""
import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.chdir(d)
    os.environ["MONGOOSE_DATA_DIR"] = d
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    import mongoose.settings as S
    if hasattr(S, "_settings"): S._settings = None
    if hasattr(S.get_settings, "cache_clear"): S.get_settings.cache_clear()
    from mongoose.db import init_db; init_db()


def test_building_issue_creates_intake_case():
    _fresh()
    from mongoose.services.rtm_portal import intake_portal_issue
    r = intake_portal_issue(flat_id="1ECF1", category="building",
                            title="Leak in hallway", description="Water through ceiling",
                            urgency="urgent", portal_reference="PI-001")
    assert r["ok"] is True, f"intake failed: {r.get('error')}"
    assert r["flat_id"] == "1ECF1"
    assert r["intake_result"].get("case_id") or r["intake_result"].get("ok")


def test_payment_issue_creates_no_ledger_entry():
    """The spec's hard non-goal: a payment issue must never create a ledger entry."""
    _fresh()
    from mongoose.services.rtm_portal import intake_portal_issue
    from mongoose.db import connect
    r = intake_portal_issue(flat_id="1ECF2", category="payment", title="I paid",
                            description="bank transfer last week", payment_date="2026-06-18",
                            amount_paid="350.00", reference_used="1ECF2")
    assert r["ok"] is True, f"intake failed: {r.get('error')}"
    with connect() as con:
        tables = [row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in tables:
            if "ledger" in t or "allocation" in t:
                n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                assert n == 0, f"payment issue wrote {n} rows to ledger table {t}"


def test_unknown_category_normalised_never_dropped():
    """A leaseholder message must NEVER be lost on a category technicality. An unrecognised category is
    normalised to 'other' and still creates a case, rather than being rejected and stuck unsynced
    forever (the bug that silently swallowed every portal message)."""
    _fresh()
    from mongoose.services.rtm_portal import intake_portal_issue
    r = intake_portal_issue(flat_id="1ECF3", category="hacking", title="x", description="y")
    assert r["ok"] is True, f"intake failed: {r.get('error')}"
    assert r["category"] == "other"


def test_public_portal_categories_mapped_to_intake_set():
    """The public portal's leaseholder-friendly categories map onto the intake set instead of being
    rejected: maintenance->building, financial->payment, property_sale/question/contact->other."""
    _fresh()
    from mongoose.services.rtm_portal import intake_portal_issue
    expected = {"maintenance": "building", "financial": "payment",
                "property_sale": "other", "question": "other", "contact": "other"}
    for wp_cat, want in expected.items():
        r = intake_portal_issue(flat_id="1ECF1", category=wp_cat, title="t", description="b")
        assert r["ok"] is True, f"{wp_cat} failed: {r.get('error')}"
        assert r["category"] == want, f"{wp_cat} -> {r['category']}, wanted {want}"


def test_intake_email_signature_compatibility():
    """Guard: intake_portal_issue must only pass kwargs that intake_email actually accepts.
    This is the exact bug that shipped: flat_id was passed but not accepted."""
    import inspect
    from mongoose.services.rtm_intake import intake_email
    sig = inspect.signature(intake_email)
    accepted = set(sig.parameters.keys())
    # The kwargs intake_portal_issue passes:
    passed = {"sender", "subject", "body", "source_message_id", "received_at", "kind"}
    unknown = passed - accepted
    assert not unknown, f"intake_portal_issue passes kwargs intake_email rejects: {unknown}"


def test_all_categories_accepted():
    _fresh()
    from mongoose.services.rtm_portal import intake_portal_issue
    for cat in ("building", "payment", "document", "portal", "other"):
        r = intake_portal_issue(flat_id="1ECF1", category=cat,
                                title=f"{cat} test", description="body")
        assert r["ok"] is True, f"category {cat} failed: {r.get('error')}"
