"""Safe, read-only execution of model-generated SQL against ipeds.db.

The model can *never* mutate the database and can *never* hang a worker:
  * the connection is opened read-only + immutable, with PRAGMA query_only;
  * only a single SELECT / WITH statement is accepted (no DDL/DML/PRAGMA/ATTACH);
  * a watchdog thread calls connection.interrupt() after a timeout — this is the
    programmatic equivalent of the CLAUDE.md `timeout 30 sqlite3 …` rule and
    defuses the known "recent-N-years JOIN full-scans c_a and hangs" foot-gun.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings

# Statements that must never appear (defense in depth on top of the RO handle).
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
    r"pragma|vacuum|reindex|analyze|begin|commit|rollback|savepoint)\b",
    re.IGNORECASE,
)
_LIMIT_RE = re.compile(r"\blimit\b", re.IGNORECASE)


class SQLValidationError(ValueError):
    """Raised when the SQL is rejected before it ever touches the database."""


class SQLTimeoutError(RuntimeError):
    """Raised when a query exceeds the configured timeout and is interrupted."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool = False
    row_count: int = 0
    sql: str = ""
    notes: list[str] = field(default_factory=list)

    def to_markdown(self, max_rows: int = 50) -> str:
        if not self.columns:
            return "_(no columns)_"
        if not self.rows:
            return "_(0 rows)_"
        head = self.rows[:max_rows]
        out = ["| " + " | ".join(self.columns) + " |",
               "| " + " | ".join("---" for _ in self.columns) + " |"]
        for r in head:
            out.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
        if len(self.rows) > max_rows:
            out.append(f"\n_…{len(self.rows) - max_rows} more rows_")
        return "\n".join(out)


def _strip_sql(sql: str) -> str:
    """Remove comments and a single trailing semicolon; return trimmed SQL."""
    # strip /* */ block comments and -- line comments
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql.strip().rstrip(";").strip()


def validate_sql(sql: str) -> str:
    """Return a cleaned, single read-only SELECT/WITH statement or raise."""
    cleaned = _strip_sql(sql)
    if not cleaned:
        raise SQLValidationError("Empty query.")
    if ";" in cleaned:
        raise SQLValidationError("Only a single statement is allowed (no ';').")
    low = cleaned.lstrip("(").lower()
    if not (low.startswith("select") or low.startswith("with")):
        raise SQLValidationError("Only SELECT / WITH queries are allowed.")
    if _FORBIDDEN.search(cleaned):
        raise SQLValidationError("Query contains a forbidden (write/DDL) keyword.")
    return cleaned


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=1.0)
    con.execute("PRAGMA query_only = ON")
    return con


def run_sql(sql: str, *, limit: int | None = None,
            timeout: float | None = None,
            db_path: Path | None = None) -> QueryResult:
    """Execute a validated read-only query with a hard timeout + row cap.

    `limit` caps the rows returned (default: settings.sql_row_cap_model). If the
    query has no LIMIT of its own, we don't rewrite it — we fetch up to limit+1
    rows and mark `truncated`, so aggregates stay correct while result sets stay
    bounded.
    """
    s = get_settings()
    limit = s.sql_row_cap_model if limit is None else limit
    timeout = s.sql_timeout_seconds if timeout is None else timeout
    db_path = s.ipeds_db_path if db_path is None else db_path

    cleaned = validate_sql(sql)
    notes: list[str] = []
    if not _LIMIT_RE.search(cleaned):
        notes.append(f"No LIMIT in query; showing at most {limit} rows.")

    con = _connect_ro(db_path)
    timed_out = threading.Event()

    def _watchdog():
        con.interrupt()
        timed_out.set()

    timer = threading.Timer(timeout, _watchdog)
    timer.start()
    try:
        cur = con.execute(cleaned)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(limit + 1)
        truncated = len(rows) > limit
        rows = rows[:limit]
    except sqlite3.OperationalError as e:
        if timed_out.is_set():
            raise SQLTimeoutError(
                f"Query exceeded {timeout:g}s and was cancelled. Simplify it or "
                "add a constant year bound (see the 'recent N years' rule)."
            ) from e
        raise
    finally:
        timer.cancel()
        con.close()

    return QueryResult(
        columns=columns, rows=rows, truncated=truncated,
        row_count=len(rows), sql=cleaned, notes=notes,
    )
