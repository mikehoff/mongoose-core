"""Money handling must be exact and CONSISTENT across the RTM modules. A previous bug: the bookkeeper
used a magnitude-guessing heuristic that silently read the integer 2100 as £21.00 (pence) while the
management module read it as £2,100.00 (pounds). For FTT-sensitive trust accounting, that 100x
divergence is a serious trust bug. These lock both to the same exact, predictable behaviour."""
from __future__ import annotations

import os
import tempfile

import pytest


def _fresh():
    d = tempfile.mkdtemp()
    os.environ["MONGOOSE_DB_PATH"] = os.path.join(d, "m.sqlite3")
    os.environ["MONGOOSE_DATA_DIR"] = d
    from mongoose.db import init_db
    init_db()


@pytest.mark.parametrize("amount,pence", [
    (500, 50000),
    (1000, 100000),
    (1500, 150000),       # £1,500.00, NOT £15.00 (the old heuristic bug)
    (2100, 210000),       # £2,100.00, NOT £21.00 (the old heuristic bug)
    (250, 25000),
    ("2100", 210000),
    ("17685.24", 1768524),
    ("£1,234.56", 123456),
    (99.99, 9999),
    (0, 0),
])
def test_bookkeeper_and_management_agree(amount, pence):
    _fresh()
    from mongoose.services.rtm_bookkeeper import _money_to_pence as bk
    from mongoose.services.rtm_management import _money_to_pence as mg
    assert bk(amount) == pence, f"bookkeeper misread {amount!r}"
    assert mg(amount) == pence, f"management misread {amount!r}"


def test_integers_are_pounds_not_pence():
    """The core fix: a bare integer is always pounds, regardless of magnitude."""
    _fresh()
    from mongoose.services.rtm_bookkeeper import _money_to_pence
    # values straddling the old >1000 heuristic boundary must all be pounds
    assert _money_to_pence(999) == 99900     # £999, not £9.99
    assert _money_to_pence(1001) == 100100   # £1,001, not unchanged-pence
    assert _money_to_pence(2100) == 210000   # £2,100, not £21


def test_bool_is_rejected_as_money():
    _fresh()
    from mongoose.services.rtm_bookkeeper import _money_to_pence
    with pytest.raises(ValueError):
        _money_to_pence(True)


def test_invalid_money_string_raises():
    _fresh()
    from mongoose.services.rtm_bookkeeper import _money_to_pence
    with pytest.raises(ValueError):
        _money_to_pence("not money")
    with pytest.raises(ValueError):
        _money_to_pence("")


def test_realistic_qlta_amount_is_not_misread():
    """Regression for the real case: the ~£2,100/month Jaguar QLTA charge passed as an int must
    record as £2,100.00, not £21.00."""
    _fresh()
    from mongoose.services import rtm_bookkeeper as B
    B.record_opening_balance("service", 0, "2026-07-16")
    B.record_transaction(fund="service", amount=2100, direction="out",
                         description="Jaguar QLTA monthly", category="major_works")
    s = B.summary()
    # £2,100 out of a £0 opening = -£2,100.00, never -£21.00
    assert "2,100.00" in s["ledger_balances"]["service_charge_trust"]
