"""Schema-migration contract for app.db (_apply_migrations + init_db).

Verifies the PRAGMA user_version-based runner: fresh dbs get every migration,
re-runs are idempotent, only newly-added migrations apply, a pre-version db
(tables already present, user_version 0) advances without losing data, and the
real init_db lands at the baseline version with all tables + bootstrap intact.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@franklin.edu"

from app.db import MIGRATIONS, _apply_migrations, connect, init_db

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _cols(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def test_fresh_applies_all_and_sets_version():
    con = sqlite3.connect(":memory:")
    migs = [(1, "CREATE TABLE t (a INTEGER);"),
            (2, "ALTER TABLE t ADD COLUMN b INTEGER;")]
    v = _apply_migrations(con, migs)
    assert v == 2, f"expected version 2, got {v}"
    assert con.execute("PRAGMA user_version").fetchone()[0] == 2
    assert _cols(con, "t") == {"a", "b"}, _cols(con, "t")


def test_idempotent_rerun():
    con = sqlite3.connect(":memory:")
    migs = [(1, "CREATE TABLE t (a INTEGER);")]
    _apply_migrations(con, migs)
    v = _apply_migrations(con, migs)  # must not re-run the CREATE (would error)
    assert v == 1, f"expected version 1, got {v}"


def test_incremental_only_new_runs():
    con = sqlite3.connect(":memory:")
    migs = [(1, "CREATE TABLE t (a INTEGER);")]
    _apply_migrations(con, migs)
    v = _apply_migrations(con, migs + [(2, "ALTER TABLE t ADD COLUMN b INTEGER;")])
    assert v == 2, f"expected version 2, got {v}"
    assert "b" in _cols(con, "t")


def test_existing_preversion_db_advances_safely():
    # A db created before this system: table exists, user_version still 0.
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (a INTEGER)")
    con.execute("INSERT INTO t VALUES (42)")
    v = _apply_migrations(con, [(1, "CREATE TABLE IF NOT EXISTS t (a INTEGER);")])
    assert v == 1, f"expected version 1, got {v}"
    assert con.execute("SELECT a FROM t").fetchone()[0] == 42, "existing data lost"


def test_real_init_db_sets_baseline_and_bootstraps():
    init_db()
    con = connect()
    try:
        v = con.execute("PRAGMA user_version").fetchone()[0]
        assert v == max(m[0] for m in MIGRATIONS), f"user_version {v}"
        tables = {r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("users", "allowlist", "sessions", "login_tokens", "meta"):
            assert t in tables, f"missing table {t}"
        admins = con.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0]
        assert admins >= 1, "bootstrap admin missing"
    finally:
        con.close()


def run():
    print("app.db migration contract:")
    check("fresh db applies all migrations + sets user_version",
          test_fresh_applies_all_and_sets_version)
    check("re-running migrations is idempotent", test_idempotent_rerun)
    check("only newly-added migrations run on re-apply", test_incremental_only_new_runs)
    check("pre-version db advances safely, data preserved",
          test_existing_preversion_db_advances_safely)
    check("real init_db sets baseline version + tables + bootstrap",
          test_real_init_db_sets_baseline_and_bootstraps)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL MIGRATION TESTS PASSED")


if __name__ == "__main__":
    run()
