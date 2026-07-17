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
# `replace` gets a negative lookahead so the REPLACE(...) scalar string
# function is allowed while `REPLACE INTO` / `INSERT OR REPLACE` DML still
# trip the `insert`/other alternatives (and are also blocked by the
# must-start-with-SELECT/WITH gate).
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|"
    r"pragma|vacuum|reindex|analyze|begin|commit|rollback|savepoint)\b"
    r"|\breplace\b(?!\s*\()",
    re.IGNORECASE,
)
_LIMIT_RE = re.compile(r"\blimit\b", re.IGNORECASE)
# Matches a whole single-quoted SQL string literal, honoring the doubled-quote
# ('') escape -- used only to build a masked *scan* copy for the safety
# checks below; the executed SQL is never altered.
_STRING_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")


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


def _mask_string_literals(sql: str) -> str:
    """Build a *scan* copy with the contents of single-quoted string literals
    blanked out, so the ';' and forbidden-keyword safety checks can't be
    fooled by text that only appears inside a literal (e.g. `LIKE '%update%'`
    or `SELECT 'a;b'`). Only characters strictly between a matched pair of
    single quotes are ever touched -- an unmatched/unbalanced quote (e.g. an
    injection attempt like `SELECT 1'; DROP TABLE t`) has no closing partner
    to pair with, so the regex won't match it and the trailing text stays
    fully visible to the scan. The doubled-quote escape (`'it''s'`) is
    honored so it doesn't prematurely end the literal.

    This never touches the SQL that actually gets executed -- callers must
    keep using the original `cleaned` string for that.
    """
    return _STRING_LITERAL_RE.sub(lambda m: "'" + ("#" * (len(m.group(0)) - 2)) + "'", sql)


def validate_sql(sql: str) -> str:
    """Return a cleaned, single read-only SELECT/WITH statement or raise."""
    cleaned = _strip_sql(sql)
    if not cleaned:
        raise SQLValidationError("Empty query.")
    scan = _mask_string_literals(cleaned)
    if ";" in scan:
        raise SQLValidationError("Only a single statement is allowed (no ';').")
    low = cleaned.lstrip("(").lower()
    if not (low.startswith("select") or low.startswith("with")):
        raise SQLValidationError("Only SELECT / WITH queries are allowed.")
    if _FORBIDDEN.search(scan):
        raise SQLValidationError("Query contains a forbidden (write/DDL) keyword.")
    return cleaned


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=1.0)
    con.execute("PRAGMA query_only = ON")
    return con


def ipeds_years(db_path: Path | None = None) -> list[int]:
    """Ending years present in ipeds.db, or [] when there's no dataset yet
    (file missing / unreadable / no `_years` table). A non-raising probe for
    the fresh-deploy "no data" state -- never creates the file (mode=ro), and
    never lets a corrupt/garbage file bubble up as an exception."""
    db_path = get_settings().ipeds_db_path if db_path is None else db_path
    if not Path(db_path).exists():
        return []
    con = None
    try:
        con = _connect_ro(db_path)
        rows = con.execute("SELECT year FROM _years ORDER BY year").fetchall()
        return [r[0] for r in rows]
    except sqlite3.Error:
        # Covers OperationalError ("unable to open database file", "no such
        # table: _years") and DatabaseError (a 0-byte/garbage non-sqlite file).
        return []
    finally:
        if con is not None:
            con.close()


def has_ipeds_data(db_path: Path | None = None) -> bool:
    return bool(ipeds_years(db_path))


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
    # Pre-flight aggregation lint (advisory): flag the IPEDS rollup/hang
    # foot-guns so the model can reconsider before a wrong number is returned.
    # Imported locally to avoid an import cycle (sqllint reuses helpers here).
    from app.tools.sqllint import lint_sql
    for finding in lint_sql(cleaned):
        notes.append(f"⚠ AGGREGATION CHECK ({finding.code}): {finding.message}")

    con = _connect_ro(db_path)
    timed_out = threading.Event()
    done = threading.Event()
    # Serializes the watchdog's con.interrupt() against the main thread's
    # con.close() below -- without this, a timer firing at the same instant
    # the query finishes could call interrupt() on an already-closing/closed
    # connection.
    lock = threading.Lock()

    def _watchdog():
        with lock:
            if done.is_set():
                return
            timed_out.set()
            con.interrupt()

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
        with lock:
            done.set()
        con.close()

    return QueryResult(
        columns=columns, rows=rows, truncated=truncated,
        row_count=len(rows), sql=cleaned, notes=notes,
    )
