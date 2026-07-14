"""Smoke test for the read-only SQL engine: guards, a real query, and timeout."""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools.sql import run_sql, validate_sql, SQLValidationError, SQLTimeoutError

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

print("\nALL SQL-GUARD TESTS PASSED")
