"""map_expenditure must create exactly ONE payable per bank line, even when called concurrently or twice in
one sync batch. Regression for the 2026-09 D&O double-count (one £128.93 payment shown as £257.86)."""
import csv
import threading


def _expenditure_txn(M, tmp_path):
    M.seed_flats()
    p = tmp_path / "m.csv"
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Details", "Transaction Type", "In", "Out", "Balance"])
        w.writerow(["2026-08-14", "Acme Insurance Ltd", "Outward Faster Payment", "", "128.93", "0.00"])
    M.reconcile_metro_csv(p, apply=True)
    from mongoose.db import connect
    with connect() as con:
        r = con.execute("SELECT id FROM rtm_mgmt_bank_transactions "
                        "WHERE match_status='expenditure' AND amount_pence=12893").fetchone()
    return r["id"]


def _count(schedule, line):
    from mongoose.db import connect
    with connect() as con:
        return con.execute("SELECT COUNT(*) c FROM rtm_mgmt_payables WHERE schedule=? AND budget_line=?",
                           (schedule, line)).fetchone()["c"]


def test_map_expenditure_sequential_reruns_are_noops(tmp_path):
    from mongoose.services import rtm_management as M
    bid = _expenditure_txn(M, tmp_path)
    M.map_expenditure(bid, "sch1", "Insurance")
    M.map_expenditure(bid, "sch1", "Insurance")
    M.map_expenditure(bid, "sch1", "Insurance")
    assert _count("sch1", "Insurance") == 1


def test_map_expenditure_concurrent_creates_one_payable(tmp_path):
    from mongoose.services import rtm_management as M
    bid = _expenditure_txn(M, tmp_path)
    out = []
    def call():
        try:
            out.append(M.map_expenditure(bid, "sch1", "Insurance"))
        except Exception as e:  # a transient SQLite-busy is acceptable; the invariant is >=1 wins, no dupes
            out.append(e)
    ts = [threading.Thread(target=call) for _ in range(4)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert _count("sch1", "Insurance") == 1, f"expected exactly 1 payable, got {_count('sch1','Insurance')}"
