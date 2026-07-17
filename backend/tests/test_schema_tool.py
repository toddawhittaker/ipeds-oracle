"""Discovery-tool contract (backend/app/tools/schema.py) — the §3 Discovery queries the
model uses to look up families, columns, variable titles, and code labels
on demand instead of guessing.

Runs against a tiny fixture ipeds.db (built at IPEDS_DB_PATH before any app
import, since app.config.get_settings() is lru_cached) rather than the real
1.9GB database, so it's fast and needs no external data.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
IPEDS_DB_PATH = Path(tmp) / "ipeds.db"
os.environ["IPEDS_DB_PATH"] = str(IPEDS_DB_PATH)
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"


def _build_fixture(path: Path) -> None:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE _family_map (src_table TEXT, family TEXT, "
        "survey_year TEXT, year INTEGER, n_rows INTEGER)"
    )
    con.executemany(
        "INSERT INTO _family_map VALUES (?,?,?,?,?)",
        [
            ("C2023_A", "c_a", "2022-23", 2024, 900),
            ("C2024_A", "c_a", "2023-24", 2025, 1000),
            ("HD2024", "hd", "2023-24", 2025, 6),
        ],
    )
    con.execute("CREATE TABLE _years (survey_year TEXT, year INTEGER PRIMARY KEY)")
    con.executemany(
        "INSERT INTO _years VALUES (?,?)",
        [("2022-23", 2024), ("2023-24", 2025)],
    )
    # An actual family (physical) table so pragma_table_info can inspect it.
    con.execute(
        "CREATE TABLE c_a (year INTEGER, ctotalt INTEGER, awlevel INTEGER, "
        "majornum INTEGER, cipcode TEXT)"
    )
    con.execute(
        "CREATE TABLE vartable (tablename TEXT, varname TEXT, vartitle TEXT, "
        "varorder INTEGER, year INTEGER)"
    )
    con.executemany(
        "INSERT INTO vartable VALUES (?,?,?,?,?)",
        [
            ("C2024_A", "CTOTALT", "Grand total", 1, 2025),
            ("C2024_A", "CTOTALM", "Grand total men", 2, 2025),
            ("C2024_A", "CTOTALW", "Grand total women", 3, 2025),
            ("C2024_A", "AWLEVEL", "Award level code", 4, 2025),
            ("HD2024", "STABBR", "State abbreviation", 1, 2025),
        ],
    )
    con.execute(
        "CREATE TABLE valuesets (varname TEXT, tablename TEXT, codevalue TEXT, "
        "valuelabel TEXT, year INTEGER)"
    )
    con.executemany(
        "INSERT INTO valuesets VALUES (?,?,?,?,?)",
        [
            ("AWLEVEL", "C2024_A", "3", "Associate's degree", 2025),
            ("AWLEVEL", "C2024_A", "5", "Bachelor's degree", 2025),
            ("CIPCODE", "C2024_A", "51.3801", "Registered Nursing", 2025),
            ("CIPCODE", "C2024_A", "11.0701", "Computer Science", 2025),
        ],
    )
    con.commit()
    con.close()


_build_fixture(IPEDS_DB_PATH)

from app.tools import schema  # noqa: E402

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def test_list_families():
    out = schema.list_families()
    assert "c_a" in out and "hd" in out, out
    # c_a's two _family_map rows (900+1000) should be summed.
    assert "1900" in out, out


def test_get_columns_known_family():
    out = schema.get_columns("c_a")
    assert "Columns of `c_a`" in out, out
    for col in ("year", "ctotalt", "awlevel", "majornum", "cipcode"):
        assert col in out, out


def test_get_columns_unknown_family():
    out = schema.get_columns("no_such_family")
    assert "No family named 'no_such_family'" in out, out
    assert "list_families" in out, out


def test_get_columns_strips_quotes_and_whitespace():
    out = schema.get_columns("  'c_a'  ")
    assert "Columns of `c_a`" in out, out


def test_describe_variables_known_family():
    out = schema.describe_variables("c_a")
    assert "Variables in `c_a` (source `C2024_A`)" in out, out
    assert "CTOTALT" in out and "Grand total" in out, out


def test_describe_variables_with_keyword_filter():
    out = schema.describe_variables("c_a", keyword="women")
    assert "matching 'women'" in out, out
    assert "CTOTALW" in out, out
    assert "CTOTALM" not in out, out


def test_describe_variables_unknown_family():
    out = schema.describe_variables("nope")
    assert "No family named 'nope'" in out, out


def test_lookup_code_known_variable():
    out = schema.lookup_code("AWLEVEL")
    assert "Codes for `AWLEVEL`" in out, out
    assert "Associate's degree" in out and "Bachelor's degree" in out, out


def test_lookup_code_with_value_filter():
    out = schema.lookup_code("awlevel", value="3")
    assert "Associate's degree" in out, out
    assert "Bachelor's degree" not in out, out


def test_lookup_code_unknown_variable():
    out = schema.lookup_code("NOTAREALVAR")
    assert "No codes found for variable 'NOTAREALVAR'" in out, out
    assert "describe_variables" in out, out


def test_find_variable_matches_by_title_or_name():
    out = schema.find_variable("total")
    assert "Variables matching 'total'" in out, out
    assert "CTOTALT" in out, out
    assert "STABBR" not in out, out


def test_find_cip_matches_by_label():
    out = schema.find_cip("Nursing")
    assert "CIP codes matching 'Nursing'" in out, out
    assert "51.3801" in out, out
    assert "11.0701" not in out, out


def run():
    print("discovery-tool (schema.py) contract:")
    check("list_families reports families with summed row counts",
          test_list_families)
    check("get_columns returns actual columns for a known family",
          test_get_columns_known_family)
    check("get_columns reports an unknown family clearly",
          test_get_columns_unknown_family)
    check("get_columns strips quotes/whitespace from the family name",
          test_get_columns_strips_quotes_and_whitespace)
    check("describe_variables lists a family's variables (latest year)",
          test_describe_variables_known_family)
    check("describe_variables filters by keyword",
          test_describe_variables_with_keyword_filter)
    check("describe_variables reports an unknown family clearly",
          test_describe_variables_unknown_family)
    check("lookup_code returns all codes for a known variable",
          test_lookup_code_known_variable)
    check("lookup_code filters to a single value", test_lookup_code_with_value_filter)
    check("lookup_code reports an unknown variable clearly",
          test_lookup_code_unknown_variable)
    check("find_variable searches title/name across families",
          test_find_variable_matches_by_title_or_name)
    check("find_cip searches CIP labels by keyword", test_find_cip_matches_by_label)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL SCHEMA-TOOL TESTS PASSED")


if __name__ == "__main__":
    run()
