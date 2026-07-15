"""Persistent, searchable store of server log records for the admin UI.

A single logging.Handler is attached to the root logger at startup. Each record
is redacted (secrets scrubbed) and written to a small DEDICATED SQLite database
(logs.db) so the admin console can browse, substring-search, and date-range
server activity that SURVIVES restarts — replacing the old in-memory ring buffer
that reset on every boot.

Security: the Logs view is readable by any admin, so we never retain records
that can carry secrets — the mailer logs full email bodies (incl. magic-link
tokens) in dev mode, and any message may embed a `token=`/`Bearer` value. The
mail logger is dropped entirely and token-like substrings are redacted before a
record is ever stored.

The store is a separate file (not app.db) so high-frequency log writes don't
contend with app state or bloat its backups; logs are non-precious and expire on
their own (LOG_RETENTION_DAYS).
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path
from threading import Lock

from app.config import get_settings

_handler: SqliteLogHandler | None = None

_EXCLUDED_LOGGERS = ("ipeds.mail",)
_REDACT_RE = re.compile(r"(?i)(token=|bearer\s+|sk-or-[\w-]{0,4})[\w.\-]+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  ts    REAL NOT NULL,
  level TEXT,
  name  TEXT,
  msg   TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);
"""

_PRUNE_EVERY = 500  # emit() calls between retention sweeps


def _like_escape(s: str) -> str:
    """Escape LIKE wildcards so a user's search is treated literally."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SqliteLogHandler(logging.Handler):
    """Logging handler that persists redacted records to a SQLite file.

    One connection is shared across threads (check_same_thread=False) and every
    access is guarded by a lock, since a sqlite3 connection is not safe for
    concurrent use. Writes commit immediately (WAL, synchronous=NORMAL) so
    records are durable across a restart.
    """

    def __init__(self, db_path: str | Path, retention_days: int = 30):
        super().__init__()
        self._retention_days = retention_days
        self._lock = Lock()
        self._since_prune = 0
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(db_path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.execute("PRAGMA busy_timeout=3000")
        self._con.executescript(_SCHEMA)
        self._con.commit()
        self._prune()  # sweep stale rows on boot

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_EXCLUDED_LOGGERS):
            return
        try:
            msg = _REDACT_RE.sub(r"\1<redacted>", record.getMessage())
        except Exception:  # noqa: BLE001 — logging must never raise
            return
        try:
            with self._lock:
                self._con.execute(
                    "INSERT INTO logs(ts, level, name, msg) VALUES (?,?,?,?)",
                    (record.created, record.levelname, record.name, msg))
                self._con.commit()
                self._since_prune += 1
                if self._since_prune >= _PRUNE_EVERY:
                    self._prune()
        except Exception:  # noqa: BLE001 — a log write must never crash a request
            return

    def _prune(self) -> None:
        """Delete records past the retention window. The caller holds the lock
        (or we're in __init__, which is single-threaded)."""
        if self._retention_days and self._retention_days > 0:
            cutoff = time.time() - self._retention_days * 86400
            self._con.execute("DELETE FROM logs WHERE ts < ?", (cutoff,))
            self._con.commit()
        self._since_prune = 0

    def records(self, limit: int = 200, level: str | None = None,
                q: str | None = None, since: float | None = None,
                until: float | None = None) -> list[dict]:
        """Return matching records, oldest-first (newest at the bottom, as the
        UI renders them). Filters: exact `level`, case-insensitive substring `q`
        over the message, and a `[since, until]` epoch-seconds window."""
        clauses: list[str] = []
        params: list = []
        if level:
            clauses.append("level = ?")
            params.append(level.upper())
        if q:
            clauses.append("msg LIKE ? ESCAPE '\\'")
            params.append(f"%{_like_escape(q)}%")
        if since is not None:
            clauses.append("ts >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("ts <= ?")
            params.append(float(until))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(int(limit), 2000))
        sql = f"SELECT ts, level, name, msg FROM logs{where} ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._con.execute(sql, (*params, limit)).fetchall()
        # Selected newest-first for the LIMIT; hand back chronological order.
        return [dict(r) for r in reversed(rows)]


def install(db_path: str | Path | None = None,
            retention_days: int | None = None) -> SqliteLogHandler:
    """Attach the persistent log store to the root logger (idempotent)."""
    global _handler
    if _handler is None:
        s = get_settings()
        _handler = SqliteLogHandler(
            db_path if db_path is not None else s.resolved_log_db_path,
            retention_days if retention_days is not None else s.log_retention_days)
        _handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(_handler)
    return _handler


def get_handler() -> SqliteLogHandler | None:
    return _handler
