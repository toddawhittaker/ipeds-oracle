"""Disk/time estimator contract (backend/app/estimate.py) — the single source of truth
for the NCES "integrate" preflight math, shared (in spirit) with its JS mirror
frontend/src/estimate.js (see frontend/src/estimate.test.js for the cross-language
agreement test against the SAME fixture: backend/tests/fixtures/estimate_cases.json).

estimate_integrate is a PURE function: no I/O, no settings lookups — every
input is passed explicitly by the caller (backend/app/importer.py reads the disk facts
and calibration knobs from app.config.Settings and shutil.disk_usage, then
calls this). That's what makes it testable byte-for-byte against a fixture.

Pinned arithmetic (see the architect's contract for the full derivation):
  MB = 1024*1024 (storage) vs. bandwidth_mbps * 1_000_000/8 (decimal Mbps ->
  bytes/sec) — these must NOT be conflated. A None entry in zip_bytes falls
  back to default_per_year_db_mb*MB (an unprobed/未知-size year still has to
  contribute *something* to the estimate). per_year_db_bytes divides
  live_db_bytes by current_integrated_year_count UNLESS either is zero/absent
  (fresh install, no live db yet) — then it falls back to the same default.
  `sufficient` is a >=, i.e. free bytes exactly equal to the safety-padded
  requirement must still read as sufficient (no off-by-one flip to "you don't
  have enough disk" on a razor's-edge but technically-fine case).

Ground truth: every value in the fixture was derived by directly executing the
architect's pinned formula (not hand arithmetic) — see the test-engineer's
report for the derivation script. This test asserts app.estimate.estimate_integrate
reproduces it exactly (byte counts as exact ints, seconds as floats compared
with a tight tolerance for float rounding).
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import estimate  # noqa: E402

FAILURES = []
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "estimate_cases.json"


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _load_cases():
    return json.loads(FIXTURE_PATH.read_text())


def _assert_case_matches(case):
    result = estimate.estimate_integrate(**case["input"])
    expected = case["expected"]
    assert set(result.keys()) == set(expected.keys()), (
        f"{case['name']}: key mismatch — got {sorted(result.keys())}, "
        f"expected {sorted(expected.keys())}")
    for key, exp_val in expected.items():
        got = result[key]
        if isinstance(exp_val, float):
            assert isinstance(got, (int, float)), f"{case['name']}.{key}: {got!r} not numeric"
            assert math.isclose(got, exp_val, rel_tol=1e-9, abs_tol=1e-6), (
                f"{case['name']}.{key}: got {got!r}, expected {exp_val!r}")
        elif isinstance(exp_val, bool):
            assert got is exp_val, f"{case['name']}.{key}: got {got!r}, expected {exp_val!r}"
        else:
            assert got == exp_val, f"{case['name']}.{key}: got {got!r}, expected {exp_val!r}"


def test_fixture_file_exists_and_has_four_cases():
    cases = _load_cases()
    assert len(cases) == 4, f"expected 4 fixture cases, got {len(cases)}"
    names = {c["name"] for c in cases}
    assert names == {
        "normal_multi_year_selection",
        "none_zip_uses_default_per_year_mb",
        "divide_by_zero_fallback_per_year_db_bytes",
        "sufficient_boundary_exact_equal_is_true",
    }, names


def test_all_fixture_cases_match():
    for case in _load_cases():
        _assert_case_matches(case)


def test_none_zip_contributes_default_per_year_mb():
    cases = {c["name"]: c for c in _load_cases()}
    case = cases["none_zip_uses_default_per_year_mb"]
    _assert_case_matches(case)
    # Explicitly pin the None contribution: total_download_bytes must equal
    # the known zip size plus exactly one default_per_year_db_mb*MB slice —
    # not zero, not the known size alone.
    MB = 1024 * 1024
    known = [z for z in case["input"]["zip_bytes"] if z is not None]
    none_count = sum(1 for z in case["input"]["zip_bytes"] if z is None)
    result = estimate.estimate_integrate(**case["input"])
    assert result["total_download_bytes"] == sum(known) + (
        case["input"]["default_per_year_db_mb"] * MB * none_count)


def test_divide_by_zero_fallback_uses_default_per_year_mb():
    cases = {c["name"]: c for c in _load_cases()}
    case = cases["divide_by_zero_fallback_per_year_db_bytes"]
    assert case["input"]["live_db_bytes"] == 0 or \
        case["input"]["current_integrated_year_count"] == 0
    _assert_case_matches(case)
    MB = 1024 * 1024
    result = estimate.estimate_integrate(**case["input"])
    assert result["per_year_db_bytes"] == case["input"]["default_per_year_db_mb"] * MB


def test_divide_by_zero_fallback_also_triggers_on_zero_year_count_alone():
    # live_db_bytes > 0 but current_integrated_year_count == 0 must ALSO hit
    # the fallback (not attempt a real division by zero).
    MB = 1024 * 1024
    result = estimate.estimate_integrate(
        zip_bytes=[100_000_000], already_integrated_count=0, selected_count=1,
        live_db_bytes=5_000_000_000, current_integrated_year_count=0,
        disk_free_bytes=10_000_000_000, disk_total_bytes=10_000_000_000,
        expand_factor=3.0, default_per_year_db_mb=380, bandwidth_mbps=10.0,
        build_seconds_per_year=60.0, safety_factor=1.2)
    assert result["per_year_db_bytes"] == 380 * MB, result["per_year_db_bytes"]


def test_sufficient_boundary_exact_equal_is_true():
    cases = {c["name"]: c for c in _load_cases()}
    case = cases["sufficient_boundary_exact_equal_is_true"]
    _assert_case_matches(case)
    result = estimate.estimate_integrate(**case["input"])
    assert result["disk_free_bytes"] == result["needed_with_safety_bytes"], (
        "this fixture case is specifically constructed so free == needed; "
        "if that's no longer true the fixture itself needs fixing")
    assert result["sufficient"] is True


def test_one_byte_short_of_boundary_is_insufficient():
    # Same inputs as the boundary case, but one byte short of free space —
    # pins the OTHER side of the >= boundary (not just the equal case).
    cases = {c["name"]: c for c in _load_cases()}
    case = cases["sufficient_boundary_exact_equal_is_true"]
    inp = dict(case["input"])
    inp["disk_free_bytes"] = case["expected"]["needed_with_safety_bytes"] - 1
    result = estimate.estimate_integrate(**inp)
    assert result["sufficient"] is False, result


def test_bandwidth_uses_decimal_megabit_not_mebibyte():
    # 10 Mbps == 10_000_000 bits/sec == 1_250_000 bytes/sec (decimal, /8) —
    # NOT 10*1024*1024/8. A download of exactly that many bytes must take
    # exactly 1.0 second, pinning that bandwidth and storage use DIFFERENT
    # unit bases (a common foot-gun: implementer silently reusing the 1024*1024
    # MB constant for the network-speed conversion too).
    result = estimate.estimate_integrate(
        zip_bytes=[1_250_000], already_integrated_count=0, selected_count=1,
        live_db_bytes=0, current_integrated_year_count=0,
        disk_free_bytes=10_000_000_000, disk_total_bytes=10_000_000_000,
        expand_factor=1.0, default_per_year_db_mb=0, bandwidth_mbps=10.0,
        build_seconds_per_year=0.0, safety_factor=1.0)
    assert math.isclose(result["est_download_seconds"], 1.0, rel_tol=1e-9), \
        result["est_download_seconds"]


def test_result_has_exactly_the_pinned_keys():
    result = estimate.estimate_integrate(
        zip_bytes=[1], already_integrated_count=0, selected_count=1,
        live_db_bytes=0, current_integrated_year_count=0,
        disk_free_bytes=1, disk_total_bytes=1,
        expand_factor=1.0, default_per_year_db_mb=1, bandwidth_mbps=1.0,
        build_seconds_per_year=1.0, safety_factor=1.0)
    expected_keys = {
        "total_download_bytes", "extracted_bytes", "staging_db_bytes",
        "per_year_db_bytes", "additional_bytes_needed", "used_now_bytes",
        "peak_used_bytes", "disk_free_bytes", "disk_total_bytes",
        "est_download_seconds", "est_build_seconds", "safety_factor",
        "needed_with_safety_bytes", "sufficient",
    }
    assert set(result.keys()) == expected_keys, set(result.keys())


def test_byte_fields_are_ints_not_floats():
    # Byte-count fields must be integer floor()s, not floats — a downstream
    # UI formatting a raw float byte count as "6900000000.0 bytes" would be a
    # visible regression, and JSON round-tripping ints vs floats matters for
    # the cross-language fixture comparison too.
    cases = {c["name"]: c for c in _load_cases()}
    result = estimate.estimate_integrate(**cases["normal_multi_year_selection"]["input"])
    for key in ("total_download_bytes", "extracted_bytes", "staging_db_bytes",
               "per_year_db_bytes", "additional_bytes_needed", "used_now_bytes",
               "peak_used_bytes", "needed_with_safety_bytes"):
        assert isinstance(result[key], int), f"{key} is {type(result[key])}, expected int"


def run():
    print("estimate contract:")
    check("fixture file exists with the 4 required cases",
          test_fixture_file_exists_and_has_four_cases)
    check("all fixture cases match estimate_integrate exactly",
          test_all_fixture_cases_match)
    check("a None zip entry contributes exactly one default_per_year_db_mb*MB slice",
          test_none_zip_contributes_default_per_year_mb)
    check("divide-by-zero fallback (live_db_bytes/current_integrated_year_count) "
          "uses default_per_year_db_mb*MB",
          test_divide_by_zero_fallback_uses_default_per_year_mb)
    check("fallback also triggers when only current_integrated_year_count is 0",
          test_divide_by_zero_fallback_also_triggers_on_zero_year_count_alone)
    check("sufficient boundary: free exactly == needed_with_safety -> True",
          test_sufficient_boundary_exact_equal_is_true)
    check("one byte short of the boundary -> insufficient (False)",
          test_one_byte_short_of_boundary_is_insufficient)
    check("bandwidth uses decimal Mbps/8, not the 1024*1024 storage MB constant",
          test_bandwidth_uses_decimal_megabit_not_mebibyte)
    check("result dict has exactly the pinned key set",
          test_result_has_exactly_the_pinned_keys)
    check("byte-count fields are ints (floor'd), not floats",
          test_byte_fields_are_ints_not_floats)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL ESTIMATE TESTS PASSED")


if __name__ == "__main__":
    run()
