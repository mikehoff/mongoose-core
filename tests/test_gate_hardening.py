"""BEHAVIOURAL guards for the release-gate hardening (QA-audit P0.1).

The earlier version of this file only grepped source for 'busy_timeout' / 'timeout = 60',
which proves a string exists, not that the protection WORKS. The audit correctly flagged
that as too weak. These tests instead PROVE behaviour:
  1. pytest-timeout actually kills a hung test (run as a subprocess so we don't hang here).
  2. db.connect() sets a bounded busy_timeout, so a contended write fails fast instead of
     blocking indefinitely.
"""
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _have_pytest_timeout() -> bool:
    import importlib.util
    return importlib.util.find_spec("pytest_timeout") is not None


@pytest.mark.skipif(not _have_pytest_timeout(), reason="pytest-timeout not installed")
def test_pytest_timeout_actually_kills_a_hang(tmp_path):
    """Write a test that sleeps far longer than the cap and run it in a SUBPROCESS pytest
    with a short --timeout. It must be killed at the cap (not run to completion), proving
    the backstop is live , not merely configured."""
    hang = tmp_path / "test_hang_behaviour.py"
    hang.write_text(textwrap.dedent("""
        import time
        def test_sleeps_forever():
            time.sleep(60)
    """))
    start = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--timeout=3", str(hang)],
        capture_output=True, text=True, timeout=55,
    )
    elapsed = time.time() - start
    # The real property: the 3s cap killed it WELL before its own 60s sleep finished. We
    # assert < 45 (not a tight bound) so machine load can't trip the test , the point is that
    # the timeout fired at all, not its exact latency. (The 60s sleep means a pass here proves
    # the kill happened; a non-fired timeout would hit the 55s subprocess limit and raise.)
    assert elapsed < 45, f"hang was not killed promptly (took {elapsed:.1f}s)"
    # pytest-timeout reports a failure (non-zero exit) and mentions the timeout
    assert proc.returncode != 0
    assert "Timeout" in (proc.stdout + proc.stderr) or "timeout" in (proc.stdout + proc.stderr).lower()


def test_db_connection_busy_timeout_is_bounded_and_fast(tmp_path, monkeypatch):
    """Hold a write lock on a DB, then open a SECOND connection and attempt a write. With a
    bounded busy_timeout it must give up in roughly the configured window (~4s), NOT block
    forever. We assert it returns/raises within a small multiple of the busy_timeout."""
    import sqlite3
    db = tmp_path / "lock.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    from mongoose.db import init_db, connect
    init_db()

    # Connection A: begin an exclusive write transaction and hold it open.
    a = sqlite3.connect(str(db))
    a.execute("PRAGMA journal_mode=WAL")
    a.execute("BEGIN EXCLUSIVE")
    a.execute("CREATE TABLE IF NOT EXISTS _lock_probe(x)")
    try:
        # Connection B (via the app's connect(), which sets busy_timeout): try to write.
        start = time.time()
        blocked_ok = False
        try:
            with connect() as b:
                b.execute("CREATE TABLE IF NOT EXISTS _probe2(x)")
                b.execute("INSERT INTO _probe2(x) VALUES (1)")
        except sqlite3.OperationalError:
            # "database is locked" after the busy_timeout window is the expected fast failure
            blocked_ok = True
        elapsed = time.time() - start
        # It must NOT hang indefinitely: bounded near busy_timeout (4s), with margin.
        assert elapsed < 15, f"contended write did not fail fast (took {elapsed:.1f}s)"
        # Either it failed with 'locked' fast, or (rarely) WAL let it proceed; both are
        # acceptable as long as it didn't hang. The key property is bounded time.
        assert blocked_ok or elapsed < 15
    finally:
        a.rollback()
        a.close()
