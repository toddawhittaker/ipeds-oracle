"""Smoke test for the read-only SQL engine: guards, a real query, and timeout."""
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.tools.sql import (
    SQL_MAX_RESULT_BYTES,
    SQL_MAX_VALUE_BYTES,
    SQLResultTooLargeError,
    SQLTimeoutError,
    SQLValidationError,
    _connect_ro,
    has_ipeds_data,
    ipeds_years,
    run_sql,
    validate_sql,
)


def expect_reject(sql):
    try:
        validate_sql(sql)
    except SQLValidationError as e:
        print(f"  ✓ rejected: {sql[:50]!r} -> {e}")
        return
    print(f"  ✗ NOT REJECTED (bad!): {sql[:60]!r}")
    sys.exit(1)

print("== validation guards ==")
for bad in [
    "DELETE FROM c_a",
    "INSERT INTO hd VALUES (1)",
    "DROP TABLE c_a",
    "SELECT 1; SELECT 2",
    "ATTACH DATABASE 'x' AS y",
    "PRAGMA table_info(c_a)",
    "UPDATE hd SET instnm='x'",
    "",
]:
    expect_reject(bad)
# these should pass validation
for ok in ["SELECT 1", "  with x as (select 1) select * from x  ;",
           "SELECT COUNT(*) FROM c_a -- comment\n"]:
    validate_sql(ok)
    print(f"  ✓ accepted: {ok.strip()[:50]!r}")

print("\n== real query: national associate's per year (should be ~1M) ==")
r = run_sql(
    "SELECT year, SUM(ctotalt) AS associates FROM c_a "
    "WHERE awlevel=3 AND majornum=1 AND cipcode='99' GROUP BY year ORDER BY year")
print(r.to_markdown())
assert r.rows, "no rows returned"
latest = r.rows[-1][1]
assert 500_000 < latest < 1_500_000, f"associates={latest} out of sane range"
print(f"  ✓ latest associates={latest:,} (sane)")

print("\n== timeout watchdog (expensive cross join, cap 2s) ==")
t0 = time.time()
try:
    run_sql("SELECT COUNT(*) FROM c_a a, c_a b, c_a c", timeout=2.0)
    print("  ✗ expected timeout")
    sys.exit(1)
except SQLTimeoutError as e:
    dt = time.time() - t0
    print(f"  ✓ interrupted after {dt:.1f}s: {e}")
    assert dt < 6, "watchdog did not fire promptly"

print("\n== single-value size cap (memory exhaustion, SEC review 2026-07-26) ==")
# THE REGRESSION: the row cap bounds how MANY rows come back, never how BIG one
# is. `SELECT length(hex(zeroblob(400000000)))` allocated 1,155 MB RSS in 1.0s
# against the real dataset -- and the 25s watchdog cannot fire inside a
# one-second allocation, so nothing stopped it. With the 200-row model cap
# (200 x 5 MB) or the 100k-row CSV cap it is an OOM-kill of the container.
t0 = time.time()
try:
    run_sql("SELECT length(hex(zeroblob(400000000)))", timeout=25.0)
    print("  ✗ an oversized single value was NOT refused")
    sys.exit(1)
except SQLResultTooLargeError as e:
    dt = time.time() - t0
    print(f"  ✓ oversized value refused in {dt:.2f}s: {str(e)[:60]}…")
    # Refused by the LIMIT, not by the watchdog. If this ever takes ~25s the
    # cap stopped working and the timeout is doing the job instead — which is
    # exactly the state that could not stop the 1.0s allocation.
    assert dt < 5, f"refusal took {dt:.1f}s — that is the watchdog, not the cap"

# The other half of the bound, and the reason it isn't set tighter: a cap that
# broke a real aggregate would be a silent data-availability bug, not a fix.
# ~800 KB is well past anything IPEDS holds (the largest real value is
# vartable.longdescription at 7,469 bytes) and still returns.
r = run_sql("SELECT length(hex(zeroblob(400000))) AS n")
assert r.rows[0][0] == 800000, f"a large-but-legitimate value must survive, got {r.rows}"
print(f"  ✓ a large but legitimate value still returns ({r.rows[0][0]:,} chars)")

