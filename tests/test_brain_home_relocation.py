"""The brain (DB + session_salt + identity) must live in the user's HOME folder, not inside the install
folder, so it survives a reinstall/update into a fresh download folder. Plus a one-time migration that
moves an existing in-folder brain to the home location without orphaning or clobbering it."""
from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile
from pathlib import Path


def test_default_db_path_is_in_home_not_install_folder():
    os.environ.pop("MONGOOSE_DB_PATH", None)
    from mongoose.settings import _default_db_path
    p = _default_db_path()
    home = str(Path(os.path.expanduser("~")))
    assert p.startswith(home), f"brain should be under home, got {p}"
    assert ".mongoose" in p
    # must NOT be the old in-folder relative location
    assert not p.startswith("./"), p


def test_explicit_env_override_still_wins():
    os.environ["MONGOOSE_DB_PATH"] = "/tmp/explicit_brain/m.sqlite3"
    try:
        import mongoose.settings as S
        importlib.reload(S)
        assert str(S.get_settings().db_path) == "/tmp/explicit_brain/m.sqlite3"
    finally:
        os.environ.pop("MONGOOSE_DB_PATH", None)
        importlib.reload(__import__("mongoose.settings", fromlist=["x"]))


def _run_migration(work: str, home: str):
    os.chdir(work)
    os.environ.pop("MONGOOSE_DB_PATH", None)
    os.environ["HOME"] = home
    import mongoose.settings as S
    importlib.reload(S)
    target = Path(S._default_db_path())
    import mongoose.db as D
    importlib.reload(D)
    D._BRAIN_MIGRATION_CHECKED = False
    # The live-brain guard protects <HOME>/.mongoose from any test. Here HOME is a throwaway temp folder
    # standing in for a user's home, so prove the guard is aimed at it (never the real home) and opt in
    # for this one connection only.
    fake_root = Path(home).resolve() / ".mongoose"
    assert Path(D._PROTECTED_BRAIN_ROOT).resolve() == fake_root, D._PROTECTED_BRAIN_ROOT
    os.environ["MONGOOSE_ALLOW_LIVE_TEST_WRITES"] = "1"
    try:
        with D.connect(None):
            pass
    finally:
        os.environ.pop("MONGOOSE_ALLOW_LIVE_TEST_WRITES", None)
    return target


def test_legacy_brain_is_migrated_to_home():
    work = tempfile.mkdtemp(); home = tempfile.mkdtemp()
    # a legacy ./data brain with a marker
    Path(work, "data").mkdir(parents=True)
    con = sqlite3.connect(os.path.join(work, "data", "mongoose.sqlite3"))
    con.execute("CREATE TABLE marker(x TEXT)"); con.execute("INSERT INTO marker VALUES('REAL')"); con.commit(); con.close()
    Path(work, "data", "session_salt").write_text("salt")

    target = _run_migration(work, home)
    moved = sqlite3.connect(str(target))
    got = moved.execute("SELECT x FROM marker").fetchone()
    moved.close()
    assert got == ("REAL",), "legacy brain should have moved to home"
    assert (target.parent / "session_salt").exists(), "session_salt should migrate too"
    assert not Path(work, "data", "mongoose.sqlite3").exists(), "legacy DB should be moved, not copied"


def test_migration_never_clobbers_existing_home_brain():
    work = tempfile.mkdtemp(); home = tempfile.mkdtemp()
    os.environ.pop("MONGOOSE_DB_PATH", None); os.environ["HOME"] = home
    import mongoose.settings as S
    importlib.reload(S)
    target = Path(S._default_db_path()); target.parent.mkdir(parents=True, exist_ok=True)
    # an existing HOME brain to protect
    con = sqlite3.connect(str(target)); con.execute("CREATE TABLE marker(x TEXT)"); con.execute("INSERT INTO marker VALUES('HOME')"); con.commit(); con.close()
    # a different legacy brain that must be ignored
    Path(work, "data").mkdir(parents=True)
    con = sqlite3.connect(os.path.join(work, "data", "mongoose.sqlite3")); con.execute("CREATE TABLE marker(x TEXT)"); con.execute("INSERT INTO marker VALUES('LEGACY')"); con.commit(); con.close()

    _run_migration(work, home)
    m = sqlite3.connect(str(target)); got = m.execute("SELECT x FROM marker").fetchone(); m.close()
    assert got == ("HOME",), "existing home brain must not be clobbered"
