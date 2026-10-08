from __future__ import annotations

import re
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .settings import get_settings

# Privacy classes whose audit payloads must not be stored in plaintext.
SENSITIVE_PRIVACY = {"high", "secret"}
# Keys whose values are always redacted regardless of privacy class.
SENSITIVE_AUDIT_KEYS = {"token", "password", "secret", "api_key", "authorization", "ha_token"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA_READY: set[str] = set()
_BRAIN_MIGRATION_CHECKED = False


def _protected_brain_root() -> Path:
    """The real Mongoose home tree (default ~/.mongoose). The live brain and ALL its backups live under
    here. connect() refuses to open any path inside this tree from a test process. Guarding the tree (not
    one captured path) is deliberately import-timing-independent: db.py is often first imported lazily,
    from inside a test, AFTER the autouse fixture has already set a tmp MONGOOSE_DB_PATH, so capturing a
    single path would wrongly capture the tmp path. expanduser() of ~/.mongoose has no such dependency.
    A test must never be able to write to real household data, however it is invoked."""
    try:
        return Path("~/.mongoose").expanduser().resolve()
    except Exception:
        return Path("/nonexistent-mongoose-home")


_PROTECTED_BRAIN_ROOT = _protected_brain_root()


def _migrate_legacy_brain_if_needed(target_db: Path) -> None:
    """One-time move of an existing brain from the OLD in-folder location (./data/mongoose.sqlite3) to the
    new stable home-folder default, so changing the default never orphans a user's existing brain.

    Only acts when ALL of these hold, so it is safe and never clobbers anything:
      - the caller is using the DEFAULT home-folder path (an explicit MONGOOSE_DB_PATH override is left
        entirely alone, tests, CI and the sandbox set it, so they never trigger this),
      - the target home-folder DB does NOT already exist (never overwrite a real brain),
      - a legacy ./data/mongoose.sqlite3 DOES exist next to the current working dir.
    Moves the DB plus its sidecars (session_salt, -wal, -shm) so the brain stays consistent.
    """
    global _BRAIN_MIGRATION_CHECKED
    if _BRAIN_MIGRATION_CHECKED:
        return
    _BRAIN_MIGRATION_CHECKED = True
    import os
    # Respect an explicit override: if the user set MONGOOSE_DB_PATH, do nothing.
    if os.getenv("MONGOOSE_DB_PATH"):
        return
    legacy = Path("./data/mongoose.sqlite3").resolve()
    target = Path(target_db).resolve()
    if target.exists() or not legacy.exists() or legacy == target:
        return
    try:
        import shutil
        target.parent.mkdir(parents=True, exist_ok=True)
        # Move the DB and its sidecars together so the brain is migrated whole.
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(legacy) + suffix)
            if src.exists():
                shutil.move(str(src), str(Path(str(target) + suffix)))
        salt = legacy.parent / "session_salt"
        if salt.exists() and not (target.parent / "session_salt").exists():
            shutil.move(str(salt), str(target.parent / "session_salt"))
    except Exception:
        # If the move fails for any reason, fall back to creating a fresh brain at the target rather than
        # crashing; the legacy brain is left untouched and can be restored manually.
        pass


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    path = db_path or settings.db_path
    # SAFETY GUARD: a test/fixture process must NEVER open the real household brain. If we are running
    # under pytest OR the `tests` package is imported (a test function executed directly bypasses the
    # pytest fixtures), refuse to connect to the production DB path. Normal
    # Mongoose never imports `tests`, so this is a pure no-op in the live server (two dict lookups). The
    # conftest autouse fixture already redirects tests to a tmp DB; this is the belt-and-braces that turns
    # a bypass into a loud, immediate failure instead of silent live corruption. Deliberate live ops set
    # MONGOOSE_ALLOW_LIVE_TEST_WRITES=1.
    import sys as _sys
    if "pytest" in _sys.modules or "tests" in _sys.modules:
        import os as _os
        if not _os.environ.get("MONGOOSE_ALLOW_LIVE_TEST_WRITES"):
            try:
                _resolved = Path(path).expanduser().resolve()
            except Exception:
                _resolved = Path(path)
            try:
                _inside = _resolved == _PROTECTED_BRAIN_ROOT or _resolved.is_relative_to(_PROTECTED_BRAIN_ROOT)
            except Exception:
                _inside = False
            if _inside:
                raise RuntimeError(
                    f"REFUSED: a test context tried to open the LIVE brain tree at {_resolved} (under "
                    f"{_PROTECTED_BRAIN_ROOT}). Tests must use an isolated DB (the conftest autouse fixture "
                    "sets a tmp MONGOOSE_DB_PATH). If this is a deliberate live operation, set "
                    "MONGOOSE_ALLOW_LIVE_TEST_WRITES=1.")
    # Before creating anything at the (possibly new home-folder) location, migrate a legacy in-folder
    # brain into place so an update never starts from a blank brain.
    if db_path is None:
        _migrate_legacy_brain_if_needed(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Self-heal: if this database has never been initialized (e.g. a manual
    # unzip + pip install that skipped `mongoose init-db`), create the schema now
    # so nothing throws a raw "no such table" at the user. Checked once per path
    # per process, so it's cheap.
    key = str(path)
    if key not in _SCHEMA_READY:
        _SCHEMA_READY.add(key)
        try:
            probe = sqlite3.connect(path)
            try:
                has = probe.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'"
                ).fetchone()
            finally:
                probe.close()
            if not has:
                init_db(path)
        except Exception:
            # Never let the self-heal probe itself break a real connection.
            pass
    con = sqlite3.connect(path, timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        # Bound how long a write waits on a lock. Without an explicit, short busy_timeout a
        # contended write can stack toward multi-second blocks under load, which is what
        # made the monolithic test gate hang (fast when uncontended, pathological when the
        # machine is slow). 4s is plenty for legitimate contention and fails fast otherwise.
        con.execute("PRAGMA busy_timeout=4000")
        yield con
        con.commit()
    finally:
        con.close()


def init_db(db_path: Path | None = None) -> None:
    with connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS weather_cache (
                key TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS weather_consensus (
                location_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS phrase_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                phrase TEXT NOT NULL,
                used_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS routing_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_key TEXT NOT NULL,
                device_id TEXT NOT NULL,
                room TEXT NOT NULL,
                action TEXT NOT NULL,
                attempted_route TEXT NOT NULL,
                success INTEGER NOT NULL,
                failure_reason TEXT,
                verified_state TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS routing_preferences (
                route_key TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                room TEXT NOT NULL,
                action TEXT NOT NULL,
                preferred_route TEXT NOT NULL,
                confidence REAL NOT NULL,
                evidence_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                app TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                privacy TEXT NOT NULL,
                urgency TEXT NOT NULL,
                device TEXT NOT NULL DEFAULT 'unknown',
                dedupe_key TEXT NOT NULL,
                routed_to TEXT,
                speak_allowed INTEGER NOT NULL DEFAULT 0,
                received_at TEXT NOT NULL,
                raw_json TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_dedupe ON notifications(dedupe_key);

            CREATE VIRTUAL TABLE IF NOT EXISTS notification_fts USING fts5(
                title, body, source, app, content=''
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_event_id TEXT,
                calendar_name TEXT,
                title TEXT NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                busy_status TEXT NOT NULL DEFAULT 'busy',
                sensitivity TEXT NOT NULL DEFAULT 'normal',
                detail_available INTEGER NOT NULL DEFAULT 1,
                raw_json TEXT,
                raw_rfc822_b64 TEXT,
                last_seen TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_events_time ON events(start_at, end_at);

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_task_id TEXT,
                list_name TEXT,
                title TEXT NOT NULL,
                due_at TEXT,
                reminder_at TEXT,
                priority TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                raw_json TEXT,
                raw_rfc822_b64 TEXT,
                last_seen TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS email_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_message_id TEXT,
                mailbox TEXT,
                sender TEXT,
                recipients TEXT,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                sent_at TEXT,
                received_at TEXT,
                privacy TEXT NOT NULL DEFAULT 'medium',
                raw_json TEXT,
                raw_rfc822_b64 TEXT,
                indexed_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS email_fts USING fts5(
                subject, body, sender, mailbox, content=''
            );

            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                mtime REAL NOT NULL,
                device TEXT NOT NULL DEFAULT 'local',
                indexed_at TEXT NOT NULL
            );

            -- File identity is (device, path): the SAME path on two devices (e.g.
            -- /sdcard/Download/report.pdf on a phone and a tablet) is two distinct
            -- files, not a collision. The unique index is created in _migrate_schema
            -- so it works on both fresh DBs and migrated old ones (which gain the
            -- device column during migration).

            CREATE VIRTUAL TABLE IF NOT EXISTS file_fts USING fts5(
                title, body, path, content=''
            );

            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id INTEGER,
                source TEXT NOT NULL,
                source_message_id TEXT,
                filename TEXT NOT NULL,
                mime_type TEXT,
                size_bytes INTEGER,
                sender TEXT,
                subject TEXT,
                email_date TEXT,
                mailbox TEXT,
                content_text TEXT,
                content_indexed INTEGER NOT NULL DEFAULT 0,
                privacy TEXT NOT NULL DEFAULT 'normal',
                dedupe_key TEXT,
                indexed_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS attachment_fts USING fts5(
                filename, subject, sender, content_text, content=''
            );

            CREATE TABLE IF NOT EXISTS voicemail_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id INTEGER,
                source TEXT NOT NULL,
                source_message_id TEXT,
                provider TEXT NOT NULL DEFAULT 'unknown',
                mailbox TEXT,
                mailbox_label TEXT,
                account_number TEXT,
                caller_number TEXT,
                subject TEXT NOT NULL,
                notice_text TEXT,
                voicemail_at TEXT,
                email_date TEXT,
                audio_attachment_id INTEGER,
                audio_filename TEXT,
                audio_mime_type TEXT,
                audio_size_bytes INTEGER,
                manage_url TEXT,
                delete_url TEXT,
                new_message_count INTEGER,
                old_message_count INTEGER,
                transcript TEXT,
                transcript_status TEXT NOT NULL DEFAULT 'pending',
                transcript_source TEXT,
                privacy TEXT NOT NULL DEFAULT 'medium',
                raw_json TEXT,
                dedupe_key TEXT,
                identity_key TEXT,
                canonical_uid TEXT,
                source_refs TEXT NOT NULL DEFAULT '[]',
                remote_audio_path TEXT,
                indexed_at TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_voicemail_dedupe
                ON voicemail_messages(dedupe_key);
            CREATE INDEX IF NOT EXISTS idx_voicemail_identity
                ON voicemail_messages(identity_key);

            CREATE VIRTUAL TABLE IF NOT EXISTS voicemail_fts USING fts5(
                provider, mailbox_label, account_number, caller_number, subject,
                notice_text, transcript, audio_filename, content=''
            );

            CREATE TABLE IF NOT EXISTS assets (
                asset_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                kind TEXT NOT NULL,
                mime_type TEXT,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                uploaded_by TEXT,
                source_surface TEXT,
                classification TEXT,
                available_for_self_update INTEGER NOT NULL DEFAULT 0,
                available_for_rtm INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                module TEXT NOT NULL,
                action TEXT NOT NULL,
                route TEXT,
                privacy TEXT,
                success INTEGER NOT NULL,
                reason TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages(conversation_id, id);

            CREATE TABLE IF NOT EXISTS bus_messages (
                id TEXT PRIMARY KEY,
                sender TEXT,
                recipient TEXT,
                type TEXT,
                subject TEXT,
                created TEXT,
                status TEXT NOT NULL DEFAULT 'unread'
            );
            CREATE INDEX IF NOT EXISTS idx_bus_recipient ON bus_messages(recipient, status, created);
            CREATE VIRTUAL TABLE IF NOT EXISTS bus_fts USING fts5(
                message_id UNINDEXED, subject, body, sender, recipient
            );

            CREATE TABLE IF NOT EXISTS memory_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact TEXT NOT NULL,
                source TEXT NOT NULL,
                dedupe_key TEXT UNIQUE,
                scope TEXT NOT NULL DEFAULT 'owner',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                fact, content=''
            );

            -- Crawler state. This is what turns the periodic re-scan into a real
            -- backfill + freshness system: each source tracks its own cursor (an
            -- IMAP UID, a Gmail historyId, a file-walk path, a Graph deltaLink) so
            -- freshness fetches only what's new, and backfill is resumable instead
            -- of re-scanning from the top every run.
            CREATE TABLE IF NOT EXISTS crawl_sources (
                source TEXT PRIMARY KEY,        -- e.g. 'imap:INBOX', 'files:/Users/x/Docs', 'gmail'
                kind TEXT NOT NULL,             -- 'imap' | 'gmail' | 'microsoft' | 'files'
                enabled INTEGER NOT NULL DEFAULT 1,
                backfill_status TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|error
                backfill_cursor TEXT,           -- resume point for historical crawl
                fresh_cursor TEXT,              -- last-seen marker for incremental freshness
                last_started TEXT,
                last_completed TEXT,
                last_status TEXT,
                last_error TEXT,
                scanned INTEGER NOT NULL DEFAULT 0,
                indexed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS crawl_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                mode TEXT NOT NULL,             -- 'backfill' | 'fresh'
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT,                    -- running|done|error
                scanned INTEGER NOT NULL DEFAULT 0,
                indexed INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                errored INTEGER NOT NULL DEFAULT 0
            );

            -- Social / ritual layer (v0.14). WhatsApp messages are indexed locally
            -- like email: searchable, brief-relevant, privacy-classed. The bridge
            -- NEVER gives the model raw send authority; outbound goes through an
            -- approval-gated draft (see whatsapp_drafts).
            CREATE TABLE IF NOT EXISTS whatsapp_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                contact TEXT,                  -- display name
                direction TEXT NOT NULL,       -- 'in' | 'out'
                wa_message_id TEXT UNIQUE,
                body TEXT,
                media_kind TEXT,               -- image/video/doc/none (metadata only)
                quoted_id TEXT,
                sent_at TEXT,
                received_at TEXT,
                read_status TEXT,
                privacy TEXT NOT NULL DEFAULT 'medium',
                provider_session TEXT,         -- WAHA session the message arrived on
                session_role TEXT,             -- owner_read | mongoose_send | NULL (legacy)
                indexed_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS whatsapp_fts USING fts5(
                contact, body, content=''
            );
            -- Maps a human alias/display name to a real chat_id, learned from
            -- indexed messages, so 'draft to Sam' can resolve to Sam's chat id
            -- before sending (WAHA needs a chat id like 1234@c.us, not a name).
            CREATE TABLE IF NOT EXISTS whatsapp_contact_aliases (
                alias TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                display_name TEXT,
                updated_at TEXT NOT NULL
            );

            -- Outbound drafts awaiting explicit human approval. Mongoose may create
            -- and edit these; only an approved draft is ever sent.
            CREATE TABLE IF NOT EXISTS whatsapp_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                contact TEXT,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|sent|cancelled|failed
                created_at TEXT NOT NULL,
                decided_at TEXT,
                send_role TEXT,                -- which sender account approves-and-sends this (default mongoose_send)
                send_session TEXT,
                error TEXT
            );

            -- Ritual completions: which micro-practices were done and when, so
            -- Mongoose can learn which actually get completed (and honour cooldowns).
            CREATE TABLE IF NOT EXISTS ritual_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ritual TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                outcome TEXT                   -- completed|skipped|snoozed
            );

            -- Local activity diary: a readable trace of what Mongoose did.
            CREATE TABLE IF NOT EXISTS diary_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                kind TEXT NOT NULL,            -- ritual|search|whatsapp|brief|notify|file|other
                summary TEXT NOT NULL
            );

            -- 'What changed?' checkpoints: the last time the user acknowledged the
            -- delta, so each call reports only genuinely-new things since then.
            CREATE TABLE IF NOT EXISTS change_checkpoints (
                domain TEXT PRIMARY KEY,       -- whatsapp|email|files|calendar|notifications
                last_seen TEXT NOT NULL
            );
            """
        )
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,           -- small persisted UI/onboarding flags
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

"""
        )
        _migrate_schema(con)
        _verify_schema(con)


def _verify_schema(con) -> None:
    """After migration, assert the invariants that recent migrations are supposed
    to establish. If a REQUIRED change didn't take (e.g. the (device, path) unique
    index is missing, or a column wasn't added), raise loudly instead of letting
    the app run on a silently-wrong schema. This is the 'migrations fail loudly'
    guard: a swallowed migration that leaves data integrity broken is worse than a
    clear startup error."""
    problems: list[str] = []
    # Required columns added across versions.
    required_columns = {
        "files": {"device"},
        "notifications": {"device"},
        "email_messages": {"privacy"},
    }
    for table, cols in required_columns.items():
        present = _column_names(con, table)
        if present:  # table exists
            missing = cols - present
            if missing:
                problems.append(f"{table} missing column(s): {', '.join(sorted(missing))}")
    # Required: file identity is (device, path), so two devices can't collide.
    if _column_names(con, "files"):
        has_idx = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_files_device_path'"
        ).fetchone()
        # the old path-only UNIQUE must be gone (it would still cause collisions)
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='files'").fetchone()
        old_unique = "PATH TEXT NOT NULL UNIQUE" in ((row["sql"] if row else "") or "").upper()
        if not has_idx or old_unique:
            problems.append("files identity is not (device, path) (cross-device collisions possible)")
    if problems:
        raise RuntimeError(
            "Schema verification FAILED after migration: " + "; ".join(problems)
            + ". The database is on an unexpected schema; do not run on this DB. "
            "Restore from backup or recreate.")


