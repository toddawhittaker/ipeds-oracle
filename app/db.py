"""App-state database (`app.db`) — everything that is NOT survey data.

Kept separate from ipeds.db so rebuilding/atomic-swapping the survey data never
touches users, skills, or chat history. Plain sqlite3 with WAL; the schema is
created idempotently on startup.
"""
from __future__ import annotations

import sqlite3
import time

from app.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    last_login    REAL
);

-- Source of truth for who may request a magic link.
CREATE TABLE IF NOT EXISTS allowlist (
    email      TEXT PRIMARY KEY,
    note       TEXT,
    added_by   TEXT,
    added_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS access_requests (
    id         INTEGER PRIMARY KEY,
    email      TEXT NOT NULL,
    reason     TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|denied
    created_at REAL NOT NULL
);

-- Single-use magic-link tokens (only the hash is stored).
CREATE TABLE IF NOT EXISTS login_tokens (
    token_hash TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used_at    REAL
);

-- One row per magic-link/access request, used for sliding-window rate limiting.
CREATE TABLE IF NOT EXISTS auth_request_attempts (
    email      TEXT NOT NULL,
    ip         TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_attempts_created ON auth_request_attempts(created_at);

-- Long-lived sessions (only the hash is stored; the cookie holds the raw token).
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    title      TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_conv_user ON conversations(user_id, updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    role            TEXT NOT NULL,        -- user|assistant
    content         TEXT NOT NULL,
    sql_log         TEXT,                 -- JSON list of executed SQL
    model_used      TEXT,
    tokens          INTEGER,
    feedback        INTEGER,              -- +1 / -1 / NULL
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_msg_conv ON messages(conversation_id, id);

-- Validated NL->SQL exemplars ("skills") retrieved as few-shot context.
CREATE TABLE IF NOT EXISTS skills (
    id            INTEGER PRIMARY KEY,
    question      TEXT NOT NULL,
    canonical_sql TEXT NOT NULL,
    notes         TEXT,
    embedding     BLOB,                   -- float32 vector
    tags          TEXT,
    upvotes       INTEGER NOT NULL DEFAULT 0,
    downvotes     INTEGER NOT NULL DEFAULT 0,
    hits          INTEGER NOT NULL DEFAULT 0,
    verified      INTEGER NOT NULL DEFAULT 0,
    created_by    TEXT,
    created_at    REAL NOT NULL
);

-- Semantic cache of recent answers (reuse SQL when a near-identical Q recurs).
CREATE TABLE IF NOT EXISTS query_cache (
    id           INTEGER PRIMARY KEY,
    question     TEXT NOT NULL,
    embedding    BLOB,
    final_sql    TEXT,
    answer_md    TEXT,
    data_version INTEGER NOT NULL,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER,
    question     TEXT,
    model_used   TEXT,
    escalated    INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    ok           INTEGER,
    cached       INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_time ON usage_log(created_at);

CREATE TABLE IF NOT EXISTS import_jobs (
    id          INTEGER PRIMARY KEY,
    filename    TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|running|checks|passed|failed|swapped
    log         TEXT,
    report      TEXT,
    created_by  TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

-- Small key/value for app metadata (e.g. data_version bumped on each import).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# Ordered schema migrations, keyed by an increasing integer version tracked in
# `PRAGMA user_version`. Migration 1 is the full baseline schema — every
# statement is CREATE ... IF NOT EXISTS, so it is a safe no-op on a database that
# predates this system (it simply advances an existing db to version 1). Add each
# future schema change as a new (version, ddl) tuple with the next integer; never
# edit or renumber a shipped migration.
MIGRATIONS: list[tuple[int, str]] = [
    (1, SCHEMA),
    # Per-request OpenRouter cost (USD), for the admin spend dashboard.
    (2, "ALTER TABLE usage_log ADD COLUMN cost REAL NOT NULL DEFAULT 0;"),
]


def connect() -> sqlite3.Connection:
    s = get_settings()
    con = sqlite3.connect(str(s.app_db_path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _apply_migrations(con: sqlite3.Connection,
                      migrations: list[tuple[int, str]] = MIGRATIONS) -> int:
    """Apply every migration whose version exceeds the db's current
    `user_version`, in order, bumping `user_version` after each. Returns the
    resulting version. Idempotent: already-applied migrations are skipped."""
    current = con.execute("PRAGMA user_version").fetchone()[0]
    for version, ddl in sorted(migrations):
        if version > current:
            con.executescript(ddl)
            # user_version can't be parameterized; version is our own trusted int.
            con.execute(f"PRAGMA user_version = {int(version)}")
            con.commit()
            current = version
    return current


def init_db() -> None:
    """Run pending migrations (idempotent) and bootstrap admins + data_version."""
    s = get_settings()
    s.app_db_path.parent.mkdir(parents=True, exist_ok=True)
    con = connect()
    try:
        _apply_migrations(con)
        # data_version starts at 1 (bumped by each successful import swap)
        con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('data_version', '1')")
        # Bootstrap admin accounts + allowlist from ADMIN_EMAILS.
        now = time.time()
        for email in s.admin_email_list:
            con.execute(
                "INSERT INTO allowlist(email, note, added_by, added_at) "
                "VALUES (?, 'bootstrap admin', 'system', ?) "
                "ON CONFLICT(email) DO NOTHING", (email, now))
            con.execute(
                "INSERT INTO users(email, is_admin, created_at) VALUES (?, 1, ?) "
                "ON CONFLICT(email) DO UPDATE SET is_admin=1", (email, now))
        con.commit()
    finally:
        con.close()


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO meta(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def data_version(con: sqlite3.Connection) -> int:
    return int(get_meta(con, "data_version", "1"))
