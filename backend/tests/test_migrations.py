"""Schema-migration contract for app.db (_apply_migrations + init_db).

Verifies the PRAGMA user_version-based runner: fresh dbs get every migration,
re-runs are idempotent, only newly-added migrations apply, a pre-version db
(tables already present, user_version 0) advances without losing data, and the
real init_db lands at the baseline version with all tables + bootstrap intact.
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"

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


def test_migration_12_adds_messages_thinking_column():
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 11])
    assert "thinking" not in _cols(con, "messages"), _cols(con, "messages")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    assert "thinking" in _cols(con, "messages"), _cols(con, "messages")
    # Nullable: pre-migration assistant rows aren't backfilled, and user rows
    # never carry a trace — the client maps NULL -> [] (no Thinking toggle).
    con.execute("INSERT INTO conversations(user_id, title, created_at, updated_at) "
               "VALUES (1, 't', 0, 0)")
    con.execute("INSERT INTO messages(conversation_id, role, content, created_at) "
               "VALUES (1, 'user', 'q', 0)")
    row = con.execute("SELECT thinking FROM messages WHERE role='user'").fetchone()
    assert row[0] is None, row


def test_migration_13_14_add_figure_columns():
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 12])
    assert "figure" not in _cols(con, "messages"), _cols(con, "messages")
    assert "figure" not in _cols(con, "query_cache"), _cols(con, "query_cache")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    # The hero statistic is persisted both on the message (survives reload) and in
    # the answer cache (a repeated question shows the same figure).
    assert "figure" in _cols(con, "messages"), _cols(con, "messages")
    assert "figure" in _cols(con, "query_cache"), _cols(con, "query_cache")


def test_migration_15_16_add_suggestions_columns():
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 14])
    assert "suggestions" not in _cols(con, "messages"), _cols(con, "messages")
    assert "suggestions" not in _cols(con, "query_cache"), _cols(con, "query_cache")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    # Drill-down chips are persisted on the message (survives reload) and in the
    # answer cache (a repeated question shows the same chips).
    assert "suggestions" in _cols(con, "messages"), _cols(con, "messages")
    assert "suggestions" in _cols(con, "query_cache"), _cols(con, "query_cache")


def test_migration_20_adds_clarify_column():
    # The disambiguation "clarify" turn's structured {question, options[]} payload
    # (parsed from the model's ```clarify fence), persisted on the assistant
    # message like figure/suggestions — so a reload shows the same clarifying
    # question + chips, not just the live in-session turn. Deliberately NO
    # query_cache.clarify column (unlike figure/suggestions): a clarify turn is
    # never cached (see backend/tests/test_chat_router.py).
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 19])
    assert "clarify" not in _cols(con, "messages"), _cols(con, "messages")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    assert "clarify" in _cols(con, "messages"), _cols(con, "messages")
    # Nullable: pre-migration rows aren't backfilled, and a normal (non-clarify)
    # answer never carries one — the client maps NULL -> no clarify chips.
    con.execute("INSERT INTO conversations(user_id, title, created_at, updated_at) "
               "VALUES (1, 't', 0, 0)")
    con.execute("INSERT INTO messages(conversation_id, role, content, created_at) "
               "VALUES (1, 'assistant', 'a', 0)")
    row = con.execute("SELECT clarify FROM messages WHERE role='assistant'").fetchone()
    assert row[0] is None, row


def test_migration_21_adds_usage_log_figure_grounding_column():
    # Observe-only figure-grounding status (app/grounding.py): whether the turn's
    # hero figure could be reproduced from the query results the turn actually
    # ran. NULLABLE by design — a turn that ran no query (an answer-cache hit, a
    # guard refusal) has nothing to ground against, and counting those as either
    # grounded or ungrounded would bias the rate the admin dashboard reports.
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 20])
    assert "figure_grounding" not in _cols(con, "usage_log"), _cols(con, "usage_log")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    assert "figure_grounding" in _cols(con, "usage_log"), _cols(con, "usage_log")
    # A pre-migration row is not backfilled, and an INSERT that omits the column
    # must still succeed (the cache/refusal paths do exactly that).
    con.execute("INSERT INTO usage_log(user_id, question, created_at) "
                "VALUES (1, 'q', 0)")
    row = con.execute("SELECT figure_grounding FROM usage_log").fetchone()
    assert row[0] is None, row


def test_migration_22_adds_usage_log_figure_derivation_column():
    # HOW the figure was reproduced ("pct_change(q1.awards)"), beside the status
    # from migration 21. The status alone cannot separate a real derivation from
    # a lucky collision across the searched ops — the exact question the
    # observe-only period exists to answer — so an 'exact' and a coincidental
    # 'derived' would otherwise be indistinguishable in the recorded data.
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 21])
    assert "figure_derivation" not in _cols(con, "usage_log"), _cols(con, "usage_log")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    assert "figure_derivation" in _cols(con, "usage_log"), _cols(con, "usage_log")
    # Nullable: an 'ungrounded' turn matched no derivation, and an unchecked one
    # ran no check at all — neither has anything to record.
    con.execute("INSERT INTO usage_log(user_id, question, figure_grounding, "
                "created_at) VALUES (1, 'q', 'ungrounded', 0)")
    row = con.execute("SELECT figure_derivation FROM usage_log").fetchone()
    assert row[0] is None, row


def test_migration_23_adds_messages_results_column():
    # Each turn's run_sql results (JSON list of {columns, rows}, capped), so a
    # LATER turn can ground a figure against an EARLIER turn's data
    # (conversation-scoped grounding, app/grounding.py). Backend-only; NULL on a
    # turn that ran no query (cache hit / refusal / clarify) or predates it.
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 22])
    assert "results" not in _cols(con, "messages"), _cols(con, "messages")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    assert "results" in _cols(con, "messages"), _cols(con, "messages")
    # Nullable: a pre-migration row and a no-query turn carry no results.
    con.execute("INSERT INTO conversations(user_id, title, created_at, updated_at) "
                "VALUES (1, 't', 0, 0)")
    con.execute("INSERT INTO messages(conversation_id, role, content, created_at) "
                "VALUES (1, 'assistant', 'a', 0)")
    row = con.execute("SELECT results FROM messages WHERE role='assistant'").fetchone()
    assert row[0] is None, row


def test_migration_24_adds_usage_log_emit_mode_and_leak_columns():
    # Structured-emission telemetry (PR-1): emit_mode ('structured'|'fence') and
    # answer_leaked (the sentinel). Together they prove structured emission drives
    # the leak rate to 0 before the default flips.
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 23])
    assert "emit_mode" not in _cols(con, "usage_log"), _cols(con, "usage_log")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    cols = _cols(con, "usage_log")
    assert "emit_mode" in cols and "answer_leaked" in cols, cols
    # emit_mode nullable (predating rows / cache hits), answer_leaked defaults 0.
    con.execute("INSERT INTO usage_log(user_id, question, created_at) VALUES (1,'q',0)")
    row = con.execute("SELECT emit_mode, answer_leaked FROM usage_log").fetchone()
    assert row[0] is None and row[1] == 0, row


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


def test_migration_7_adds_headline_column():
    # Generalized structured lessons: skills gains a nullable `headline` column
    # (short generalized rule title). Pure DDL, no backfill (the Python
    # upgrade_seed_lessons()/reembed_skills_if_needed() backfills handle text +
    # embeddings after migrations run, at app startup).
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 6])
    assert "headline" not in _cols(con, "skills"), _cols(con, "skills")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    # Baseline bumped 9 -> 10 by round 3 (migration 10: an expression index
    # for is_denied's COALESCE predicate -- see
    # test_migration_10_adds_expression_index_used_by_is_denied below).
    assert "headline" in _cols(con, "skills"), _cols(con, "skills")
    # New column must be nullable (existing rows aren't backfilled by the DDL).
    con.execute("INSERT INTO skills(question, canonical_sql, created_at) "
               "VALUES ('q', 'SELECT 1', 0)")
    row = con.execute("SELECT headline FROM skills WHERE question='q'").fetchone()
    assert row[0] is None, row


def test_access_requests_email_index_exists():
    """Migration 8: is_denied() (backend/app/auth.py) runs a per-address lookup on
    access_requests on EVERY unauthenticated POST /api/auth/request, and the
    table is attacker-growable (an unauth caller can insert rows), so the
    lookup must be indexed rather than a full scan."""
    con = sqlite3.connect(":memory:")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    idx_names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_access_requests_email" in idx_names, \
        f"expected idx_access_requests_email among {idx_names}"

    # Re-applying against an already-migrated db must be a safe no-op.
    v2 = _apply_migrations(con, MIGRATIONS)
    assert v2 == v, f"expected version to stay {v}, got {v2}"
    idx_names_after = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_access_requests_email" in idx_names_after, idx_names_after


def test_migration_9_adds_canon_email_column_index_and_backfills():
    """FIX ROUND -- Defect 2 (HIGH, security review, CONFIRMED): exact-string
    matching is fail-OPEN for a denylist (an attacker bypasses a denial by
    adding "+anything" to their address). The fix stores a CANONICAL form
    (lowercase + a `+tag` local-part suffix stripped -- dots are deliberately
    NOT stripped, see the behavioral tests in backend/tests/test_admin_router.py) in a
    new indexed `canon_email` column, backfilled for pre-existing rows.

    Seeds a row directly at the pre-migration-9 schema (simulating real
    production data written before this migration existed) and confirms the
    migration both adds the column/index AND backfills that row correctly.
    Only the BACKFILL is pinned here (a schema-level, migration-owned
    concern) -- whether/how a freshly-inserted row gets its canon_email
    populated going forward is an application-level concern tested through
    the real endpoints in backend/tests/test_admin_router.py and
    backend/tests/test_access_gate.py, not here."""
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 8])
    assert "canon_email" not in _cols(con, "access_requests"), _cols(con, "access_requests")
    con.execute(
        "INSERT INTO access_requests(email, status, created_at) VALUES (?,?,?)",
        ("mallory+old@example.edu", "pending", 0))
    con.commit()

    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    # Baseline bumped 9 -> 10 by round 3 (migration 10: an expression index
    # for is_denied's COALESCE predicate -- see
    # test_migration_10_adds_expression_index_used_by_is_denied below).
    assert "canon_email" in _cols(con, "access_requests"), _cols(con, "access_requests")

    idx_names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert any("canon_email" in n for n in idx_names), \
        f"expected an index on access_requests.canon_email among {idx_names}"

    row = con.execute(
        "SELECT canon_email FROM access_requests WHERE email='mallory+old@example.edu'"
    ).fetchone()
    assert row[0] == "mallory@example.edu", (
        f"a pre-existing row's canon_email must be BACKFILLED to the "
        f"canonical form (lowercase, +tag stripped), got {row[0]!r}")

    # Re-applying against an already-migrated db must be a safe no-op.
    v2 = _apply_migrations(con, MIGRATIONS)
    assert v2 == v, f"expected version to stay {v}, got {v2}"
    row_after = con.execute(
        "SELECT canon_email FROM access_requests WHERE email='mallory+old@example.edu'"
    ).fetchone()
    assert row_after[0] == "mallory@example.edu", row_after


# ---------------------------------------------------------------------------
# Round 3 (.plan-undeny.md) -- FOLDED-IN FIX 2: migration 9's
# idx_access_requests_canon_email is a PLAIN column index, but is_denied()'s
# predicate wraps the column in COALESCE(canon_email, LOWER(email)) -- SQLite
# cannot match a plain index to an expression, so the lookup that migration 9
# was written to protect (an unauthenticated, attacker-growable hot path)
# still full-table-SCANs. Measured directly (see the test below): migration 9
# alone -> SCAN; add an index on the EXPRESSION -> SEARCH. Migration 10 adds
# that expression index. RED today (migration 10 doesn't exist yet).
# ---------------------------------------------------------------------------

def test_migration_10_adds_expression_index_used_by_is_denied():
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 9])
    idx_before = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_access_requests_canon_expr" not in idx_before, idx_before

    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    idx_after = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_access_requests_canon_expr" in idx_after, (
        f"expected a NEW expression index idx_access_requests_canon_expr "
        f"among {idx_after}")
    # Migration 9's plain-column index must NOT be dropped/renamed -- never
    # edit a shipped migration (CLAUDE.md / MIGRATIONS' own header comment).
    assert "idx_access_requests_canon_email" in idx_after, idx_after

    # Re-applying against an already-migrated db must be a safe no-op.
    v2 = _apply_migrations(con, MIGRATIONS)
    assert v2 == v, f"expected version to stay {v}, got {v2}"
    idx_after2 = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert idx_after2 == idx_after, idx_after2


def test_is_denied_lookup_uses_an_index_not_a_scan():
    """The ONLY test in this suite that can catch the real regression this
    feature is fragile to: the predicate here and migration 10's index
    EXPRESSION drifting apart. That drift fails SILENTLY -- correct query
    results either way -- so nothing about correctness would ever flag it;
    only an EXPLAIN QUERY PLAN check on the exact predicate does. The SQL
    below is copy-pasted character-for-character from app.auth.is_denied's
    query (see backend/app/auth.py) -- if a future edit changes one and not the
    other, THIS test goes red, not is_denied's own behavioral suite (which
    only checks results, and would stay green through a full-scan
    regression).

    RED today: migration 10 (the expression index) doesn't exist yet, so
    this plans as a SCAN. Verified independently with sqlite3 CLI against
    the shipped migration 9 schema before writing this test."""
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, MIGRATIONS)
    plan = con.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM access_requests "
        "WHERE status='denied' AND COALESCE(canon_email, LOWER(email))=? LIMIT 1",
        ("someone@example.edu",)).fetchall()
    plan_text = " ".join(str(row[-1]) for row in plan)
    assert "SCAN" not in plan_text, (
        f"is_denied's unauthenticated hot-path predicate must not full-scan "
        f"access_requests -- got plan: {plan_text!r}")
    assert "SEARCH" in plan_text, (
        f"expected an index SEARCH in the query plan, got: {plan_text!r}")


# ---------------------------------------------------------------------------
# Schema-drift guard (release hardening). Every OTHER test in this file drives
# the schema THROUGH `MIGRATIONS`, so none of them catch the one mistake that
# silently breaks upgrades: editing the FROZEN baseline `SCHEMA` (or a shipped
# migration) instead of appending a new migration tuple. A column added straight
# to `SCHEMA` reaches a FRESH install (which runs SCHEMA as migration 1) but
# NEVER reaches an UPGRADED install (no migration adds it) -- the two diverge,
# and because both answer queries fine the divergence is invisible. The golden
# fingerprint below turns any app.db schema change into a readable, reviewable
# diff of EXPECTED_SCHEMA_FINGERPRINT, forcing the "did this need a migration?"
# checkpoint. Regenerate it deliberately: `python test_migrations.py --print-schema`.
# ---------------------------------------------------------------------------

def _schema_fingerprint(con):
    """A deterministic, structural fingerprint of the whole app.db schema.

    PRAGMA-based (not raw `sqlite_master.sql` text, which is whitespace-fragile):
    every user table -> its columns as (name, type, notnull, dflt_value, pk)
    sorted by column name; every index -> (table, unique, [indexed columns]).
    An EXPRESSION index (idx_access_requests_canon_expr) reports [null] columns
    -- expected and stable; the index name still anchors its existence."""
    tables = {}
    for (name,) in sorted(con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")):
        tables[name] = sorted(
            [list(r[1:]) for r in con.execute(f"PRAGMA table_info({name})")],
            key=lambda c: c[0])
    indexes = {}
    for (name, tbl) in sorted(con.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%'")):
        info = con.execute(f"PRAGMA index_list({tbl})").fetchall()
        unique = next((r[2] for r in info if r[1] == name), 0)
        icols = [r[2] for r in con.execute(f"PRAGMA index_info({name})")]
        indexes[name] = {"table": tbl, "unique": unique, "columns": icols}
    return {"tables": tables, "indexes": indexes}


def _current_fingerprint():
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, MIGRATIONS)
    return _schema_fingerprint(con)


# The expected app.db schema after ALL migrations apply. A change here is a
# CHECKPOINT, not a chore: regenerate with `python test_migrations.py
# --print-schema` and confirm the change shipped as a NEW migration tuple (never
# an edit to the frozen SCHEMA or a shipped migration). Compared as a parsed
# structure, so whitespace/formatting of this literal is irrelevant.
EXPECTED_SCHEMA_FINGERPRINT = json.loads(r"""
{
  "indexes": {
    "idx_access_requests_canon_email":
      {"table": "access_requests", "unique": 0, "columns": ["canon_email"]},
    "idx_access_requests_canon_expr":
      {"table": "access_requests", "unique": 0, "columns": [null]},
    "idx_access_requests_email":
      {"table": "access_requests", "unique": 0, "columns": ["email"]},
    "idx_auth_attempts_created":
      {"table": "auth_request_attempts", "unique": 0, "columns": ["created_at"]},
    "ix_conv_user": {"table": "conversations", "unique": 0, "columns": ["user_id", "updated_at"]},
    "ix_msg_conv": {"table": "messages", "unique": 0, "columns": ["conversation_id", "id"]},
    "ix_usage_time": {"table": "usage_log", "unique": 0, "columns": ["created_at"]}
  },
  "tables": {
    "access_requests": [
      ["canon_email", "TEXT", 0, null, 0],
      ["created_at", "REAL", 1, null, 0],
      ["denied_at", "REAL", 0, null, 0],
      ["email", "TEXT", 1, null, 0],
      ["id", "INTEGER", 0, null, 1],
      ["reason", "TEXT", 0, null, 0],
      ["status", "TEXT", 1, "'pending'", 0]
    ],
    "admin_log_seen": [
      ["email", "TEXT", 0, null, 1],
      ["seen_ts", "REAL", 1, null, 0]
    ],
    "allowlist": [
      ["added_at", "REAL", 1, null, 0],
      ["added_by", "TEXT", 0, null, 0],
      ["email", "TEXT", 0, null, 1],
      ["note", "TEXT", 0, null, 0]
    ],
    "auth_request_attempts": [
      ["created_at", "REAL", 1, null, 0],
      ["email", "TEXT", 1, null, 0],
      ["ip", "TEXT", 1, null, 0]
    ],
    "conversations": [
      ["created_at", "REAL", 1, null, 0],
      ["id", "INTEGER", 0, null, 1],
      ["title", "TEXT", 0, null, 0],
      ["updated_at", "REAL", 1, null, 0],
      ["user_id", "INTEGER", 1, null, 0]
    ],
    "import_jobs": [
      ["created_at", "REAL", 1, null, 0],
      ["created_by", "TEXT", 0, null, 0],
      ["filename", "TEXT", 0, null, 0],
      ["id", "INTEGER", 0, null, 1],
      ["log", "TEXT", 0, null, 0],
      ["progress", "TEXT", 0, null, 0],
      ["report", "TEXT", 0, null, 0],
      ["status", "TEXT", 1, "'pending'", 0],
      ["updated_at", "REAL", 1, null, 0]
    ],
    "login_tokens": [
      ["email", "TEXT", 1, null, 0],
      ["expires_at", "REAL", 1, null, 0],
      ["token_hash", "TEXT", 0, null, 1],
      ["used_at", "REAL", 0, null, 0]
    ],
    "messages": [
      ["clarify", "TEXT", 0, null, 0],
      ["content", "TEXT", 1, null, 0],
      ["conversation_id", "INTEGER", 1, null, 0],
      ["created_at", "REAL", 1, null, 0],
      ["feedback", "INTEGER", 0, null, 0],
      ["figure", "TEXT", 0, null, 0],
      ["id", "INTEGER", 0, null, 1],
      ["model_used", "TEXT", 0, null, 0],
      ["results", "TEXT", 0, null, 0],
      ["role", "TEXT", 1, null, 0],
      ["sql_log", "TEXT", 0, null, 0],
      ["suggestions", "TEXT", 0, null, 0],
      ["thinking", "TEXT", 0, null, 0],
      ["tokens", "INTEGER", 0, null, 0]
    ],
    "meta": [
      ["key", "TEXT", 0, null, 1],
      ["value", "TEXT", 0, null, 0]
    ],
    "query_cache": [
      ["answer_md", "TEXT", 0, null, 0],
      ["created_at", "REAL", 1, null, 0],
      ["data_version", "INTEGER", 1, null, 0],
      ["embedding", "BLOB", 0, null, 0],
      ["figure", "TEXT", 0, null, 0],
      ["final_sql", "TEXT", 0, null, 0],
      ["id", "INTEGER", 0, null, 1],
      ["question", "TEXT", 1, null, 0],
      ["suggestions", "TEXT", 0, null, 0]
    ],
    "sessions": [
      ["created_at", "REAL", 1, null, 0],
      ["expires_at", "REAL", 1, null, 0],
      ["token_hash", "TEXT", 0, null, 1],
      ["user_id", "INTEGER", 1, null, 0]
    ],
    "skills": [
      ["canonical_sql", "TEXT", 1, null, 0],
      ["created_at", "REAL", 1, null, 0],
      ["created_by", "TEXT", 0, null, 0],
      ["downvotes", "INTEGER", 1, "0", 0],
      ["embedding", "BLOB", 0, null, 0],
      ["headline", "TEXT", 0, null, 0],
      ["hits", "INTEGER", 1, "0", 0],
      ["id", "INTEGER", 0, null, 1],
      ["lesson", "TEXT", 0, null, 0],
      ["notes", "TEXT", 0, null, 0],
      ["question", "TEXT", 1, null, 0],
      ["tags", "TEXT", 0, null, 0],
      ["upvotes", "INTEGER", 1, "0", 0],
      ["verified", "INTEGER", 1, "0", 0]
    ],
    "usage_log": [
      ["answer_leaked", "INTEGER", 1, "0", 0],
      ["cached", "INTEGER", 1, "0", 0],
      ["cached_prompt_tokens", "INTEGER", 1, "0", 0],
      ["completion_tokens", "INTEGER", 0, null, 0],
      ["cost", "REAL", 1, "0", 0],
      ["created_at", "REAL", 1, null, 0],
      ["emit_mode", "TEXT", 0, null, 0],
      ["escalated", "INTEGER", 0, null, 0],
      ["figure_derivation", "TEXT", 0, null, 0],
      ["figure_grounding", "TEXT", 0, null, 0],
      ["first_call_cached_prompt_tokens", "INTEGER", 1, "0", 0],
      ["first_call_prompt_tokens", "INTEGER", 1, "0", 0],
      ["id", "INTEGER", 0, null, 1],
      ["model_used", "TEXT", 0, null, 0],
      ["ok", "INTEGER", 0, null, 0],
      ["prompt_tokens", "INTEGER", 0, null, 0],
      ["question", "TEXT", 0, null, 0],
      ["user_id", "INTEGER", 0, null, 0]
    ],
    "users": [
      ["created_at", "REAL", 1, null, 0],
      ["email", "TEXT", 1, null, 0],
      ["id", "INTEGER", 0, null, 1],
      ["is_admin", "INTEGER", 1, "0", 0],
      ["last_login", "REAL", 0, null, 0]
    ],
    "year_provenance": [
      ["end_year", "INTEGER", 1, null, 0],
      ["release", "TEXT", 0, null, 0],
      ["source", "TEXT", 0, null, 0],
      ["start_year", "INTEGER", 0, null, 1],
      ["updated_at", "REAL", 1, null, 0]
    ]
  }
}
""")


def test_migration_versions_are_contiguous():
    """Version numbers must be 1..N with no gap, duplicate, or renumber -- the
    runner keys off `> user_version`, so a gap would silently skip a migration
    and a duplicate/renumber means a shipped migration was edited in place."""
    versions = [v for v, _ in sorted(MIGRATIONS)]
    assert versions == list(range(1, len(MIGRATIONS) + 1)), (
        f"migration versions must be contiguous 1..{len(MIGRATIONS)}, got {versions}")


def test_fresh_schema_matches_golden_fingerprint():
    """DRIFT GUARD: the full post-migration app.db schema must match the checked-in
    golden fingerprint. Goes RED on ANY schema change -- an edited shipped
    migration, or (the invisible one) a column added straight to the frozen
    SCHEMA constant without a new migration. Regenerate deliberately via
    `python test_migrations.py --print-schema` and confirm the change shipped as
    a new migration tuple."""
    con = sqlite3.connect(":memory:")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), f"user_version {v}"
    actual = _schema_fingerprint(con)
    assert actual == EXPECTED_SCHEMA_FINGERPRINT, (
        "app.db schema drifted from the golden fingerprint. If this change is "
        "INTENTIONAL, it must ship as a NEW migration tuple (never an edit to the "
        "frozen SCHEMA or a shipped migration) -- then regenerate the golden value "
        "with `python backend/tests/test_migrations.py --print-schema`.\n"
        f"actual:\n{json.dumps(actual, sort_keys=True, indent=2)}")


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
    check("migration 12 adds messages.thinking (nullable)",
          test_migration_12_adds_messages_thinking_column)
    check("migration 13+14 add figure columns (messages + query_cache)",
          test_migration_13_14_add_figure_columns)
    check("migration 15+16 add suggestions columns (messages + query_cache)",
          test_migration_15_16_add_suggestions_columns)
    check("migration 20 adds messages.clarify (nullable, no query_cache column)",
          test_migration_20_adds_clarify_column)
    check("migration 21 adds usage_log.figure_grounding (nullable)",
          test_migration_21_adds_usage_log_figure_grounding_column)
    check("migration 22 adds usage_log.figure_derivation (nullable)",
          test_migration_22_adds_usage_log_figure_derivation_column)
    check("migration 23 adds messages.results (nullable)",
          test_migration_23_adds_messages_results_column)
    check("migration 24 adds usage_log.emit_mode + answer_leaked",
          test_migration_24_adds_usage_log_emit_mode_and_leak_columns)
    check("fresh db advances to the baseline version with all new objects",
          test_fresh_db_advances_to_baseline_version_with_all_new_objects)
    check("migration 6 rewrites terse seed lessons, leaves admin edits alone",
          test_migration_6_rewrites_terse_seed_lessons)
    check("migration 6 is idempotent and a no-op on a fresh (unseeded) db",
          test_migration_6_is_idempotent_and_noop_on_fresh_db)
    check("migration 7 adds skills.headline (nullable)",
          test_migration_7_adds_headline_column)
    check("migration 8 adds idx_access_requests_email (idempotent re-apply)",
          test_access_requests_email_index_exists)
    check("migration 9 adds access_requests.canon_email + index, backfills existing rows",
          test_migration_9_adds_canon_email_column_index_and_backfills)
    check("migration 10 adds an expression index for is_denied's COALESCE predicate "
          "(fold-in fix 2)", test_migration_10_adds_expression_index_used_by_is_denied)
    check("is_denied's exact predicate plans as a SEARCH, not a SCAN (fold-in fix 2)",
          test_is_denied_lookup_uses_an_index_not_a_scan)
    check("real init_db sets baseline version + tables + bootstrap",
          test_real_init_db_sets_baseline_and_bootstraps)
    check("migration versions are contiguous 1..N (no gap/dup/renumber)",
          test_migration_versions_are_contiguous)
    check("fresh schema matches the golden fingerprint (drift guard)",
          test_fresh_schema_matches_golden_fingerprint)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL MIGRATION TESTS PASSED")


if __name__ == "__main__":
    if "--print-schema" in sys.argv:
        # Regen path for the golden fingerprint: paste this into
        # EXPECTED_SCHEMA_FINGERPRINT after an INTENTIONAL, migration-backed
        # schema change.
        print(json.dumps(_current_fingerprint(), sort_keys=True, indent=2))
    else:
        run()
