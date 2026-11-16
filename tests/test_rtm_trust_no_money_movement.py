"""The RTM trust/accounting layer records and recommends. It must NEVER move money: no transfer
initiation, no payment execution, no refund instruction. This static guard fails loudly if anyone
reintroduces a money-moving capability into the RTM code.

It deliberately matches only real callables (a `def name(` definition or a `.name(` method call), NOT
bare substrings, so honest monitoring/verification vocabulary like "expected transfer", "transfer
status", or "recommendation_only" stays allowed."""
from __future__ import annotations

import re
from pathlib import Path

# Operations that would actually move money or instruct the bank. Recording an OBSERVED refund from the
# feed is fine (that is a record, not an action); initiating/scheduling/executing one is not.
FORBIDDEN_MONEY_MOVERS = [
    "initiate_transfer", "execute_transfer", "schedule_transfer", "confirm_transfer",
    "initiate_payment", "execute_payment", "payment_initiation", "send_payment",
    "bank_write", "push_payment", "make_payment",
    "initiate_refund", "schedule_refund", "recommend_refund", "create_bank_refund",
    "refund_instruction",
]

SRC = Path(__file__).resolve().parent.parent / "src" / "mongoose"


def _rtm_money_files() -> list[Path]:
    out = []
    for p in SRC.rglob("*.py"):
        low = str(p).lower()
        if "rtm" in low or "trust" in low or "bank" in low:
            out.append(p)
    return out


def test_rtm_layer_defines_no_money_moving_functions():
    offenders = []
    for p in _rtm_money_files():
        text = p.read_text(encoding="utf-8")
        for name in FORBIDDEN_MONEY_MOVERS:
            if re.search(r"\bdef\s+" + re.escape(name) + r"\s*\(", text):
                offenders.append(f"{p.name}: def {name}(")
    assert not offenders, (
        "RTM trust layer must never define money-moving functions (it records and recommends only): "
        + "; ".join(offenders)
    )


def test_rtm_layer_calls_no_money_moving_operations():
    offenders = []
    for p in _rtm_money_files():
        text = p.read_text(encoding="utf-8")
        for name in FORBIDDEN_MONEY_MOVERS:
            if re.search(r"\." + re.escape(name) + r"\s*\(", text):
                offenders.append(f"{p.name}: .{name}(")
    assert not offenders, (
        "RTM trust layer must never call money-moving operations: " + "; ".join(offenders)
    )


def test_recorded_refund_is_observation_only_if_present():
    """If a refund-recording function exists, it must be framed as recording an observed feed line, not
    as initiating anything. This pins the addendum's record-only refund shape without requiring it."""
    from mongoose.services import rtm_trust
    fn = getattr(rtm_trust, "record_observed_refund_from_feed", None)
    if fn is None:
        return  # not implemented yet; the guard above still forbids the action variants
    doc = (fn.__doc__ or "").lower()
    assert "observed" in doc or "record" in doc