# The limit has to live on the connection the queries actually run on. A
# refactor that builds a connection some other way would silently drop it.
# Resolve the path from settings, never a literal "ipeds.db": CI runs against a
# fixture DB via IPEDS_DB_PATH, so a hardcoded relative path is the classic
# passes-locally / fails-in-CI shape.
_lim = _connect_ro(get_settings().ipeds_db_path).getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
assert _lim == SQL_MAX_VALUE_BYTES, f"read-only connection lost the cap: {_lim}"
print(f"  ✓ read-only connections carry the cap ({_lim:,} bytes)")

print("\n== whole-result byte budget: the per-value cap does not bound the TOTAL ==")
# THE REGRESSION: SQL_MAX_VALUE_BYTES bounds ONE value at 1 MiB, and the row cap
# bounds how MANY rows come back -- but nothing bounded the product. The CSV
# download path runs at sql_row_cap_download (100,000), so the reachable ceiling
# was 100k x 1 MiB ~= 100 GB, all resident before a single CSV byte is written.
#
# The value cap's own comment argues it "restores" the watchdog because serious
# memory then needs thousands of values and therefore enough TIME for
# con.interrupt() to land. Measured against the real dataset with values UNDER
# the cap, that is true at the 200-row model cap and false at the download cap:
# ~2.3 GB/s (1000 rows of 1 MB in 0.40s), so any realistic container limit is
# gone in well under a second and the 25 s watchdog never fires.
#
# A recursive CTE rather than a real table, so this holds on CI's tiny fixture
# DB as well as the full dataset -- the rows are generated, not stored.
_rows_needed = (SQL_MAX_RESULT_BYTES // 1_000_000) + 8
_fat = (f"WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<{_rows_needed}) "
        "SELECT hex(zeroblob(500000)) AS v FROM n")
_t0 = time.time()
try:
    _r = run_sql(_fat, limit=100_000)
    raise AssertionError(
        f"a {_rows_needed}-row x 1 MB result was returned whole "
        f"(~{_rows_needed} MB) -- the whole-result budget did not fire")
except SQLResultTooLargeError as e:
    _dt = time.time() - _t0
    assert "MiB" in str(e), f"the error must name the ceiling, got: {e}"
    print(f"  ✓ a result over {SQL_MAX_RESULT_BYTES // (1 << 20)} MiB is refused "
          f"({_dt:.2f}s) -> {str(e)[:60]}...")

# ...and the budget must not break an ordinary export. Well under the ceiling,
# many rows: this is the shape a real 100k-row CSV has, and it must still work.
_r = run_sql("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<5000) "
             "SELECT i, hex(zeroblob(20)) AS v FROM n", limit=100_000)
assert len(_r.rows) == 5000, f"an ordinary wide-ish export must survive, got {len(_r.rows)}"
assert not _r.truncated, "5000 rows under a 100k cap must not report truncated"
print(f"  ✓ an ordinary {len(_r.rows):,}-row export is unaffected")

# THE SECOND-PASS REGRESSIONS. Both were found by review AFTER the byte budget
# shipped, and both defeated it while it was "working": the budget refused the
# query, but only once the memory had already been allocated.
#
# (a) ROW WIDTH. Nothing bounded one row (n_columns x 1 MiB). Measured on the
#     real dataset with 500 columns of hex(zeroblob(500000)): 2,185 MB peak at
#     limit=1 and 5,046 MB at limit=200 -- and the 25 s watchdog cannot fire
#     inside 1.6 s. Now a LIMIT 0 probe reads the column count before any row is
#     built, and the per-value limit is derived from it. Same query: 35 MB.
_wide = "SELECT " + ", ".join(f"hex(zeroblob(500000)) AS c{i}" for i in range(300))
try:
    run_sql(_wide, limit=1)
    raise AssertionError("a 300-column result of 1 MB values was returned whole")
except SQLResultTooLargeError:
    print("  ✓ a single very WIDE row is refused before it is materialized")

# (b) NON-UNIFORM ROWS. The budget used to size a fetchmany() from the running
#     AVERAGE row size, which only holds on a uniform result: with small rows
#     first the average stayed tiny, the batch pinned at its 1000 ceiling, and
#     one fetch pulled ~1 GB resident BEFORE the check ran. validate_sql accepts
#     the shaping CASE, so this was reachable on the CSV path. Now checked per
#     row: same query, 101 MB.
_shaped = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<400) "
           "SELECT CASE WHEN i < 50 THEN 'x' ELSE hex(zeroblob(500000)) END AS v FROM n")