# Current schema version. Bump when adding a migration below.
SCHEMA_VERSION = 1


def get_app_state(key: str, default: str | None = None) -> str | None:
    """Read a small persisted flag from app_state (e.g. an onboarding choice)."""
    with connect() as con:
        row = con.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_app_state(key: str, value: str) -> None:
    """Persist a small flag so a choice survives a page reload / restart."""
    with connect() as con:
        con.execute(
            "INSERT INTO app_state(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, utc_now_iso()))
        con.commit()


def _column_names(con, table: str) -> set[str]:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _migrate_schema(con) -> None:
    """Idempotent schema migrations for DBs created by older versions.
    CREATE TABLE IF NOT EXISTS adds new tables but never new COLUMNS to existing
    tables, so an old DB can be missing columns added across versions. We use
    PRAGMA user_version to apply column-adds once, safely, preserving data.

    Each migration is written to be safe to run on a fresh DB too (it checks for
    the column first), so init order doesn't matter."""
    version = con.execute("PRAGMA user_version").fetchone()[0]

    # Migration 1: ensure columns that were added to existing tables after their
    # original creation exist. Add new ones here as the schema evolves; guard each
    # with a column-existence check so it is safe and idempotent.
    add_if_missing = [
        # (table, column, column_def)
        ("events", "sensitivity", "TEXT NOT NULL DEFAULT 'normal'"),
        ("events", "detail_available", "INTEGER NOT NULL DEFAULT 1"),
        ("email_messages", "privacy", "TEXT NOT NULL DEFAULT 'normal'"),
        ("email_messages", "raw_rfc822_b64", "TEXT"),
        ("tasks", "reminder_at", "TEXT"),
        ("attachments", "payload_b64", "TEXT"),
        ("files", "device", "TEXT NOT NULL DEFAULT 'local'"),
        ("notifications", "device", "TEXT NOT NULL DEFAULT 'unknown'"),
        # Voicemail dual-source reconciliation: identity columns let the same real-world voicemail seen
        # via IMAP and via the Pi/HA protocol staging file merge into one canonical row. Additive, so an
        # older voicemail_messages table gains them without data loss.
        ("voicemail_messages", "identity_key", "TEXT"),
        ("voicemail_messages", "canonical_uid", "TEXT"),
        ("voicemail_messages", "source_refs", "TEXT NOT NULL DEFAULT '[]'"),
        ("voicemail_messages", "remote_audio_path", "TEXT"),
        # Memory isolation: trusted-guest conversations get their own fact namespace so the owner's
        # facts never surface in a guest chat, and a guest's facts never pollute the owner brain.
        # Default 'owner' keeps every existing fact and caller on the owner scope unchanged.
        ("memory_facts", "scope", "TEXT NOT NULL DEFAULT 'owner'"),
        # Dual-account WhatsApp: inbound messages record which WAHA session/role they arrived on, so
        # the owner's inbox search stays separate from replies to Mongoose's own notification number. Drafts
        # record which sender role/session they should go out on. All additive with safe defaults, so
        # single-account installs are unchanged (NULL role reads as legacy/owner).
        ("whatsapp_messages", "provider_session", "TEXT"),
        ("whatsapp_messages", "session_role", "TEXT"),
        ("whatsapp_drafts", "send_role", "TEXT"),
        ("whatsapp_drafts", "send_session", "TEXT"),
        # Message attribution: WHICH device wrote a user message. Nullable; historical rows
        # stay NULL. Exists because a mis-paired device once wrote a stranger's chats into
        # the shared main conversation, and without attribution neither the UI nor revoke-
        # time cleanup could tell whose words were whose.
        ("messages", "sender_device_id", "TEXT"),
        ("messages", "sender_label", "TEXT"),
        # Is this task RTM (company) work rather than personal? Owner-set, with an advisory guess when a
        # task arrives from RTM context. NULL means "not yet decided", so an unclassified task reads as
        # unclassified instead of being silently asserted personal. Purely a LOCAL label: the Microsoft
        # To Do sync is read-only (Tasks.Read, no write-back anywhere), so an RTM task cannot travel back
        # out to Microsoft , this exists so the owner can SEE and FILTER the split.
        ("tasks", "is_rtm", "INTEGER"),
    ]
    for table, column, coldef in add_if_missing:
        cols = _column_names(con, table)
        if cols and column not in cols:
            try:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
            except Exception:
                pass  # best-effort; never block startup on a migration

    # Old DBs may contain rows with a NULL source_*_id (manual ingests before the
    # dedupe fix). In SQLite GROUP BY treats all NULLs as EQUAL, so a naive dedupe
    # would collapse DISTINCT manual rows into one and delete real user data. So
    # FIRST synthesize a deterministic local id for each NULL row (making distinct
    # rows distinct), THEN dedupe genuine duplicates, THEN create the unique index.
    _dedupe_unique = [
        ("idx_email_dedupe", "email_messages", "source_message_id", "source, source_message_id"),
        ("idx_event_dedupe", "events", "source_event_id", "source, source_event_id"),
        ("idx_task_dedupe", "tasks", "source_task_id", "source, source_task_id"),
    ]
    for index_name, table, id_col, cols in _dedupe_unique:
        if not _column_names(con, table):
            continue
        try:
            # Give every NULL-id row its own synthetic, stable id derived from its
            # primary key, so two distinct NULL rows never look like duplicates.
            con.execute(
                f"UPDATE {table} SET {id_col} = 'local:' || id "
                f"WHERE {id_col} IS NULL")
            # Now collapse only genuine duplicates (same source + same real id).
            con.execute(
                f"DELETE FROM {table} WHERE id NOT IN "
                f"(SELECT MIN(id) FROM {table} GROUP BY {cols})")
            con.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table}({cols})")
        except Exception:
            pass  # best-effort

    # Migration: file identity must be (device, path), not path alone, so the same
    # path on two devices doesn't collide. Older DBs created the files table with a
    # column-level UNIQUE on path. If that's present, rebuild the table without it
    # and add the composite unique index. SQLite can't drop a column constraint in
    # place, so we recreate. Safe and idempotent: only runs if the old shape exists.
    try:
        if _column_names(con, "files"):
            # Detect the old path-only UNIQUE by checking the table's SQL.
            row = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='files'").fetchone()
            sql = (row["sql"] if row else "") or ""
            has_composite = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_files_device_path'"
            ).fetchone()
            if "UNIQUE" in sql.upper() and "PATH TEXT NOT NULL UNIQUE" in sql.upper():
                con.execute("ALTER TABLE files RENAME TO files_old")
                con.execute(
                    """
                    CREATE TABLE files (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        path TEXT NOT NULL,
                        title TEXT NOT NULL,
                        body TEXT NOT NULL,
                        mtime REAL NOT NULL,
                        device TEXT NOT NULL DEFAULT 'local',
                        indexed_at TEXT NOT NULL
                    )
                    """
                )
                cols = _column_names(con, "files_old")
                dev = "device" if "device" in cols else "'local'"
                con.execute(
                    f"INSERT INTO files(id, path, title, body, mtime, device, indexed_at) "
                    f"SELECT id, path, title, body, mtime, {dev}, indexed_at FROM files_old")
                con.execute("DROP TABLE files_old")
                con.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_files_device_path ON files(device, path)")
            elif not has_composite:
                con.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_files_device_path ON files(device, path)")
    except Exception:
        pass  # never block startup on this migration

    if version < SCHEMA_VERSION:
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if str(k).lower() in SENSITIVE_AUDIT_KEYS else _redact_value(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _redact_privacy_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact_privacy_payload(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_privacy_payload(v) for v in value]
    return "[redacted:privacy]"


def redact_audit_payload(payload: dict[str, Any] | None, privacy: str | None = None) -> dict[str, Any]:
    payload = payload or {}
    if (privacy or "").lower() in SENSITIVE_PRIVACY:
        return _redact_privacy_payload(payload)
    return _redact_value(payload)


def safe_fts_query(text: str) -> str | None:
    """Turn arbitrary user text into a safe FTS5 MATCH query.

    FTS5 has its own query syntax; passing raw user input to MATCH throws
    OperationalError on stray quotes, NEAR(, colons, etc. We extract alnum
    terms, quote each as a literal, and OR them. Returns None if there are no
    usable terms (caller should return empty results, not error).
    """
    terms = re.findall(r"[A-Za-z0-9]+", text or "")
    terms = [t for t in terms if len(t) > 1]
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)


