"""The RTM leaseholder portal, implemented: sealed-box claim crypto, validation decisions,
snapshot exclusion gate, publish, PDFs and issue intake. WordPress is mocked; the crypto is real."""
from __future__ import annotations

import base64
import json
import warnings

import pytest

warnings.filterwarnings("ignore")

nacl = pytest.importorskip("nacl.public")


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("PC_PORTAL_CLAIM_KEYPAIR_PATH", str(tmp_path / "kp.json"))
    monkeypatch.setenv("RTM_PORTAL_BANK_NAME", "Example Bank")
    monkeypatch.setenv("RTM_PORTAL_ACCOUNT_NAME", "Example Court RTM")
    monkeypatch.setenv("RTM_PORTAL_SORT_CODE", "00-00-00")
    monkeypatch.setenv("RTM_PORTAL_ACCOUNT_NUMBER", "12345678")
    monkeypatch.setenv("WP_URL", "https://examplecourt.example")
    monkeypatch.setenv("WP_USERNAME", "m")
    monkeypatch.setenv("WP_APP_PASSWORD", "x")
    monkeypatch.setenv("RTM_PORTAL_FLAT_EMAILS", json.dumps({"1ECF1": "jordan@example.com"}))


def _seed():
    from mongoose.db import connect
    from mongoose.services import rtm_management as M
    M.create_demand(document_number="DM-P-001", flat="1ECF1", period="2026-H2",
                    amount="350.00", due_date="2026-07-01")
    with connect() as con:
        con.execute("UPDATE rtm_mgmt_flats SET payment_reference='EC1ECF1' WHERE flat_id='1ECF1'")
        con.commit()


def _seal(pub_b64: str, payload: dict) -> str:
    pk = nacl.PublicKey(base64.b64decode(pub_b64))
    return base64.b64encode(nacl.SealedBox(pk).encrypt(json.dumps(payload).encode())).decode()


def test_keypair_roundtrip_and_private_key_stays_local(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from mongoose.services import rtm_portal as P
    kp = P.generate_claim_keypair()
    assert kp["ok"] and kp["algorithm"] == "x25519-xsalsa20-poly1305"
    assert "private_b64" not in kp                       # never returned
    pub = P.get_claim_public_key()
    sealed = _seal(pub["public_key_b64"], {"payment_reference": "R", "document_number": "D",
                                           "name": "N", "email": "e@x"})
    dec = P.decrypt_claim_payload(sealed)
    assert dec["ok"] and dec["claim"]["payment_reference"] == "R"
    # second generate refuses (rotation must be deliberate)
    assert P.generate_claim_keypair()["ok"] is False


def test_claim_decisions_approve_review_reject(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _seed()
    from mongoose.services import rtm_portal as P
    P.generate_claim_keypair()
    pub = P.get_claim_public_key()["public_key_b64"]
    claims = [
        {"id": 1, "encrypted_payload_b64": _seal(pub, {"payment_reference": "EC1ECF1", "document_number": "DM-P-001", "name": "Jordan", "email": "jordan@example.com"})},
        {"id": 2, "encrypted_payload_b64": _seal(pub, {"payment_reference": "EC1ECF1", "document_number": "DM-P-001", "name": "S", "email": "stranger@x.com"})},
        {"id": 3, "encrypted_payload_b64": _seal(pub, {"payment_reference": "WRONG", "document_number": "DM-P-001", "name": "X", "email": "x@x"})},
        {"id": 4, "encrypted_payload_b64": "not-valid-ciphertext"},
    ]
    pushed = []

    def fake_wp(method, path, *, json_body=None, timeout=20.0):
        if path.endswith("/pending-claims"):
            return {"ok": True, "status": 200, "data": {"claims": claims}}
        if "/claim-decision/" in path:
            pushed.append((path.rsplit("/", 1)[-1], json_body["decision"]))
            # rejected users only ever see the fixed failure message
            if json_body["decision"] == "reject":
                assert json_body["failure_message"] == P.CLAIM_FAILURE_MESSAGE
            return {"ok": True, "status": 200, "data": {}}
        return {"ok": True, "status": 200, "data": {}}

    monkeypatch.setattr(P, "_wp_call", fake_wp)
    res = P.process_pending_claims()
    assert (res["approved"], res["director_review"], res["rejected"], res["undecryptable"]) == (1, 1, 1, 1)
    assert res["processed"] == 4 and res["pushed"] == 4
    assert dict(pushed)["4"] == "director_review_required"   # undecryptable routes to a human
    assert dict(pushed)["1"].startswith("approve")
    assert dict(pushed)["3"] == "reject"


def test_snapshot_exclusion_gate_and_no_storage_path(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _seed()
    from mongoose.services import rtm_portal as P
    snap = P.build_portal_snapshot(full=True)
    assert snap["ok"] and "pdf_storage_path" not in json.dumps(snap)
    assert snap["caveat_7day"] == P.CAVEAT_7DAY
    with pytest.raises(AssertionError):
        P._assert_no_excluded_fields({"flats": [{"witness_statement": "x"}]})


def test_pdf_attach_records_and_never_leaks_path(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("PC_PORTAL_PRIVATE_DIR", str(tmp_path / "private"))
    _seed()
    from mongoose.services import rtm_portal as P
    f = tmp_path / "d.pdf"; f.write_bytes(b"%PDF-1.4 demo")
    r = P.attach_demand_pdf(document_number="DM-P-001", path=str(f))
    assert r["ok"] and r["pdf_key"] and r["pdf_sha256"] and "pdf_storage_path" not in r
    assert P.missing_portal_assets()["missing"] == []
    bad = P.attach_demand_pdf(document_number="NOPE", path=str(f))
    assert bad["ok"] is False


def test_payment_issue_intake_never_touches_ledger(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _seed()
    from mongoose.db import connect
    from mongoose.services import rtm_portal as P
    with connect() as con:
        before = con.execute("SELECT COUNT(*) c FROM rtm_mgmt_ledger_entries").fetchone()["c"]
    r = P.intake_portal_issue(flat_id="1ECF1", category="payment", title="I paid",
                              description="paid last week", payment_date="2026-06-25",
                              amount_paid="350.00", reference_used="EC1ECF1")
    assert r["ok"]
    with connect() as con:
        after = con.execute("SELECT COUNT(*) c FROM rtm_mgmt_ledger_entries").fetchone()["c"]
    assert after == before      # record-only: intake case created, ledger untouched
