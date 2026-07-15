"""Pre-flight aggregation lint (app/tools/sqllint.py) + its wiring into run_sql.

The linter is the deterministic enforcement behind the prompt's "sanity-check
magnitudes" rule: it flags the IPEDS rollup/hang foot-guns (CIP LIKE / no-guard
overcount, second-major double count, DISTINCT-year join) so the model can fix
the query before a wrong number is returned. These are pure string heuristics,
so most tests need no DB; one integration test runs a real query against the
IPEDS_DB_PATH fixture to prove the ⚠ note reaches the tool result.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools import registry  # noqa: E402
from app.tools.sqllint import lint_sql  # noqa: E402

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _codes(sql):
    return {f.code for f in lint_sql(sql)}


# --- CIP LIKE rollup -----------------------------------------------------------

def test_cip_like_flagged():
    assert "cip-like-rollup" in _codes(
        "SELECT SUM(ctotalt) FROM c_a WHERE cipcode LIKE '51.%' AND majornum=1")


def test_cip_not_like_flagged():
    assert "cip-like-rollup" in _codes(
        "SELECT SUM(ctotalt) FROM c_a WHERE cipcode NOT LIKE '51.%' AND majornum=1")


def test_qualified_cip_like_flagged():
    # aliased column `c.cipcode` must still trip the check
    assert "cip-like-rollup" in _codes(
        "SELECT SUM(c.ctotalt) FROM c_a c WHERE c.cipcode LIKE '11.%'")


# --- CIP sum with no level guard ----------------------------------------------

def test_sum_no_guard_flagged():
    codes = _codes("SELECT SUM(ctotalt) FROM c_a WHERE year=2025 AND majornum=1")
    assert "cip-sum-no-guard" in codes, codes


def test_exact_cip_not_flagged():
    codes = _codes(
        "SELECT SUM(ctotalt) FROM c_a WHERE cipcode='51.3801' AND majornum=1")
    assert "cip-sum-no-guard" not in codes, codes


def test_cip_99_total_not_flagged():
    # the documented correct national-total pattern must stay clean
    codes = _codes(
        "SELECT year, SUM(ctotalt) FROM c_a "
        "WHERE awlevel=3 AND majornum=1 AND cipcode='99' GROUP BY year")
    assert codes == set(), codes


def test_length_cip_guard_not_flagged():
    codes = _codes(
        "SELECT SUM(ctotalt) FROM c_a WHERE length(cipcode)=7 AND majornum=1")
    assert "cip-sum-no-guard" not in codes, codes


def test_group_by_cipcode_suppresses_rollup_check():
    codes = _codes(
        "SELECT cipcode, SUM(ctotalt) FROM c_a WHERE majornum=1 GROUP BY cipcode")
    assert "cip-sum-no-guard" not in codes, codes


def test_cip_in_list_guard_not_flagged():
    codes = _codes(
        "SELECT SUM(ctotalt) FROM c_a "
        "WHERE cipcode IN ('51.3801','51.3802') AND majornum=1")
    assert "cip-sum-no-guard" not in codes, codes


# --- second-major double count -------------------------------------------------

def test_missing_majornum_flagged():
    assert "majornum-missing" in _codes(
        "SELECT SUM(ctotalt) FROM c_a WHERE cipcode='51.3801'")


def test_majornum_present_not_flagged():
    codes = _codes(
        "SELECT SUM(ctotalt) FROM c_a WHERE cipcode='51.3801' AND majornum=1")
    assert "majornum-missing" not in codes, codes


# --- DISTINCT-year join hang ---------------------------------------------------

def test_distinct_year_join_flagged():
    assert "distinct-year-join" in _codes(
        "SELECT SUM(ctotalt) FROM c_a JOIN (SELECT DISTINCT year FROM _years) y "
        "USING(year) WHERE cipcode='51.3801' AND majornum=1")


def test_distinct_year_in_flagged():
    assert "distinct-year-join" in _codes(
        "SELECT SUM(ctotalt) FROM c_a "
        "WHERE year IN (SELECT DISTINCT year FROM _years) "
        "AND cipcode='51.3801' AND majornum=1")


def test_constant_year_bound_not_flagged():
    codes = _codes(
        "SELECT SUM(ctotalt) FROM c_a "
        "WHERE year > (SELECT MAX(year)-3 FROM _years) "
        "AND cipcode='51.3801' AND majornum=1")
    assert codes == set(), codes


# --- scope: only c_a sums, and literal/comment safety --------------------------

def test_non_c_a_query_clean():
    # summing a finance column out of a different family is not our concern
    assert lint_sql("SELECT SUM(f1_ttl_rev) FROM f_f1 WHERE year=2025") == []


def test_like_inside_string_literal_not_flagged():
    # 'cipcode like' only appears inside a literal → must NOT trip cip-like
    codes = _codes(
        "SELECT instnm FROM hd WHERE instnm LIKE '%cipcode like table%'")
    assert "cip-like-rollup" not in codes, codes


def test_comment_mentioning_cipcode_like_not_flagged():
    codes = _codes(
        "SELECT instnm FROM hd -- filter cipcode like later\nWHERE stabbr='CA'")
    assert "cip-like-rollup" not in codes, codes


def test_clean_query_returns_empty():
    assert lint_sql(
        "SELECT instnm, stabbr FROM institutions_current WHERE control=1") == []


def test_double_wrong_query_gets_both_checks():
    # no CIP guard AND no majornum → both rollup foot-guns fire
    codes = _codes("SELECT SUM(ctotalt) FROM c_a WHERE year=2025")
    assert {"cip-sum-no-guard", "majornum-missing"} <= codes, codes


# --- integration: the note reaches the run_sql tool result ---------------------

def test_lint_note_surfaced_in_tool_result():
    # a valid query (fixture c_a exists) that is nonetheless a double-count:
    # it executes fine but the tool result must carry the ⚠ warning.
    out = registry.dispatch("run_sql", {"sql": "SELECT SUM(ctotalt) FROM c_a"})
    assert out.startswith("OK"), out
    assert "⚠ AGGREGATION CHECK" in out, out
    assert "cip-sum-no-guard" in out, out


def test_clean_query_has_no_lint_note():
    out = registry.dispatch(
        "run_sql",
        {"sql": "SELECT SUM(ctotalt) FROM c_a WHERE cipcode='99' AND majornum=1"})
    assert out.startswith("OK"), out
    assert "AGGREGATION CHECK" not in out, out


def run():
    print("sql aggregation lint:")
    check("cipcode LIKE is flagged", test_cip_like_flagged)
    check("cipcode NOT LIKE is flagged", test_cip_not_like_flagged)
    check("qualified c.cipcode LIKE is flagged", test_qualified_cip_like_flagged)
    check("SUM over c_a with no CIP guard is flagged", test_sum_no_guard_flagged)
    check("exact 6-digit cipcode is clean", test_exact_cip_not_flagged)
    check("cipcode='99' national total is clean", test_cip_99_total_not_flagged)
    check("length(cipcode)=7 guard is clean", test_length_cip_guard_not_flagged)
    check("GROUP BY cipcode suppresses the rollup check",
          test_group_by_cipcode_suppresses_rollup_check)
    check("cipcode IN (...) guard is clean", test_cip_in_list_guard_not_flagged)
    check("missing majornum is flagged", test_missing_majornum_flagged)
    check("majornum=1 present is clean", test_majornum_present_not_flagged)
    check("DISTINCT-year JOIN is flagged", test_distinct_year_join_flagged)
    check("DISTINCT-year IN (...) is flagged", test_distinct_year_in_flagged)
    check("constant year bound is clean", test_constant_year_bound_not_flagged)
    check("non-c_a SUM is out of scope", test_non_c_a_query_clean)
    check("'like' inside a string literal is ignored",
          test_like_inside_string_literal_not_flagged)
    check("'cipcode like' inside a comment is ignored",
          test_comment_mentioning_cipcode_like_not_flagged)
    check("a clean query returns no findings", test_clean_query_returns_empty)
    check("a doubly-wrong query trips both rollup checks",
          test_double_wrong_query_gets_both_checks)
    check("the ⚠ note is surfaced in the run_sql tool result",
          test_lint_note_surfaced_in_tool_result)
    check("a clean query carries no ⚠ note", test_clean_query_has_no_lint_note)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} lint test(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL SQL-LINT TESTS PASSED")


if __name__ == "__main__":
    run()