def redact_reason(reason: str | None) -> str | None:
    """Strip secrets from a free-text audit reason before it is persisted.

    Exception strings from HTTP clients can contain full URLs with credentials
    in the query string (e.g. ...?key=SECRET) or auth headers. We redact common
    secret-bearing patterns and drop query strings entirely.
    """
    if not reason:
        return reason
    text = str(reason)
    # Redact "Bearer <token>" sequences first (auth headers carry a space, which
    # the key=value rule below would otherwise stop at).
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", text)
    # Redact secret-bearing query params / headers anywhere in the text.
    text = re.sub(r"(?i)\b(key|api_key|apikey|token|access_token|authorization|password|secret)=[^\s&\"']+",
                  r"\1=[redacted]", text)
    # Drop any remaining query string after a URL (defence in depth).
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]*", r"\1?[redacted]", text)
    return text


def _insert_audit_row(module: str, action: str, success: bool, *, route: str | None = None,
                      privacy: str | None = None, reason: str | None = None,
                      payload: dict[str, Any] | None = None) -> None:
    safe_payload = redact_audit_payload(payload, privacy)
    safe_reason = redact_reason(reason)
    with connect() as con:
        con.execute(
            """
            INSERT INTO audit_log(module, action, route, privacy, success, reason, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (module, action, route, privacy, 1 if success else 0, safe_reason, json.dumps(safe_payload), utc_now_iso()),
        )


def audit(module: str, action: str, success: bool, *, route: str | None = None,
          privacy: str | None = None, reason: str | None = None,
          payload: dict[str, Any] | None = None) -> None:
    """Write an audit event, creating the schema on first use if needed.

    The app lifespan normally calls init_db(), but connector/LLM/cloud modules
    may be exercised directly in tests or scripts. Audit must not be the thing
    that makes a safe pre-flight refusal crash because the DB was not yet
    initialised. Only the missing-table case self-heals; other errors propagate.
    """
    try:
        _insert_audit_row(module, action, success, route=route, privacy=privacy, reason=reason, payload=payload)
    except sqlite3.OperationalError as exc:
        if "no such table: audit_log" not in str(exc).lower():
            raise
        init_db()
        _insert_audit_row(module, action, success, route=route, privacy=privacy, reason=reason, payload=payload)
