"""run_sql result isolation: the QueryResult behind an answer must be scoped to
the request, not stashed in module-global state where concurrent chat turns
could clobber each other's data (the bug this guards against). Uses the CI
fixture db via IPEDS_DB_PATH; the queries need no tables.
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools import registry

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def test_separate_sinks_are_independent():
    a = {"result": None}
    b = {"result": None}
    registry.dispatch("run_sql", {"sql": "SELECT 1 AS v"}, result_sink=a)
    registry.dispatch("run_sql", {"sql": "SELECT 2 AS v"}, result_sink=b)
    assert a["result"].rows[0][0] == 1, a["result"].rows
    assert b["result"].rows[0][0] == 2, b["result"].rows


def test_dispatch_without_sink_ok():
    out = registry.dispatch("run_sql", {"sql": "SELECT 3 AS v"})
    assert out.startswith("OK"), out


def test_no_module_global():
    # Regression guard: the shared LAST_RESULT global must stay gone.
    assert not hasattr(registry, "LAST_RESULT"), "module-global result state is back"
    assert not hasattr(registry, "reset_last_result")


def test_concurrent_dispatch_no_crosstalk():
    n = 40
    sinks = [{"result": None} for _ in range(n)]
    errors = []

    def worker(i):
        registry.dispatch("run_sql", {"sql": f"SELECT {i} AS v"}, result_sink=sinks[i])
        got = sinks[i]["result"].rows[0][0]
        if got != i:
            errors.append((i, got))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"cross-talk between concurrent requests: {errors[:5]}"


def run():
    print("run_sql result isolation:")
    check("separate sinks are independent", test_separate_sinks_are_independent)
    check("dispatch without a sink still works", test_dispatch_without_sink_ok)
    check("no shared module-global result state", test_no_module_global)
    check("concurrent dispatch has no cross-talk", test_concurrent_dispatch_no_crosstalk)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL RESULT-ISOLATION TESTS PASSED")


if __name__ == "__main__":
    run()