try:
    run_sql(_shaped, limit=100_000)
    raise AssertionError("a small-rows-then-large-rows result was returned whole")
except SQLResultTooLargeError:
    print("  ✓ small rows followed by large ones cannot defeat the budget")

print("\n== ipeds_years / has_ipeds_data: fresh-deploy 'no data' probes ==")
# Non-raising probes for the "no dataset loaded yet" state. Built entirely on
# tiny throwaway sqlite files under a tempdir -- never the real ipeds.db, and
# never mdbtools.
_probe_tmp = Path(tempfile.mkdtemp())

missing_path = _probe_tmp / "does_not_exist.db"
assert ipeds_years(missing_path) == [], \
    f"a missing db file must yield [], got {ipeds_years(missing_path)}"
assert has_ipeds_data(missing_path) is False, "a missing db file must yield has_data=False"
print("  ✓ missing db file -> ipeds_years=[] / has_ipeds_data=False")

empty_path = _probe_tmp / "empty.db"
empty_path.write_bytes(b"")
assert ipeds_years(empty_path) == [], \
    f"a 0-byte file must yield [], got {ipeds_years(empty_path)}"
assert has_ipeds_data(empty_path) is False, "a 0-byte file must yield has_data=False"
print("  ✓ 0-byte file -> ipeds_years=[] / has_ipeds_data=False")

garbage_path = _probe_tmp / "garbage.db"
garbage_path.write_bytes(b"this is not a sqlite database, just plain garbage bytes" * 50)
assert ipeds_years(garbage_path) == [], \
    f"a non-sqlite garbage file must yield [], got {ipeds_years(garbage_path)}"
assert has_ipeds_data(garbage_path) is False, "a garbage file must yield has_data=False"
print("  ✓ garbage (non-sqlite) file -> ipeds_years=[] / has_ipeds_data=False")

no_years_table_path = _probe_tmp / "no_years_table.db"
_con = sqlite3.connect(str(no_years_table_path))
_con.execute("CREATE TABLE hd (unitid INTEGER)")
_con.commit()
_con.close()
assert ipeds_years(no_years_table_path) == [], \
    "a real sqlite file with no _years table must yield []"
assert has_ipeds_data(no_years_table_path) is False, \
    "a real sqlite file with no _years table must yield has_data=False"
print("  ✓ sqlite file with no _years table -> ipeds_years=[] / has_ipeds_data=False")

fixture_path = _probe_tmp / "fixture.db"
_con = sqlite3.connect(str(fixture_path))
_con.execute("CREATE TABLE _years (year INTEGER)")
_con.executemany("INSERT INTO _years(year) VALUES (?)", [(2024,), (2023,), (2025,)])
_con.commit()
_con.close()
years = ipeds_years(fixture_path)
assert years == [2023, 2024, 2025], f"expected sorted [2023, 2024, 2025], got {years}"
assert has_ipeds_data(fixture_path) is True, "a fixture with rows must yield has_data=True"
print(f"  ✓ fixture _years table -> ipeds_years={years} / has_ipeds_data=True")

# default (no db_path arg) must resolve against settings.ipeds_db_path, exactly
# like run_sql's default -- not silently require the caller to pass a path.
default_years = ipeds_years()
assert isinstance(default_years, list), \
    f"ipeds_years() with no args must return a list, got {type(default_years)}"
print(f"  ✓ ipeds_years() with no db_path arg returns a list ({len(default_years)} year(s))")

