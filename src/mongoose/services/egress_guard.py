"""The execution-context boundary between Mongoose and the real world.

Why this exists: on 2026-07-18 a test (`test_rtm_metro_reconcile.py`) run on the live Mac published its
fixtures (Nina / Nadia / a £300 demand) straight onto the live leaseholder portal, because the auto-publish
path was gated only by "is WordPress configured" , which is always true on this machine. There was no wall
between test/dev execution and live external systems. This module is that wall.

Every action that mutates or messages a LIVE external system (WordPress writes, WhatsApp/push/email sends,
Home Assistant actuation) MUST pass `assert_live_egress()` / check `egress_allowed()` first. It is
FAIL-CLOSED: external side effects are refused whenever the process looks like a test, CI, or ephemeral/dev
context. The primary signal (`pytest` imported into the process) is race-free , it stays true for the whole
process lifetime, so even an OFF-THREAD callback fired after a test's per-test env var is cleared is still
refused. A test therefore physically cannot reach a real system, no matter which code path it exercises.

Deliberate admin actions from a real shell on the real brain (e.g. `mongoose rtm portal publish`) are NOT a
test context and remain allowed , the guard blocks test/ephemeral execution, not intent.
"""
from __future__ import annotations

import os
import sys


class EgressBlocked(RuntimeError):
    """Raised by assert_live_egress() when a real-world side effect is attempted from a non-live context."""


def _under_test() -> bool:
    # `pytest` in sys.modules is the load-bearing, race-free signal: if this interpreter imported pytest,
    # it is a test process for its whole life, including background threads and atexit callbacks.
    if "pytest" in sys.modules or "_pytest" in sys.modules:
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if (os.environ.get("MONGOOSE_ENV", "") or "").strip().lower() in ("test", "ci", "dev", "sandbox"):
        return True
    return False


def _data_dir_is_ephemeral() -> bool:
    """A test/sandbox points MONGOOSE_DB_PATH / MONGOOSE_DATA_DIR at a throwaway temp dir (that isolation is
    exactly what made the DB writes 'safe' , and is exactly what marks the process as non-live for egress)."""
    for var in ("MONGOOSE_DB_PATH", "MONGOOSE_DATA_DIR"):
        d = (os.environ.get(var, "") or "").lower()
        if d and any(seg in d for seg in ("/tmp/", "/private/tmp/", "/var/folders/",
                                          "/private/var/folders/", "pytest-", "/t/pytest")):
            return True
    return False


def egress_allowed(action: str = "") -> tuple[bool, str]:
    """(allowed, reason). Fail-closed: any test/ephemeral signal denies real-world egress."""
    if _under_test():
        return False, "test harness (pytest present in this process)"
    if _data_dir_is_ephemeral():
        return False, "ephemeral/temp data dir (isolated, non-live)"
    if (os.environ.get("MONGOOSE_EGRESS", "") or "").strip().lower() == "off":
        return False, "MONGOOSE_EGRESS=off (manual hold)"
    return True, "live"


def assert_live_egress(action: str) -> None:
    ok, why = egress_allowed(action)
    if not ok:
        raise EgressBlocked(f"external egress refused for '{action}': {why}")
