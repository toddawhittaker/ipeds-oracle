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
# ('') escape -- used to build a masked *scan* copy for the safety checks below
# AND to locate comments without mistaking a `--`/`/*` INSIDE a literal for one.
_STRING_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


class SQLValidationError(ValueError):
    """Raised when the SQL is rejected before it ever touches the database."""


class SQLTimeoutError(RuntimeError):
    """Raised when a query exceeds the configured timeout and is interrupted."""


class SQLResultTooLargeError(RuntimeError):
    """Raised when a single value in the result exceeds SQL_MAX_VALUE_BYTES."""


# Ceiling on the size of ONE value SQLite will produce (SQLITE_LIMIT_LENGTH).
#
# The row cap bounds how MANY rows come back, never how BIG one is, and that
# gap was reachable in a single query: `SELECT length(hex(zeroblob(400000000)))`
# allocated 1,155 MB RSS in 1.0 s against the real dataset, and the 25 s
# watchdog structurally cannot fire inside a one-second allocation. Multiply by
# the 200-row model cap (200 x 5 MB) or the 100k-row CSV cap and it is an
# OOM-kill of the container.
#
# 1 MiB is ~140x the largest value in the real dataset (vartable.longdescription
# at 7,469 bytes) and ~4x the widest plausible legitimate aggregate (a national
# group_concat of every institution name is ~270 KB), so nothing a real question
# asks for comes close. It does not replace the watchdog — it RESTORES it: with
# each value bounded, obtaining serious memory now takes thousands of values and
# therefore long enough for con.interrupt() to land.
#
# Deliberately a module constant, not a setting: no operator has a reason to
# raise it, and raising it re-opens the hole.
SQL_MAX_VALUE_BYTES = 1 << 20

# ...and a cumulative ceiling on the WHOLE result, because the per-value cap
# above does not bound the total.
#
# The comment above argues the value cap "restores" the watchdog: with each
# value bounded, serious memory needs thousands of values and therefore long
# enough for con.interrupt() to land. That is true at the 200-row model cap
# (200 MB, survivable) and FALSE at the 100k-row CSV download cap, which is the
# one place it was never re-checked. Measured against the real dataset with
# values UNDER the per-value cap (`SELECT hex(zeroblob(500000)) FROM c_a`):
#
#     limit=  100   0.05s   ~95 MB    1,811 MB/s
#     limit=  400   0.18s  ~382 MB    2,082 MB/s
#     limit= 1000   0.40s  ~954 MB    2,359 MB/s
#
# At ~2.3 GB/s the 25 s watchdog is irrelevant -- any realistic container limit
# is gone in well under a second, and the reachable ceiling on the download path
# is 100,000 x 1 MiB ~= 100 GB, all resident before a single CSV byte is
# written. An allowlisted user only has to steer one turn into emitting a
# large-value query (llm.py appends every emitted statement to sql_log BEFORE
# dispatch, so it persists whether or not it ran) and then request the CSV.
#
# 64 MiB is ~3x the widest plausible legitimate export (100k rows of ordinary
# IPEDS columns is ~20 MB) and small enough that the overshoot below cannot
# matter. Surfaces as the same SQLResultTooLargeError the value cap uses, so the
# model gets a steer instead of the generic handler.
SQL_MAX_RESULT_BYTES = 64 << 20

# Rows are fetched in batches so the budget is checked BEFORE the whole result
# is resident. The batch adapts: the first one is deliberately tiny (a wide row
# of capped values can be tens of MB on its own), then it is sized from the
# measured average so no single batch can overshoot by more than an eighth of
# the budget. Ordinary narrow rows reach the 1000 ceiling immediately, so a
# normal 100k-row export costs ~100 round trips, not 100k.
_FETCH_BATCH_FIRST = 8
_FETCH_BATCH_MAX = 1000


def _value_bytes(v: object) -> int:
    """Rough resident size of one cell. Only str/bytes can be large (the value
    cap bounds them at 1 MiB); everything else is a small fixed-width scalar and
    is counted as 8 so a wide all-numeric row still accrues against the budget."""
    if isinstance(v, (str, bytes, bytearray, memoryview)):
        return len(v)
    return 8


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

    def to_storage(self, max_rows: int = 200) -> dict:
        """A JSON-able snapshot (columns + up to `max_rows` rows) for persisting a
        turn's result so a LATER turn can ground a figure against it
        (app/grounding.py is conversation-scoped). Only what grounding needs —
        columns + cell values; the SQL text, notes and truncation flag are not
        reloaded. Tuples become lists (JSON has no tuple)."""
        return {"columns": list(self.columns),
                "rows": [list(r) for r in self.rows[:max_rows]]}

    @classmethod
    def from_storage(cls, data: dict) -> QueryResult:
        """Rebuild a QueryResult from to_storage() JSON. Rows stay as lists —
        grounding indexes them positionally, so tuples aren't needed. Tolerant of
        a malformed/partial blob (missing keys → empty), since it reads
        persisted data that must never break a live turn."""
        cols = list((data or {}).get("columns") or [])
        rows = [tuple(r) for r in ((data or {}).get("rows") or [])]
        return cls(columns=cols, rows=rows, row_count=len(rows))