print("\n== truncation raises a ⚠ AGGREGATION CHECK note (S4) ==")
# A CUT page summed as a TOTAL is a wrong number whose SQL looks perfect, so
# truncation must carry the SAME blocking marker the rollup lints do — not just
# the soft "(truncated)" header word. Hermetic temp db (no real ipeds.db):
# run_sql opens mode=ro&immutable=1, which reads a pre-written file fine.
_trunc_path = _probe_tmp / "trunc.db"
_con = sqlite3.connect(str(_trunc_path))
_con.execute("CREATE TABLE t (n INTEGER)")
_con.executemany("INSERT INTO t(n) VALUES (?)", [(i,) for i in range(5)])
_con.commit()
_con.close()

r_cut = run_sql("SELECT n FROM t", limit=2, db_path=_trunc_path)
assert r_cut.truncated is True, "5 rows with limit=2 must truncate"
assert any("⚠ AGGREGATION CHECK (truncated)" in note for note in r_cut.notes), \
    f"a truncated result must carry the blocking marker; notes={r_cut.notes}"
print("  ✓ truncated result carries '⚠ AGGREGATION CHECK (truncated)'")

r_full = run_sql("SELECT n FROM t", limit=10, db_path=_trunc_path)
assert r_full.truncated is False, "5 rows with limit=10 must NOT truncate"
assert not any("truncated" in note.lower() for note in r_full.notes), \
    f"a complete result must not carry a truncation note; notes={r_full.notes}"
print("  ✓ complete result carries no truncation marker")

print("\n== SEC-5: string literals survive comment stripping ==")
# Regression: _strip_sql removed `--`/`/* */` comments from the RAW sql before
# masking literals, so a literal like '2020--Q1' lost everything after `--` in
# the EXECUTED query (the WHERE then matched the wrong row or nothing). The fix
# strips comments literal-awarely; these assert the literals reach SQLite intact
# while REAL comments are still removed.
_lit_path = _probe_tmp / "literals.db"
_con = sqlite3.connect(str(_lit_path))
_con.execute("CREATE TABLE labels (tag TEXT)")
_con.executemany("INSERT INTO labels(tag) VALUES (?)",
                 [("2020--Q1",), ("a /* b */ c",), ("plain",)])
_con.commit()
_con.close()

r = run_sql("SELECT tag FROM labels WHERE tag = '2020--Q1'", db_path=_lit_path)
assert [row[0] for row in r.rows] == ["2020--Q1"], \
    f"a '--' literal must survive comment stripping; got {r.rows}"
print("  ✓ literal '2020--Q1' survives (no line-comment truncation)")

r = run_sql("SELECT tag FROM labels WHERE tag = 'a /* b */ c'", db_path=_lit_path)
assert [row[0] for row in r.rows] == ["a /* b */ c"], \
    f"a '/* */' literal must survive; got {r.rows}"
print("  ✓ literal 'a /* b */ c' survives (no block-comment stripping)")

r = run_sql("SELECT tag FROM labels WHERE tag='plain' -- trailing note\n", db_path=_lit_path)
assert [row[0] for row in r.rows] == ["plain"], \
    f"a real -- comment must still be stripped cleanly; got {r.rows}"
print("  ✓ a real -- comment is still stripped")

from app.tools.sql import _strip_sql  # noqa: E402

cleaned = _strip_sql("SELECT '2020--Q1' -- note")
assert "2020--Q1" in cleaned and "note" not in cleaned, \
    f"_strip_sql must keep the literal, drop the real comment; got {cleaned!r}"
cleaned_blk = _strip_sql("SELECT 'a/*x*/b' /* real */ FROM t")
assert "a/*x*/b" in cleaned_blk and "real" not in cleaned_blk, \
    f"_strip_sql must keep a /* */ inside a literal, drop the real block; got {cleaned_blk!r}"
validate_sql("SELECT 'a;b' AS x")  # a ';' inside a literal doesn't trip the single-stmt guard
print("  ✓ _strip_sql / single-statement guard are literal-aware")

print("\nALL SQL-GUARD TESTS PASSED")
