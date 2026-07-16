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
from app.seeds import SEED_LESSON_REWRITES

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


def test_migration_3_adds_lesson_and_backfills_from_notes():
    # Bring a db up to version 2 (skills table exists, no `lesson` column yet),
    # insert a legacy row whose `notes` reads as a rule, then apply the real
    # migration 3 and confirm it adds the column and backfills lesson=notes.
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 2])
    assert "lesson" not in _cols(con, "skills")
    con.execute("INSERT INTO skills(question, canonical_sql, notes, created_at) "
                "VALUES ('q', 'SELECT 1', 'use cipcode=99 for totals', 0)")
    con.commit()
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    assert "lesson" in _cols(con, "skills")
    lesson = con.execute("SELECT lesson FROM skills").fetchone()[0]
    assert lesson == "use cipcode=99 for totals", lesson


def test_migration_4_adds_year_provenance_table():
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 3])
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "year_provenance" not in tables, tables
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "year_provenance" in tables, tables
    cols = _cols(con, "year_provenance")
    for c in ("start_year", "end_year", "release", "source", "updated_at"):
        assert c in cols, f"year_provenance missing column {c!r}: {cols}"
    # release must be nullable (NULL = unknown / manual import).
    con.execute("INSERT INTO year_provenance(start_year, end_year, release, source, "
               "updated_at) VALUES (2025, 2026, NULL, 'manual', 0)")
    row = con.execute("SELECT release FROM year_provenance WHERE start_year=2025").fetchone()
    assert row[0] is None, row
    # start_year is the primary key -> a duplicate insert without a conflict
    # clause must raise (pins the schema's uniqueness constraint).
    try:
        con.execute("INSERT INTO year_provenance(start_year, end_year, release, "
                   "source, updated_at) VALUES (2025, 2026, 'Final', 'nces', 1)")
        raise AssertionError("expected a UNIQUE/PK violation on duplicate start_year")
    except sqlite3.IntegrityError:
        pass


def test_migration_5_adds_import_jobs_progress_column():
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 4])
    assert "progress" not in _cols(con, "import_jobs"), _cols(con, "import_jobs")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    assert "progress" in _cols(con, "import_jobs"), _cols(con, "import_jobs")
    # New column must be nullable (existing rows aren't backfilled with JSON).
    con.execute("INSERT INTO import_jobs(filename, status, created_at, updated_at) "
               "VALUES ('x', 'pending', 0, 0)")
    row = con.execute("SELECT progress FROM import_jobs WHERE filename='x'").fetchone()
    assert row[0] is None, row


def test_fresh_db_advances_to_baseline_version_with_all_new_objects():
    con = sqlite3.connect(":memory:")
    v = _apply_migrations(con, MIGRATIONS)
    expected = max(m[0] for m in MIGRATIONS)
    assert v == expected, f"expected baseline user_version {expected}, got {v}"
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "year_provenance" in tables, tables
    assert "progress" in _cols(con, "import_jobs"), _cols(con, "import_jobs")


def test_migration_6_rewrites_terse_seed_lessons():
    # Bring a db up to version 5 (skills table exists, pre-rewrite), insert one
    # row still bearing an OLD terse seed lesson and one admin-edited row whose
    # lesson isn't in the rewrite map, then apply migration 6 and confirm only
    # the terse row is rewritten (lesson AND notes), the edited row is untouched.
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 5])
    old_text, new_text = SEED_LESSON_REWRITES[0]
    con.execute(
        "INSERT INTO skills(question, canonical_sql, notes, lesson, created_by, "
        "created_at) VALUES ('q1', 'SELECT 1', ?, ?, 'seed', 0)",
        (old_text, old_text))
    edited_text = "An admin rewrote this seed lesson to say something else entirely."
    con.execute(
        "INSERT INTO skills(question, canonical_sql, notes, lesson, created_by, "
        "created_at) VALUES ('q2', 'SELECT 1', ?, ?, 'seed', 0)",
        (edited_text, edited_text))
    con.commit()
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    row1 = con.execute("SELECT lesson, notes FROM skills WHERE question='q1'").fetchone()
    assert row1[0] == new_text, row1[0]
    assert row1[1] == new_text, row1[1]
    row2 = con.execute("SELECT lesson, notes FROM skills WHERE question='q2'").fetchone()
    assert row2[0] == edited_text, "admin-edited seed lesson must not be rewritten"
    assert row2[1] == edited_text, "admin-edited seed notes must not be rewritten"


def test_migration_6_is_idempotent_and_noop_on_fresh_db():
    # A fresh install has no skills rows yet (seeding happens AFTER migrations),
    # so migration 6 must be a harmless no-op, and re-applying must not error.
    con = sqlite3.connect(":memory:")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    count = con.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    assert count == 0, "fresh db should have no skills rows before seeding"
    v2 = _apply_migrations(con, MIGRATIONS)  # re-apply: must not error, no-op
    assert v2 == v, f"expected version to stay {v}, got {v2}"


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
    check("migration 3 adds skills.lesson + backfills from notes",
          test_migration_3_adds_lesson_and_backfills_from_notes)
    check("migration 4 adds the year_provenance table (nullable release, PK start_year)",
          test_migration_4_adds_year_provenance_table)
    check("migration 5 adds import_jobs.progress (nullable)",
          test_migration_5_adds_import_jobs_progress_column)
    check("fresh db advances to the baseline version with all new objects",
          test_fresh_db_advances_to_baseline_version_with_all_new_objects)
    check("migration 6 rewrites terse seed lessons, leaves admin edits alone",
          test_migration_6_rewrites_terse_seed_lessons)
    check("migration 6 is idempotent and a no-op on a fresh (unseeded) db",
          test_migration_6_is_idempotent_and_noop_on_fresh_db)
    check("real init_db sets baseline version + tables + bootstrap",
          test_real_init_db_sets_baseline_and_bootstraps)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL MIGRATION TESTS PASSED")


if __name__ == "__main__":
    run()