def _strip_comments(sql: str, pattern: re.Pattern) -> str:
    """Remove every `pattern` match that is a real comment -- i.e. NOT one that
    only appears inside a single-quoted string literal. We locate the matches on
    a literal-MASKED copy (`_mask_string_literals` blanks a literal's contents
    but preserves length), so a `--`/`/*` between quotes is masked to `#` and
    never matched; the surviving spans map 1:1 onto the ORIGINAL text, which we
    splice (from the end forward so earlier indices stay valid). Each comment
    becomes a single space to keep tokens separated."""
    masked = _mask_string_literals(sql)
    for start, end in sorted((m.span() for m in pattern.finditer(masked)), reverse=True):
        sql = sql[:start] + " " + sql[end:]
    return sql


def _strip_sql(sql: str) -> str:
    """Remove comments and a single trailing semicolon; return trimmed SQL.

    Comments are stripped literal-awarely (block first, then line — mirroring
    the old two-pass order, so a `--` inside a `/* ... */` block is gone before
    the line pass runs) so a string literal like `'2020--Q1'` survives verbatim
    in the SQL that actually executes (SEC-5)."""
    sql = _strip_comments(sql, _BLOCK_COMMENT_RE)
    sql = _strip_comments(sql, _LINE_COMMENT_RE)
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
    # Bound the size of any single value the query can produce. See
    # SQL_MAX_VALUE_BYTES — this is what keeps one crafted expression from
    # allocating a gigabyte faster than the timeout watchdog can interrupt it.
    con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, SQL_MAX_VALUE_BYTES)
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


def run_sql(sql: str, *, params: tuple | list = (), limit: int | None = None,
            timeout: float | None = None,
            db_path: Path | None = None) -> QueryResult:
    """Execute a validated read-only query with a hard timeout + row cap.

    `params` are bound positionally (`?` placeholders) so caller-supplied values
    are never string-interpolated into SQL. `limit` caps the rows returned
    (default: settings.sql_row_cap_model). If the query has no LIMIT of its own,
    we don't rewrite it — we fetch up to limit+1 rows and mark `truncated`, so
    aggregates stay correct while result sets stay bounded.
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
        cur = con.execute(cleaned, params)
        columns = [d[0] for d in cur.description] if cur.description else []
        # One row past the cap is how truncation is detected; keep that.
        want = limit + 1
        rows: list = []
        seen = 0
        batch_n = _FETCH_BATCH_FIRST
        while len(rows) < want:
            batch = cur.fetchmany(min(batch_n, want - len(rows)))
            if not batch:
                break
            seen += sum(_value_bytes(v) for row in batch for v in row)
            if seen > SQL_MAX_RESULT_BYTES:
                raise SQLResultTooLargeError(
                    f"The result exceeded {SQL_MAX_RESULT_BYTES // (1 << 20)} MiB "
                    "in total. Aggregate it in SQL (count/sum/avg), select fewer "
                    "columns, or narrow the query with a WHERE clause."
                )
            rows.extend(batch)
            # Size the next batch from the measured average so a wide result
            # cannot overshoot the budget by more than an eighth of it.
            avg = max(seen // len(rows), 1)
            batch_n = max(1, min(_FETCH_BATCH_MAX,
                                 (SQL_MAX_RESULT_BYTES // 8) // avg))
        truncated = len(rows) > limit
        rows = rows[:limit]
    except sqlite3.DataError as e:
        # SQLITE_LIMIT_LENGTH surfaces as DataError ("string or blob too big"),
        # NOT OperationalError — so it needs its own branch or it falls through
        # to registry's generic handler and the model gets no steer about what
        # to do differently.
        raise SQLResultTooLargeError(
            f"A single value in the result exceeded "
            f"{SQL_MAX_VALUE_BYTES // (1 << 20)} MiB. Aggregate it in SQL "
            "(count/sum/avg) or trim it with substr(), rather than selecting "
            "the whole value."
        ) from e
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

    # Truncation is an AGGREGATION foot-gun, not just a display cap: summing/
    # counting/averaging a CUT page as a TOTAL yields a wrong number whose SQL
    # looks perfect (and which grounding would "validate" — the same partial
    # rows recompute the same wrong total). Raise the SAME ⚠ AGGREGATION CHECK
    # marker the rollup lints use (sql.py above) so prompt step 3's "treat as
    # blocking, fix and re-run" instruction fires on it too. Distinct from the
    # "No LIMIT…" note above, which flags a missing LIMIT whether or not the
    # result actually overflowed. Appended here, not in the pre-flight block,
    # because `truncated` is only known after execution.
    if truncated:
        notes.append(
            f"⚠ AGGREGATION CHECK (truncated): this result was CUT to {limit} rows "
            "— it is NOT the full result set. Do NOT sum/count/average these rows "
            "as a TOTAL. Aggregate in SQL (SUM/COUNT/AVG), add a tighter filter, "
            "or bound the query so the whole result fits, then re-run. "
            "EXCEPTION — if the user asked for a LISTING or ranking (not an "
            f"aggregate): you MAY present these as the first {limit} rows, but you "
            "MUST state the full row count (run SELECT COUNT(*)) and tell the user "
            "the complete data can be downloaded in full below the table (do NOT "
            "name or quote the download button — a control already sits there).")

    return QueryResult(
        columns=columns, rows=rows, truncated=truncated,
        row_count=len(rows), sql=cleaned, notes=notes,
    )
