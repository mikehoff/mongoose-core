"""The RTM Ops surface: the service-charge engine's write layer, now drivable from the cockpit.

Bank-truth invariant: nothing is marked paid without an imported bank record; allocation reduces a
demand's outstanding; a receipt only issues at zero and its PDF is downloadable via a gated route."""
from __future__ import annotations
import pytest

import warnings

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient

import mongoose.core_app as A


def _client():
    return TestClient(A.app), {"x-cockpit-nonce": A._COCKPIT_NONCE}


def _seed(tmp_path):
    from mongoose.services import rtm_management as M
    M.create_demand(document_number="DM-T-001", flat="1ECF2", period="2026-H2",
                    amount="100.00", due_date="2026-07-01")
    p = tmp_path / "metro.csv"
    p.write_text("Date,Description,Reference,Amount\n01/07/2026,FPS CREDIT TEST,DM-T-001,100.00\n")
    return p


def test_ops_routes_are_gated():
    c = TestClient(A.app)
    for method, path in [("get", "/rtm/manage/bank-transactions"), ("post", "/rtm/manage/metro-import"),
                         ("post", "/rtm/manage/allocate"), ("post", "/rtm/receipt/build"),
                         ("get", "/rtm/receipt/download"), ("post", "/rtm/books/transaction"),
                         ("post", "/rtm/reconcile/weekly")]:
        r = c.get(path) if method == "get" else c.post(path, json={})
        assert r.status_code in (401, 403), f"{path} not gated ({r.status_code})"


def test_bank_first_flow_import_allocate_receipt_pdf(tmp_path):
    pytest.importorskip("docxtpl", reason="branded receipt PDFs need docxtpl (the rtm_docs extra)")
    c, H = _client()
    csvp = _seed(tmp_path)
    imp = c.post("/rtm/manage/metro-import", json={"path": str(csvp)}, headers=H).json()
    assert imp.get("imported") == 1
    txs = c.get("/rtm/manage/bank-transactions", headers=H).json()["transactions"]
    tx = txs[0]
    assert tx["unallocated_pence"] == 10000
    al = c.post("/rtm/manage/allocate", headers=H,
                json={"bank_transaction_id": tx["id"],
                      "allocations": [{"document_number": "DM-T-001", "amount": "100.00"}]}).json()
    assert al.get("ok", True) is not False
    r = c.post("/rtm/receipt/build", headers=H,
               json={"flat": "1ECF2", "document_number": "DM-T-001", "payment_ref": "DM-T-001",
                     "amount": "100.00", "received_date": "2026-07-01"}).json()
    assert r.get("ok") and r.get("receipt_number")
    dl = c.get("/rtm/receipt/download?flat=1ECF2&document=DM-T-001", headers=H)
    assert dl.status_code == 200 and dl.content[:4] == b"%PDF"
    st = c.get("/rtm/manage/statement/1ECF2", headers=H).json()
    assert st.get("balance") in ("\u00a30.00", "0.00", "£0.00")


def test_receipt_refused_before_allocation(tmp_path):
    c, H = _client()
    from mongoose.services import rtm_management as M
    M.create_demand(document_number="DM-T-002", flat="1ECF3", period="2026-H2",
                    amount="80.00", due_date="2026-07-01")
    r = c.post("/rtm/receipt/build", headers=H,
               json={"flat": "1ECF3", "document_number": "DM-T-002", "payment_ref": "x",
                     "amount": "80.00", "received_date": "2026-07-01"}).json()
    assert r.get("ok") is False and r.get("reason") == "demand_not_fully_paid"
    dl = c.get("/rtm/receipt/download?flat=1ECF3&document=DM-T-002", headers=H)
    assert dl.status_code == 404


def test_write_routes_return_clean_errors_not_500s():
    c, H = _client()
    bad = c.post("/rtm/books/transaction", json={"fund": "nonsense", "direction": "in", "amount": "1"}, headers=H)
    assert bad.status_code == 200 and bad.json().get("ok") is False
    bad2 = c.post("/rtm/manage/allocate", json={"bank_transaction_id": "nope", "allocations": []}, headers=H)
    assert bad2.status_code == 200 and bad2.json().get("ok") is False
    bad3 = c.post("/rtm/manage/metro-import", json={"path": "/does/not/exist.csv"}, headers=H)
    assert bad3.status_code == 200 and bad3.json().get("ok") is False
