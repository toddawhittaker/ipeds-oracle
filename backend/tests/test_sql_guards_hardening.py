"""Hardening / characterization tests for the read-only SQL sandbox.

Extends backend/tests/test_sql_guards.py with:
  1. MUST-REJECT probes: validator-bypass attempts (ATTACH/DETACH/PRAGMA
     variants, multi-statement smuggling, comment tricks, side-effecting
     SQLite functions not in the forbidden keyword list). Where a probe
     slips past `validate_sql`, we fall through to `run_sql` and demonstrate
     the read-only + immutable connection still prevents any side effect —
     that is the actual security boundary; the regex is defense in depth.
  2. MUST-ACCEPT probes: legitimate read-only queries that must not be
     rejected. Several of these are KNOWN, PINNED DEFECTS (false positives)
     in the current `_FORBIDDEN` regex / `;` check, which operate on the raw
     SQL text rather than a parsed statement and so also match inside string
     literals. They are intentionally left failing (RED) per TDD — do not
     "fix" them by weakening the assertions. See the summary at the bottom
     for exact classification + recommended fix.

No API key required. Run with:
    /home/todd/projects/ipeds/.venv/bin/python backend/tests/test_sql_guards_hardening.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.tools.sql import (
    SQLValidationError,
    _connect_ro,
    run_sql,
    validate_sql,
)

DB_PATH = get_settings().ipeds_db_path

passed = []
failed = []


def record(ok: bool, label: str, detail: str = "") -> None:
    if ok:
        passed.append(label)
        print(f"  ✓ {label}" + (f" ({detail})" if detail else ""))
    else:
        failed.append(label)
        print(f"  ✗ FAIL: {label}" + (f" -- {detail})" if detail else ""))


def must_reject(sql: str, label: str | None = None) -> None:
    label = label or sql[:70]
    try:
        validate_sql(sql)
        record(False, label, f"NOT REJECTED: {sql!r}")
    except SQLValidationError as e:
        record(True, label, f"rejected: {e}")


def must_accept_validate(sql: str, label: str | None = None) -> None:
    """Assert validate_sql does NOT raise (a false-positive probe)."""
    label = label or sql[:70]
    try:
        validate_sql(sql)
        record(True, label)
    except SQLValidationError as e:
        record(False, label, f"WRONGLY REJECTED {sql!r} -> {e}")


# ============================================================
print("== 1. MUST-REJECT: validator-bypass / dangerous-input probes ==")
# ============================================================

print("\n-- extended write/DDL/DML keyword coverage --")
must_reject("DETACH DATABASE main", "DETACH")
must_reject("REPLACE INTO hd (unitid) VALUES (1)", "REPLACE INTO (DML)")
must_reject("INSERT OR REPLACE INTO hd (unitid) VALUES (1)", "INSERT OR REPLACE")
must_reject("VACUUM INTO '/tmp/pwned.db'", "VACUUM INTO (writes a file)")
must_reject("CREATE TABLE evil AS SELECT * FROM hd", "CREATE TABLE AS SELECT")
must_reject("SAVEPOINT x", "SAVEPOINT")
must_reject("ROLLBACK", "ROLLBACK")

print("\n-- ATTACH / PRAGMA / multi-statement --")
must_reject("ATTACH DATABASE 'x' AS y", "ATTACH (already covered, re-pinned)")
must_reject("PRAGMA table_info(c_a)", "PRAGMA statement (already covered, re-pinned)")
must_reject("SELECT * FROM hd; ATTACH DATABASE 'evil' AS y",
            "multi-statement: SELECT then ATTACH")
must_reject("SELECT 1; DROP TABLE c_a", "multi-statement: SELECT then DROP")

print("\n-- comment tricks trying to smuggle a forbidden statement --")
# Comment-split keyword: SQLite itself would tokenize ATTA / CH as two
# separate (invalid) tokens, so this was never executable as ATTACH -- but
# confirm our validator rejects it too, and via which check.
must_reject("ATTA/**/CH DATABASE 'x' AS y",
            "comment-split ATTACH (also fails 'must start with SELECT')")
# Nested block comment: the non-greedy comment-stripper closes at the FIRST
# '*/', so the "DROP TABLE" text survives stripping and remains visible to
# the forbidden-keyword scan.
must_reject("/* /* nested */ DROP TABLE c_a */ SELECT 1",
            "nested block comment leaking DROP TABLE text")
# Hidden semicolon inside a line comment -- must not resurrect a 2nd statement.
ok_after_strip = "SELECT 1 -- ; DROP TABLE c_a\n"
try:
    cleaned = validate_sql(ok_after_strip)
    record(cleaned == "SELECT 1",
           "line-comment-hidden '; DROP TABLE' safely stripped, not executed",
           f"cleaned={cleaned!r}")
except SQLValidationError as e:
    record(False, "line-comment-hidden '; DROP TABLE' safely stripped, not executed", str(e))

print("\n-- side-effecting SQLite functions NOT in the forbidden keyword list --")
print("   (validator may accept these; the read-only+immutable connection must still block them)")


def probe_function_is_neutralized(sql: str, label: str, expect_error_substr: str) -> None:
    """A function-call attack that `validate_sql` may not catch by name.
    We require the DB layer to neutralize it -- either it fails outright
    (no such function / not authorized) or, if it somehow succeeds, that no
    file was actually written / no state actually changed. Any clean success
    with the intended side effect achieved is treated as a genuine finding.
    """
    try:
        validate_sql(sql)
    except SQLValidationError as e:
        record(True, f"{label}: rejected at validate_sql layer ({e})")
        return
    # It slipped past validate_sql -- the connection-level guarantee must hold.
    try:
        run_sql(sql, timeout=5)
        record(False, f"{label}: validate_sql accepted it AND run_sql executed it (SECURITY HOLE)")
    except Exception as e:
        ok = expect_error_substr.lower() in str(e).lower()
        record(ok, f"{label}: validate_sql accepted (gap) but run_sql blocked it "
                   f"-- {type(e).__name__}: {e}")


probe_function_is_neutralized(
    "SELECT load_extension('/tmp/whatever.so')", "load_extension()", "not authorized")
probe_function_is_neutralized(
    "SELECT writefile('/tmp/pwned_by_sql_sandbox.txt', 'pwned')", "writefile()", "no such function")
probe_function_is_neutralized(
    "SELECT readfile('/etc/passwd')", "readfile()", "no such function")

print("\n-- pragma_*() table-valued functions --")
# These used to be ACCEPTED, and this block used to assert that the gap was
# harmless: `\bpragma\b` does not match `pragma_table_info`, because `_` is a
# word character and so there is no boundary there. The old reasoning was that
# it is read-only introspection on a query_only/immutable connection, so no
# mutation is possible. That much was true and still is.
#
# What it missed, found by a pre-v0.5.0 security pass over the MCP endpoint --
# the first place a HUMAN writes this SQL deliberately rather than an LLM
# writing it by accident:
#
#   * `SELECT * FROM pragma_database_list` returns the server's ABSOLUTE
#     filesystem path to ipeds.db. That is host layout disclosed to anyone
#     holding an API key.
#   * `SELECT * FROM pragma_quick_check` reads the WHOLE 2.2 GB database until
#     the watchdog stops it -- a full-file I/O amplifier, once per call, eight
#     of them concurrent under MCP_TOOL_CONCURRENCY.
#
# Neither is mutation, so the old conclusion was not wrong about what it
# checked; it was answering a narrower question than the one that mattered. The
# regex now says `pragma(?!_table_info\b)\w*`, and these must be REJECTED.
for sql, label in [
    ("SELECT * FROM pragma_database_list", "pragma_database_list TVF"),
    ("SELECT * FROM pragma_quick_check", "pragma_quick_check TVF"),
    ("SELECT * FROM pragma_compile_options", "pragma_compile_options TVF"),
    ("SELECT * FROM pragma_table_list", "pragma_table_list TVF"),
    # The carve-out is exact: a name that merely STARTS with the allowed one
    # must not ride in on it.
    ("SELECT * FROM pragma_table_infoX", "pragma_table_infoX (prefix of the allowed one)"),
]:
    try:
        validate_sql(sql)
        record(False, f"{label}: rejected by validator",
               "ACCEPTED -- the pragma_ table-valued form is back through the filter")
    except SQLValidationError:
        record(True, f"{label}: rejected by validator")

# ...with ONE exception, which is a product feature rather than an oversight:
# tools/schema.py::get_columns is built on pragma_table_info, and get_columns is
# a first-class tool in the registry — it is how the agent and an MCP client
# discover a family's columns. Blocking it outright broke the registry suite,
# which is how this carve-out was found. It names columns of a table the caller
# can already read.
try:
    validate_sql("SELECT name, type FROM pragma_table_info('hd')")
    record(True, "pragma_table_info() TVF: allowed (get_columns is built on it)")
except SQLValidationError as e:
    record(False, "pragma_table_info() TVF: allowed (get_columns is built on it)",
           f"REJECTED -- get_columns is now broken: {e}")

print("\n-- Python sqlite3 driver itself refuses multi-statement `execute()` "
      "(defense-in-depth below our own ';' check) --")
con = _connect_ro(DB_PATH)
try:
    con.execute("SELECT 1; SELECT 2")
    record(False, "Connection.execute() single-statement enforcement",
           "allowed a multi-statement string!")
except Exception as e:
    record("one statement at a time" in str(e).lower(),
           "Connection.execute() single-statement enforcement", f"{type(e).__name__}: {e}")
finally:
    con.close()


# ============================================================
print("\n== 2. MUST-ACCEPT: legitimate read-only queries must not be rejected ==")
# ============================================================

print("\n-- forbidden-keyword substrings inside string literals (regex scans raw text) --")
must_accept_validate(
    "SELECT instnm FROM hd WHERE instnm LIKE '%update%'",
    "LIKE '%update%' (word 'update' in a literal)")
must_accept_validate(
    "SELECT instnm FROM hd WHERE instnm LIKE '%Delete%'",
    "LIKE '%Delete%' (word 'delete' in a literal)")
must_accept_validate(
    "SELECT instnm FROM hd WHERE instnm LIKE '%Create%College%'", "LIKE '%Create%College%'")
must_accept_validate(
    "SELECT instnm FROM hd WHERE instnm LIKE '%Commit%'",
    "LIKE '%Commit%' (word 'commit' in a literal)")

print("\n-- semicolon inside a string literal (naive ';' membership check) --")
must_accept_validate("SELECT 'a;b' AS x", "string literal containing ';'")

print("\n-- REPLACE() the builtin scalar string function (not REPLACE INTO the DML verb) --")
must_accept_validate(
    "SELECT REPLACE(instnm, 'University', 'Univ') AS short_name FROM hd LIMIT 5",
    "REPLACE(...) scalar function")

print("\n-- sanity: legitimate query with these accepted really executes and returns real data --")
try:
    r = run_sql("SELECT instnm FROM hd WHERE instnm LIKE '%Delete%' LIMIT 5")
    record(True, "run_sql executes the LIKE '%Delete%' query successfully",
           f"{r.row_count} row(s)")
except SQLValidationError as e:
    record(False, "run_sql executes the LIKE '%Delete%' query successfully",
           f"blocked before it ever reached the DB: {e}")


# ============================================================
print("\n" + "=" * 70)
print(f"PASSED: {len(passed)}   FAILED (pinned defects / regressions): {len(failed)}")
if failed:
    print("\nFailing checks:")
    for f in failed:
        print(f"  - {f}")
print("=" * 70)

if failed:
    sys.exit(1)
print("\nALL SQL-GUARD HARDENING TESTS PASSED")
