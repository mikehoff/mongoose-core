from __future__ import annotations

import os
import tempfile


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    for k in [k for k in os.environ if k.startswith("MONGOOSE_METRO")]:
        os.environ.pop(k)
    from mongoose.db import init_db
    init_db()
    return d


def test_playbook_contains_all_research_products_in_order():
    _fresh()
    from mongoose.services.rtm_bank_feed_playbook import provider_ids, provider_playbook
    assert provider_ids() == ["xero", "crunch", "plaid", "pandle", "gocardless", "moneyhub", "yapily", "clearbooks", "truelayer", "finverse"]
    names = [p["name"] for p in provider_playbook()["providers"]]
    assert "Xero direct feed" in names
    assert "Crunch Plus" in names
    assert "GoCardless Bank Account Data" in names


def test_account_inventory_drives_xero_and_reserve_next_actions():
    _fresh()
    from mongoose.services import rtm_bank_feed_playbook as P
    svc = P.upsert_account(nickname="Service Charge", purpose="service_charge", account_type="Community Current Account", online_banking_platform="Business Online Banking")
    res = P.upsert_account(nickname="Reserve", purpose="reserve", account_type="Community Instant Access Deposit Account", online_banking_platform="Business Online Banking")
    assert svc["account_type"] == "community_current"
    assert res["account_type"] == "community_instant_access_deposit"
    actions = P.next_actions()
    titles = "\n".join(a["title"] + " " + a["action"] for a in actions)
    assert "Test Xero" in titles
    assert "Do not waste time on Xero for reserve" in titles


def test_record_provider_test_and_evidence_pack_export():
    d = _fresh()
    from mongoose.services import rtm_bank_feed_playbook as P
    acct = P.upsert_account(purpose="service_charge", account_type="community_current", online_banking_platform="Business Online Banking")
    test = P.record_test(provider="xero", account_id=acct["id"], status="in_progress", connector_path="Metro direct feed", read_only_confirmed=True)
    ev = P.record_evidence(test_id=test["id"], provider="xero", account_id=acct["id"], evidence_type="governance", description="Xero/Metro direct feed authority captured")
    assert ev["ok"] is True
    pack = P.export_pack(os.path.join(d, "pack"))
    assert pack["ok"] is True
    assert any(str(f).endswith("rtm-bank-feed-playbook.md") for f in pack["files"])
# The chat-tool and Cockpit route checks for the bank feed live in mongoose-assistant.
