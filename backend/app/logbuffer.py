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
their own, bounded two ways: LOG_RETENTION_DAYS (how far back they reach) and
LOG_MAX_ROWS (how many they may total). Age alone is unbounded within its window
— a log storm can run the file away long before a 30-day sweep would notice — so
the cap is what actually makes the size predictable. Freed pages are handed back
to the filesystem via incremental auto-vacuum; a bare DELETE would leave the file
at its high-water mark forever.
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

    def __init__(self, db_path: str | Path, retention_days: int = 30,
                 max_rows: int = 0):
        super().__init__()
        self._retention_days = retention_days
        self._max_rows = max_rows
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
        self._enable_incremental_autovacuum()
        self._prune()  # sweep stale rows on boot

    def _enable_incremental_autovacuum(self) -> None:
        """Make freed pages actually return to the filesystem.

        A plain DELETE only marks pages reusable, so the file keeps its
        high-water mark forever — prune as much as you like and logs.db never
        gets smaller. The obvious fix, VACUUM after each prune, rewrites the
        WHOLE file; under the row cap that would fire on essentially every
        prune (steady state = always at the ceiling), so it's the wrong tool.
        Incremental auto-vacuum instead reclaims just the pages a prune freed.

        auto_vacuum can only be changed on an existing database by a full
        VACUUM rewrite, so pay that once, here, on a DB created before this
        setting existed. isolation_level=None is required: VACUUM cannot run
        inside the implicit transaction sqlite3 would otherwise open.
        """
        try:
            if self._con.execute("PRAGMA auto_vacuum").fetchone()[0] == 2:
                return  # already INCREMENTAL — nothing to convert
            prev = self._con.isolation_level
            self._con.isolation_level = None
            try:
                self._con.execute("PRAGMA auto_vacuum=INCREMENTAL")
                self._con.execute("VACUUM")
            finally:
                self._con.isolation_level = prev
        except sqlite3.Error:
            # Reclaiming space is an optimization, never a reason to lose
            # logging. Fall back to the old grow-only behavior.
            pass

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
        """Drop records past the retention window AND past the row ceiling,
        then hand the freed pages back to the filesystem. Whichever limit bites
        first wins: age bounds how far back logs reach, the cap bounds how big
        they get inside that window. The caller holds the lock (or we're in
        __init__, which is single-threaded)."""
        deleted = 0
        if self._retention_days and self._retention_days > 0:
            cutoff = time.time() - self._retention_days * 86400
            deleted += self._con.execute(
                "DELETE FROM logs WHERE ts < ?", (cutoff,)).rowcount
        if self._max_rows and self._max_rows > 0:
            # Keep the newest _max_rows by id: find the id of the _max_rows-th
            # newest row and drop everything strictly older.
            #
            # OFFSET, not `id <= max(id) - _max_rows`: ids come from AUTOINCREMENT
            # and gap after every prune, so arithmetic on max(id) drifts and would
            # delete far more than intended once gaps accumulate. With fewer than
            # _max_rows rows the subquery yields no row -> NULL -> `id < NULL` is
            # NULL, never true, so nothing is deleted. That's the desired no-op.
            deleted += self._con.execute(
                "DELETE FROM logs WHERE id < "
                "(SELECT id FROM logs ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (self._max_rows - 1,)).rowcount
        self._con.commit()
        self._since_prune = 0
        if deleted > 0:
            self._reclaim()

    def _reclaim(self) -> None:
        """Return pages freed by the prune to the filesystem. Cheap: touches
        only the freelist, not the whole file (see _enable_incremental_autovacuum).
        A no-op when auto_vacuum isn't INCREMENTAL, which is the fallback if the
        one-time conversion failed."""
        try:
            # .fetchall() is LOad-BEARING, not defensive tidiness: this pragma
            # frees one page per sqlite3_step, and execute() steps exactly once.
            # Without draining it, each prune returns a single page while the
            # freelist grows forever — measurably 46 pages/40 free vs 6/0 on the
            # same data. It looks like it works, which is what makes it nasty.
            self._con.execute("PRAGMA incremental_vacuum").fetchall()
            self._con.commit()
        except sqlite3.Error:
            pass  # space reclamation must never break logging

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
            retention_days: int | None = None,
            max_rows: int | None = None) -> SqliteLogHandler:
    """Attach the persistent log store to the root logger (idempotent)."""
    global _handler
    if _handler is None:
        s = get_settings()
        _handler = SqliteLogHandler(
            db_path if db_path is not None else s.resolved_log_db_path,
            retention_days if retention_days is not None else s.log_retention_days,
            max_rows if max_rows is not None else s.log_max_rows)
        _handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(_handler)
    return _handler


def get_handler() -> SqliteLogHandler | None:
    return _handler
