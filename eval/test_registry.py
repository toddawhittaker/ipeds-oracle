"""Tool-registry contract (app/tools/registry.py): the JSON-schema tool specs
sent to the model, and dispatch()'s error handling for every failure mode a
model-generated tool call can hit — unknown tool name, malformed/missing
arguments (TypeError), and every run_sql failure branch (rejected SQL, a
timeout, and a real execution error) — so a bad model turn always comes back
as a string the agent loop can show the model, never an unhandled exception.

Runs against the shared IPEDS_DB_PATH fixture (c_a/hd, no API key needed).
SQL_TIMEOUT_SECONDS is dropped to 1s (set before any app import, since
get_settings() is lru_cached) so the timeout branch doesn't need a real 25s
wait.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["SQL_TIMEOUT_SECONDS"] = "1"

from app.tools import registry  # noqa: E402

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def test_tool_specs_shape():
    specs = registry.tool_specs()
    names = {s["function"]["name"] for s in specs}
    assert names == {"run_sql", "list_families", "get_columns",
                     "describe_variables", "lookup_code", "find_variable",
                     "find_cip"}, names
    for s in specs:
        assert s["type"] == "function", s
        assert "description" in s["function"], s


def test_dispatch_unknown_tool():
    out = registry.dispatch("not_a_real_tool", "{}")
    assert out == "ERROR: unknown tool 'not_a_real_tool'.", out


def test_dispatch_run_sql_rejected():
    out = registry.dispatch("run_sql", {"sql": "DROP TABLE c_a"})
    assert out.startswith("SQL REJECTED:"), out


def test_dispatch_run_sql_execution_error():
    out = registry.dispatch("run_sql", {"sql": "SELECT * FROM no_such_table_xyz"})
    assert out.startswith("SQL ERROR:"), out


def test_dispatch_run_sql_timeout():
    out = registry.dispatch(
        "run_sql", {"sql": "SELECT COUNT(*) FROM c_a a, c_a b, c_a c"})
    assert out.startswith("SQL TIMEOUT:"), out


def test_dispatch_non_run_sql_tool_success():
    out = registry.dispatch("get_columns", {"family": "c_a"})
    assert "Columns of `c_a`" in out, out
    for col in ("year", "ctotalt", "awlevel"):
        assert col in out, out


def test_dispatch_missing_required_argument_is_type_error():
    out = registry.dispatch("get_columns", {})  # 'family' is required
    assert out.startswith("ERROR calling get_columns:"), out


def run():
    print("tool-registry contract:")
    check("tool_specs() lists every registered tool with a description",
          test_tool_specs_shape)
    check("dispatch() reports an unknown tool name clearly",
          test_dispatch_unknown_tool)
    check("dispatch(run_sql) surfaces a rejected/forbidden query",
          test_dispatch_run_sql_rejected)
    check("dispatch(run_sql) surfaces a real execution error",
          test_dispatch_run_sql_execution_error)
    check("dispatch(run_sql) surfaces a timeout", test_dispatch_run_sql_timeout)
    check("dispatch() runs a non-run_sql tool successfully",
          test_dispatch_non_run_sql_tool_success)
    check("dispatch() reports a missing required argument (TypeError)",
          test_dispatch_missing_required_argument_is_type_error)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL TOOL-REGISTRY TESTS PASSED")


if __name__ == "__main__":
    run()
