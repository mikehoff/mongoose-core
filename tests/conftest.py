"""Test safety net.

A release-gate test must be deterministic and offline. v0.14.18 shipped a test that
called the real install-missing endpoint and triggered a live `pip install
openai-whisper`, which hung the suite. To stop that class of bug recurring SILENTLY,
this autouse fixture intercepts any attempt to shell out to `pip install` during the
test run and fails loudly instead, so a test that tries a real install is caught at
once and must mock it (or be marked slow and excluded from the gate)."""
import os
import subprocess
import tempfile

import pytest

# Session-wide HOME isolation. _default_secret_path()'s legacy migration copies CWD-relative
# ./secrets/<file> into the stable per-user location under HOME; several tests chdir into temp
# dirs containing fake tokens, so with a REAL home the suite silently plants fake OAuth
# credential files in the developer's own ~/.mongoose/secrets (observed live). Isolating HOME
# for the whole test session closes that class at a single point. Applied at import time so it
# is in place before any module-level Settings load.
_TEST_HOME = tempfile.mkdtemp(prefix="mongoose-test-home-")
os.environ["HOME"] = _TEST_HOME


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Fresh HOME per test, so credential files one test migrates into ~/.mongoose/secrets can
    never bleed into another test's 'bare install' state, let alone the developer's real home.
    Tests that set HOME themselves simply override this for their own duration."""
    home = tmp_path / "_isolated_test_home"   # deliberately unusual name: several tests create their own tmp_path/"home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    yield


@pytest.fixture(autouse=True)
def _restore_cwd():
    """Restore the working directory after each test. Several setup/OAuth tests os.chdir into a
    temp dir that contains a ./secrets/ folder (client JSON, tokens); without restoring CWD that
    temp dir, and its tokens, leaks into later tests whose OAuth/mail-status logic reads
    ./secrets/ relative to CWD. That produced order-dependent failures (e.g. a later mail-status
    test seeing a stray Google token). Saving/restoring CWD per test removes the whole class."""
    import os as _os
    cwd = _os.getcwd()
    try:
        yield
    finally:
        try:
            _os.chdir(cwd)
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _isolate_db_per_test(tmp_path, monkeypatch):
    """Give every test its own fresh SQLite DB by default, so tests can't leak state into
    each other (the class of order-dependent flakiness that made the gate feel fragile,
    e.g. a prior test's rows changing a later test's result). A test that deliberately
    sets its own MONGOOSE_DB_PATH still wins, monkeypatch.setenv inside the test overrides
    this. This is set BEFORE the test body runs so module-level get_settings() calls in
    the test pick it up.

    Also reset db._SCHEMA_READY (the per-path schema-init cache). It's keyed by path so a
    fresh tmp path is already a fresh key, but clearing it per test removes the whole
    category of 'a module cache outlived the isolation' risk at no cost (the QA audit
    correctly flagged this pattern as a determinism hazard).
    """
    db = tmp_path / "test_mongoose.sqlite3"
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(db))
    # Ordinary tests must NOT start indefinite background loops via the app lifespan (TestClient
    # triggers lifespan). Hidden loops were a source of slow/flaky/"frozen"-looking tests. A test that
    # specifically exercises the scheduler/watchdog opts back in by setting these env vars itself.
    monkeypatch.setenv("MONGOOSE_INPROCESS_SCHEDULER", "false")
    monkeypatch.setenv("MONGOOSE_RUNTIME_WATCHDOG", "false")
    # Tests must never make the Mac speak. The TTS command defaults to macOS `say`, so point it at a
    # command that cannot exist; voice.speak() then only prints. A test that needs TTS mocks it.
    monkeypatch.setenv("MONGOOSE_TTS_CMD", "mongoose-test-no-tts")
    try:
        import mongoose.settings as _settings
        _settings._SETTINGS = None
    except Exception:
        pass
    try:
        import mongoose.db as _db
        _db._SCHEMA_READY.clear()
        try:
            import mongoose.profile as _profile
            _profile.reset_cache()
        except Exception:
            pass
    except Exception:
        pass
    yield
    try:
        import mongoose.settings as _settings
        _settings._SETTINGS = None
    except Exception:
        pass
    try:
        import mongoose.db as _db
        _db._SCHEMA_READY.clear()
        try:
            import mongoose.profile as _profile
            _profile.reset_cache()
        except Exception:
            pass
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_env_path(tmp_path, monkeypatch):
    """Isolate setup_wizard.ENV_PATH AND the process environment per test.

    Two distinct leaks made the full suite order-dependent while every test passed in isolation:

    1. ENV_PATH is a mutable module global (default Path('.env')). Setup/wizard tests reassign it;
       not all restore it, and the default is CWD-relative, so a stray .env from a prior test could
       be read by a later test's effective_env() and make a 'bare install' look configured.

    2. dotenv.load_dotenv() (called from settings and the wizard) MUTATES os.environ in place with
       no undo. A test that writes a .env containing HA_URL/HA_TOKEN/etc. and triggers a settings or
       wizard load leaks those keys into os.environ for every subsequent test in the process. That is
       what actually broke test_connected_domains_empty_on_bare_install, test_fresh_install_nothing_
       fake_ready and test_probe_distinguishes_configured_from_usable when a setup test ran first.

    We snapshot os.environ and ENV_PATH before the test and restore both afterwards, so no test can
    pollute another regardless of its own cleanup hygiene.
    """
    import os as _os
    env_snapshot = dict(_os.environ)
    try:
        import mongoose.setup_wizard as _sw
        original_env_path = getattr(_sw, "ENV_PATH", None)
    except Exception:
        _sw = None
        original_env_path = None
    if _sw is not None:
        from pathlib import Path as _P
        # Point at a guaranteed-nonexistent file so effective_env() reads no stray .env by default.
        _sw.ENV_PATH = _P(str(tmp_path / "nonexistent.env"))
    try:
        yield
    finally:
        if _sw is not None and original_env_path is not None:
            _sw.ENV_PATH = original_env_path
        # Restore os.environ exactly: remove keys the test added, restore keys it changed/removed.
        for k in list(_os.environ.keys()):
            if k not in env_snapshot:
                del _os.environ[k]
        for k, v in env_snapshot.items():
            if _os.environ.get(k) != v:
                _os.environ[k] = v


@pytest.fixture(autouse=True)
def _block_real_pip_installs(monkeypatch, request):
    """Fail fast if a test invokes a real `pip install`. Tests that intend to exercise
    install plumbing must mock subprocess.run themselves (which overrides this), or be
    marked @pytest.mark.slow and run outside the gate."""
    real_run = subprocess.run

    def guarded_run(cmd, *args, **kwargs):
        try:
            parts = cmd if isinstance(cmd, (list, tuple)) else str(cmd).split()
            joined = " ".join(str(p) for p in parts)
        except Exception:
            joined = str(cmd)
        if "pip" in joined and "install" in joined and "--dry-run" not in joined:
            raise AssertionError(
                "a test attempted a REAL `pip install` during the gate: "
                f"{joined!r}. Mock subprocess.run / install_capability, or mark the "
                "test @pytest.mark.slow.")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    yield
