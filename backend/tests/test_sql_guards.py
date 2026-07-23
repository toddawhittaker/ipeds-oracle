"""Smoke test for the read-only SQL engine: guards, a real query, and timeout."""
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools.sql import (
    SQLTimeoutError,
    SQLValidationError,
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

print("\nALL SQL-GUARD TESTS PASSED")
