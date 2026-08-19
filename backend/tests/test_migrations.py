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

from app.db import (
    _BOOTSTRAP_APPLIED_KEY,
    MIGRATIONS,
    SchemaTooNewError,
    _apply_migrations,
    _bootstrap_admins,
    _prune_snapshots,
    connect,
    get_meta,
    init_db,
    set_meta,
)
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


def test_migration_25_adds_usage_log_table_grounding_columns():
    # Table grounding (observe-only): the per-turn status plus the numeric-cell
    # counts that drive Admin -> Usage's cell-level rate.
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 24])
    assert "table_grounding" not in _cols(con, "usage_log"), _cols(con, "usage_log")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    cols = _cols(con, "usage_log")
    assert "table_grounding" in cols, cols
    assert "table_cells_checked" in cols and "table_cells_matched" in cols, cols
    # status nullable (predating rows / cache hits); the counts default to 0 so a
    # predating row contributes 0/0 to the SUM-based rate rather than 0/N.
    con.execute("INSERT INTO usage_log(user_id, question, created_at) VALUES (1,'q',0)")
    row = con.execute(
        "SELECT table_grounding, table_cells_checked, table_cells_matched "
        "FROM usage_log").fetchone()
    assert row[0] is None and row[1] == 0 and row[2] == 0, row


def test_migration_26_adds_messages_duration_ms():
    # Turn duration (ms) on the assistant message — the "Thought for N seconds"
    # display. Nullable (NULL on cache/refusal/predating rows).
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 25])
    assert "duration_ms" not in _cols(con, "messages"), _cols(con, "messages")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    assert "duration_ms" in _cols(con, "messages"), _cols(con, "messages")
    con.execute("INSERT INTO conversations(user_id, title, created_at, updated_at) "
                "VALUES (1, 't', 0, 0)")
    con.execute("INSERT INTO messages(conversation_id, role, content, created_at) "
                "VALUES (1, 'user', 'q', 0)")
    row = con.execute("SELECT duration_ms FROM messages").fetchone()
    assert row[0] is None, row  # nullable, no backfill


def test_migration_27_adds_usage_log_exhaustion_column():
    # Tool-budget exhaustion status (app/llm.py, S5): NULL = didn't exhaust;
    # 'answered' = exhausted but shipped a synthesis; 'degraded' = exhausted and the
    # grounding gate replaced fabricated numbers. Drives Admin -> Usage "Exhausted".
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 26])
    assert "exhaustion" not in _cols(con, "usage_log"), _cols(con, "usage_log")
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    assert "exhaustion" in _cols(con, "usage_log"), _cols(con, "usage_log")
    # Nullable, no backfill: a predating row (and every non-exhausted turn) is NULL.
    con.execute("INSERT INTO usage_log(user_id, question, created_at) VALUES (1, 'q', 0)")
    row = con.execute("SELECT exhaustion FROM usage_log").fetchone()
    assert row[0] is None, row


def test_migration_28_adds_chat_request_attempts_table():
    # Per-user chat throttle (SEC-3, app/ratelimit.py enforce_chat_rate_limit):
    # a sliding-window table mirroring auth_request_attempts. New in migration 28.
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 27])
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "chat_request_attempts" not in tables, tables
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    assert "user_id" in _cols(con, "chat_request_attempts"), _cols(con, "chat_request_attempts")
    assert "created_at" in _cols(con, "chat_request_attempts"), \
        _cols(con, "chat_request_attempts")
    # The window-sweep index exists.
    idx = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_chat_attempts_created" in idx, idx


def test_migration_32_drops_the_dead_messages_feedback_column():
    """The 👍/👎 column outlived its feature; migration 32 is the first DROP here.

    Two things make a DROP different from every migration above it, and both are
    asserted:

    1. CONVERGENCE. The baseline SCHEMA is frozen, so it still CREATEs the
       column — an install that runs SCHEMA as migration 1 must therefore end up
       identical to one upgraded from an old app.db. Removing the column from
       SCHEMA instead of shipping this migration would give a fresh install no
       column and an upgraded install a live one, and both answer queries fine,
       so the divergence would be invisible (the failure mode the golden
       fingerprint exists to force a checkpoint on).
    2. SQLite rewrites the whole table for a DROP COLUMN, so neighbouring data
       has to be proven intact rather than assumed.
    """
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 31])
    assert "feedback" in _cols(con, "messages"), (
        "precondition: the frozen baseline SCHEMA still creates the column")

    con.execute("INSERT INTO conversations(user_id, title, created_at, updated_at) "
                "VALUES (1, 't', 0, 0)")
    con.execute("INSERT INTO messages(conversation_id, role, content, sql_log, "
                "feedback, created_at) VALUES (1, 'assistant', 'kept', 'SELECT 1', 1, 7)")

    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    assert "feedback" not in _cols(con, "messages"), _cols(con, "messages")

    # The table rewrite preserved every other column of the existing row.
    row = con.execute("SELECT conversation_id, role, content, sql_log, created_at "
                      "FROM messages").fetchone()
    assert tuple(row) == (1, "assistant", "kept", "SELECT 1", 7), tuple(row)

    # Convergence: a FRESH database ends in the same shape as the upgraded one.
    fresh = sqlite3.connect(":memory:")
    _apply_migrations(fresh, MIGRATIONS)
    assert _cols(fresh, "messages") == _cols(con, "messages"), (
        "a fresh install and an upgraded install disagree on messages' columns")


def test_migration_33_adds_messages_table_grounding_columns():
    """Table grounding, persisted per message so the READER can see it.

    check_table already graded every measure cell on every turn, but the verdict
    landed only on usage_log — it drove an admin stat and nothing else, so the
    person about to put those numbers in a report learned nothing. Mirrors
    migration 31's messages.figure_grounding: STATUS + counts only, never the
    per-cell detail. Nullable because a cache hit and a refusal grade nothing,
    and NULL correctly renders no mark at all.
    """
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 32])
    for c in ("table_grounding", "table_cells_checked", "table_cells_matched"):
        assert c not in _cols(con, "messages"), (c, _cols(con, "messages"))
    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v
    for c in ("table_grounding", "table_cells_checked", "table_cells_matched"):
        assert c in _cols(con, "messages"), (c, _cols(con, "messages"))
    # An INSERT that omits them must still succeed and read back NULL — the
    # cache/refusal paths do exactly that, and NULL is what renders no mark.
    con.execute("INSERT INTO conversations(user_id, title, created_at, updated_at) "
                "VALUES (1, 't', 0, 0)")
    con.execute("INSERT INTO messages(conversation_id, role, content, created_at) "
                "VALUES (1, 'assistant', 'a', 0)")
    row = con.execute("SELECT table_grounding, table_cells_checked, table_cells_matched "
                      "FROM messages WHERE role='assistant'").fetchone()
    assert tuple(row) == (None, None, None), tuple(row)


def test_fresh_db_advances_to_baseline_version_with_all_new_objects():
    con = sqlite3.connect(":memory:")
    v = _apply_migrations(con, MIGRATIONS)
    expected = max(m[0] for m in MIGRATIONS)
    assert v == expected, f"expected baseline user_version {expected}, got {v}"
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "year_provenance" in tables, tables
    assert "chat_request_attempts" in tables, tables
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


def test_migration_36_relabels_historical_suppressed_figures():
    """The suppression fix (#330) corrected NEW rows only. A figure the retry
    forced, found ungrounded and WITHHELD was recorded as a plain `ungrounded`
    figure, which put a turn that shipped NO figure into the Grounded-figures
    denominator as a miss.

    Those rows are exactly identifiable — `figure_derivation` has recorded
    `retry:suppressed` all along — and on the real app.db there were 10 of them,
    the entire evidence base the fix was argued from. Without this backfill an
    admin viewing any window covering pre-upgrade history still reads the wrong
    rate, while `figures_suppressed` reads 0 for that period, so the new
    "· N suppressed" tail is missing on precisely the data that motivated it.

    The predicate must be BOTH columns: `figure_derivation='retry:suppressed'`
    alone would be right today, but pairing it with the old status is what keeps
    the statement idempotent and stops it touching a genuinely ungrounded figure
    that merely came from a retry (`retry:sum(...)`, `retry:value(...)` — 32 such
    rows exist and must keep their status)."""
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 35])
    con.execute("INSERT INTO usage_log(user_id, question, created_at, "
                "figure_grounding, figure_derivation) VALUES "
                "(1,'a',0,'ungrounded','retry:suppressed'),"        # relabel
                "(1,'b',0,'ungrounded','retry:sum(q1.awards)'),"    # keep: real miss
                "(1,'c',0,'ungrounded',NULL),"                      # keep: first-pass
                "(1,'d',0,'exact','retry:value(q1.n)')")            # keep: grounded
    con.commit()

    _apply_migrations(con, MIGRATIONS)

    got = dict(con.execute(
        "SELECT question, figure_grounding FROM usage_log").fetchall())
    assert got == {"a": "retry_suppressed", "b": "ungrounded",
                   "c": "ungrounded", "d": "exact"}, got


def test_migration_35_adds_category_and_lesson_rejections_table():
    """A2 (lesson-rejection memory): rejecting a lesson currently DELETEs the row
    outright with no trace, so skills._find_duplicate can never suppress the
    same proposal recurring -- the evidence a rejection should have left behind
    is destroyed by the rejection itself. Migration 35 adds a nullable
    skills.category (pre-existing/seed/feedback rows stay NULL -- only the
    critic path populates it going forward) and a lesson_rejections tombstone
    table (+ its created_at index) that a DELETE writes to BEFORE removing the
    skill row, so a near-identical candidate can be recognized and suppressed
    instead of silently duplicating the admin's already-rejected judgment."""
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, [m for m in MIGRATIONS if m[0] <= 34])
    assert "category" not in _cols(con, "skills"), _cols(con, "skills")
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "lesson_rejections" not in tables, tables

    v = _apply_migrations(con, MIGRATIONS)
    assert v == max(m[0] for m in MIGRATIONS), v

    assert "category" in _cols(con, "skills"), _cols(con, "skills")
    # Nullable: existing rows aren't backfilled by the DDL.
    con.execute("INSERT INTO skills(question, canonical_sql, created_at) "
               "VALUES ('q', 'SELECT 1', 0)")
    row = con.execute("SELECT category FROM skills WHERE question='q'").fetchone()
    assert row[0] is None, row

    lr_cols = _cols(con, "lesson_rejections")
    assert lr_cols == {"id", "headline", "description", "embedding", "category",
                       "created_by", "skill_id", "was_verified", "hits",
                       "created_at"}, lr_cols

    idx_names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "ix_lesson_rejections_created" in idx_names, idx_names

    # Deliberately NO foreign key on skill_id -- the referenced skill row is
    # gone by definition (that's the whole point of a tombstone), and
    # db.connect() sets PRAGMA foreign_keys=ON, so a real FK here would make
    # every DELETE .../skills/{id} that writes a tombstone fail outright.
    fks = con.execute("PRAGMA foreign_key_list(lesson_rejections)").fetchall()
    assert fks == [], f"lesson_rejections must carry NO foreign key on skill_id: {fks}"

    # was_verified/hits default to 0 and are NOT NULL; created_at is NOT NULL
    # with no default (every writer must supply it, like every other table here).
    info = {r[1]: r for r in con.execute("PRAGMA table_info(lesson_rejections)")}
    assert info["was_verified"][3] == 1 and info["was_verified"][4] == "0", info["was_verified"]
    assert info["hits"][3] == 1 and info["hits"][4] == "0", info["hits"]
    assert info["created_at"][3] == 1 and info["created_at"][4] is None, info["created_at"]

    # Re-applying against an already-migrated db is a safe no-op.
    v2 = _apply_migrations(con, MIGRATIONS)
    assert v2 == v, f"expected version to stay {v}, got {v2}"


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
    # edit a shipped migration (docs/ARCHITECTURE.md / MIGRATIONS' own header comment).
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
    "idx_access_requests_canon_email": {
      "columns": [
        "canon_email"
      ],
      "table": "access_requests",
      "unique": 0
    },
    "idx_access_requests_canon_expr": {
      "columns": [
        null
      ],
      "table": "access_requests",
      "unique": 0
    },
    "idx_access_requests_email": {
      "columns": [
        "email"
      ],
      "table": "access_requests",
      "unique": 0
    },
    "idx_auth_attempts_created": {
      "columns": [
        "created_at"
      ],
      "table": "auth_request_attempts",
      "unique": 0
    },
    "idx_chat_attempts_created": {
      "columns": [
        "created_at"
      ],
      "table": "chat_request_attempts",
      "unique": 0
    },
    "idx_query_cache_lookup": {
      "columns": [
        "data_version",
        "user_id"
      ],
      "table": "query_cache",
      "unique": 0
    },
    "ix_api_keys_user": {
      "columns": [
        "user_id"
      ],
      "table": "api_keys",
      "unique": 0
    },
    "ix_conv_user": {
      "columns": [
        "user_id",
        "updated_at"
      ],
      "table": "conversations",
      "unique": 0
    },
    "ix_lesson_rejections_created": {
      "columns": [
        "created_at"
      ],
      "table": "lesson_rejections",
      "unique": 0
    },
    "ix_mcp_attempts_created": {
      "columns": [
        "created_at"
      ],
      "table": "mcp_request_attempts",
      "unique": 0
    },
    "ix_msg_conv": {
      "columns": [
        "conversation_id",
        "id"
      ],
      "table": "messages",
      "unique": 0
    },
    "ix_usage_time": {
      "columns": [
        "created_at"
      ],
      "table": "usage_log",
      "unique": 0
    }
  },
  "tables": {
    "access_requests": [
      [
        "canon_email",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "created_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "denied_at",
        "REAL",
        0,
        null,
        0
      ],
      [
        "email",
        "TEXT",
        1,
        null,
        0
      ],
      [
        "id",
        "INTEGER",
        0,
        null,
        1
      ],
      [
        "reason",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "status",
        "TEXT",
        1,
        "'pending'",
        0
      ]
    ],
    "admin_log_seen": [
      [
        "email",
        "TEXT",
        0,
        null,
        1
      ],
      [
        "seen_ts",
        "REAL",
        1,
        null,
        0
      ]
    ],
    "allowlist": [
      [
        "added_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "added_by",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "email",
        "TEXT",
        0,
        null,
        1
      ],
      [
        "note",
        "TEXT",
        0,
        null,
        0
      ]
    ],
    "api_keys": [
      [
        "created_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "created_by",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "id",
        "INTEGER",
        0,
        null,
        1
      ],
      [
        "key_hash",
        "TEXT",
        1,
        null,
        0
      ],
      [
        "label",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "last4",
        "TEXT",
        1,
        null,
        0
      ],
      [
        "last_used_at",
        "REAL",
        0,
        null,
        0
      ],
      [
        "revoked_at",
        "REAL",
        0,
        null,
        0
      ],
      [
        "user_id",
        "INTEGER",
        1,
        null,
        0
      ]
    ],
    "auth_request_attempts": [
      [
        "created_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "email",
        "TEXT",
        1,
        null,
        0
      ],
      [
        "ip",
        "TEXT",
        1,
        null,
        0
      ]
    ],
    "chat_request_attempts": [
      [
        "created_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "user_id",
        "INTEGER",
        1,
        null,
        0
      ]
    ],
    "conversations": [
      [
        "created_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "id",
        "INTEGER",
        0,
        null,
        1
      ],
      [
        "title",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "updated_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "user_id",
        "INTEGER",
        1,
        null,
        0
      ]
    ],
    "import_jobs": [
      [
        "created_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "created_by",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "filename",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "id",
        "INTEGER",
        0,
        null,
        1
      ],
      [
        "log",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "progress",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "report",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "status",
        "TEXT",
        1,
        "'pending'",
        0
      ],
      [
        "updated_at",
        "REAL",
        1,
        null,
        0
      ]
    ],
    "lesson_rejections": [
      [
        "category",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "created_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "created_by",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "description",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "embedding",
        "BLOB",
        0,
        null,
        0
      ],
      [
        "headline",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "hits",
        "INTEGER",
        1,
        "0",
        0
      ],
      [
        "id",
        "INTEGER",
        0,
        null,
        1
      ],
      [
        "skill_id",
        "INTEGER",
        0,
        null,
        0
      ],
      [
        "was_verified",
        "INTEGER",
        1,
        "0",
        0
      ]
    ],
    "login_tokens": [
      [
        "email",
        "TEXT",
        1,
        null,
        0
      ],
      [
        "expires_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "token_hash",
        "TEXT",
        0,
        null,
        1
      ],
      [
        "used_at",
        "REAL",
        0,
        null,
        0
      ]
    ],
    "mcp_request_attempts": [
      [
        "created_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "key_id",
        "INTEGER",
        1,
        null,
        0
      ]
    ],
    "messages": [
      [
        "clarify",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "content",
        "TEXT",
        1,
        null,
        0
      ],
      [
        "conversation_id",
        "INTEGER",
        1,
        null,
        0
      ],
      [
        "created_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "duration_ms",
        "INTEGER",
        0,
        null,
        0
      ],
      [
        "figure",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "figure_grounding",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "id",
        "INTEGER",
        0,
        null,
        1
      ],
      [
        "model_used",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "results",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "results_truncated",
        "INTEGER",
        0,
        null,
        0
      ],
      [
        "role",
        "TEXT",
        1,
        null,
        0
      ],
      [
        "sql_log",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "suggestions",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "table_cells_checked",
        "INTEGER",
        0,
        null,
        0
      ],
      [
        "table_cells_matched",
        "INTEGER",
        0,
        null,
        0
      ],
      [
        "table_grounding",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "thinking",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "tokens",
        "INTEGER",
        0,
        null,
        0
      ]
    ],
    "meta": [
      [
        "key",
        "TEXT",
        0,
        null,
        1
      ],
      [
        "value",
        "TEXT",
        0,
        null,
        0
      ]
    ],
    "query_cache": [
      [
        "answer_md",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "created_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "data_version",
        "INTEGER",
        1,
        null,
        0
      ],
      [
        "embedding",
        "BLOB",
        0,
        null,
        0
      ],
      [
        "figure",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "final_sql",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "id",
        "INTEGER",
        0,
        null,
        1
      ],
      [
        "question",
        "TEXT",
        1,
        null,
        0
      ],
      [
        "results",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "results_truncated",
        "INTEGER",
        0,
        null,
        0
      ],
      [
        "suggestions",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "user_id",
        "INTEGER",
        0,
        null,
        0
      ]
    ],
    "sessions": [
      [
        "created_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "expires_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "token_hash",
        "TEXT",
        0,
        null,
        1
      ],
      [
        "user_id",
        "INTEGER",
        1,
        null,
        0
      ]
    ],
    "skills": [
      [
        "canonical_sql",
        "TEXT",
        1,
        null,
        0
      ],
      [
        "category",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "created_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "created_by",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "downvotes",
        "INTEGER",
        1,
        "0",
        0
      ],
      [
        "embedding",
        "BLOB",
        0,
        null,
        0
      ],
      [
        "headline",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "hits",
        "INTEGER",
        1,
        "0",
        0
      ],
      [
        "id",
        "INTEGER",
        0,
        null,
        1
      ],
      [
        "lesson",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "notes",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "question",
        "TEXT",
        1,
        null,
        0
      ],
      [
        "tags",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "upvotes",
        "INTEGER",
        1,
        "0",
        0
      ],
      [
        "verified",
        "INTEGER",
        1,
        "0",
        0
      ]
    ],
    "usage_log": [
      [
        "answer_leaked",
        "INTEGER",
        1,
        "0",
        0
      ],
      [
        "cached",
        "INTEGER",
        1,
        "0",
        0
      ],
      [
        "cached_prompt_tokens",
        "INTEGER",
        1,
        "0",
        0
      ],
      [
        "completion_tokens",
        "INTEGER",
        0,
        null,
        0
      ],
      [
        "cost",
        "REAL",
        1,
        "0",
        0
      ],
      [
        "cost_estimated",
        "INTEGER",
        1,
        "0",
        0
      ],
      [
        "created_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "emit_mode",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "escalated",
        "INTEGER",
        0,
        null,
        0
      ],
      [
        "exhaustion",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "figure_derivation",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "figure_grounding",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "first_call_cached_prompt_tokens",
        "INTEGER",
        1,
        "0",
        0
      ],
      [
        "first_call_prompt_tokens",
        "INTEGER",
        1,
        "0",
        0
      ],
      [
        "id",
        "INTEGER",
        0,
        null,
        1
      ],
      [
        "model_used",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "ok",
        "INTEGER",
        0,
        null,
        0
      ],
      [
        "prompt_tokens",
        "INTEGER",
        0,
        null,
        0
      ],
      [
        "question",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "source",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "table_cells_checked",
        "INTEGER",
        1,
        "0",
        0
      ],
      [
        "table_cells_matched",
        "INTEGER",
        1,
        "0",
        0
      ],
      [
        "table_grounding",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "user_id",
        "INTEGER",
        0,
        null,
        0
      ]
    ],
    "users": [
      [
        "created_at",
        "REAL",
        1,
        null,
        0
      ],
      [
        "email",
        "TEXT",
        1,
        null,
        0
      ],
      [
        "id",
        "INTEGER",
        0,
        null,
        1
      ],
      [
        "is_admin",
        "INTEGER",
        1,
        "0",
        0
      ],
      [
        "last_login",
        "REAL",
        0,
        null,
        0
      ]
    ],
    "year_provenance": [
      [
        "end_year",
        "INTEGER",
        1,
        null,
        0
      ],
      [
        "release",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "source",
        "TEXT",
        0,
        null,
        0
      ],
      [
        "start_year",
        "INTEGER",
        0,
        null,
        1
      ],
      [
        "updated_at",
        "REAL",
        1,
        null,
        0
      ]
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


def test_a_db_from_a_newer_build_is_refused():
    """THE REGRESSION: migrations are forward-only and the runner only ever
    tested `version > current`, so a user_version PAST our newest matched no
    branch, the loop did nothing, and the app then ran — and WROTE — against a
    schema it does not understand. Silently: no log, no exception, no signal.
    That happens on an ordinary rollback (pinning IPEDS_TAG back after an
    upgrade) or restoring a newer app.db backup into an older image, and app.db
    is the one irreplaceable store."""
    con = sqlite3.connect(":memory:")
    _apply_migrations(con, MIGRATIONS)
    newest = max(m[0] for m in MIGRATIONS)
    con.execute(f"PRAGMA user_version = {newest + 3}")
    try:
        _apply_migrations(con, MIGRATIONS)
    except SchemaTooNewError as e:
        msg = str(e)
        assert str(newest) in msg and str(newest + 3) in msg, \
            f"the refusal must name both versions so an operator can act: {msg}"
        return
    raise AssertionError(
        "a db newer than this build was accepted — the app would write against "
        "a schema it does not understand")


def test_an_equal_version_is_not_refused():
    """The control: being exactly up to date is the normal case, not a downgrade."""
    con = sqlite3.connect(":memory:")
    v = _apply_migrations(con, MIGRATIONS)
    assert _apply_migrations(con, MIGRATIONS) == v, "a no-op re-apply must stay a no-op"


def test_snapshots_are_capped():
    """One pre-migration snapshot is written per upgrade and nothing removed
    them, so a long-lived deployment accumulated a full copy of app.db per
    version, forever — the same unbounded-growth shape as the ipeds.db.prev copy
    the importer used to leave behind. Two is enough to step back across the
    upgrade you just did and the one before it; older ones are the operator's own
    volume backups to keep (scheduled backups are deliberately not the app's
    job — see the README)."""
    d = Path(tempfile.mkdtemp())
    db = d / "app.db"
    db.write_bytes(b"live")
    # Five snapshots, oldest first, with distinct mtimes so "newest" is well-defined.
    for i, v in enumerate([25, 26, 27, 28, 29]):
        snap = d / f"app.db.pre-v{v}"
        snap.write_bytes(b"x")
        os.utime(snap, (1000 + i, 1000 + i))

    _prune_snapshots(db)

    left = sorted(p.name for p in d.glob("app.db.pre-v*"))
    assert left == ["app.db.pre-v28", "app.db.pre-v29"], \
        f"expected the two NEWEST snapshots to survive, got {left}"
    assert db.exists(), "the live database must never be touched by the sweep"


def test_pruning_is_safe_with_fewer_snapshots_than_the_cap():
    d = Path(tempfile.mkdtemp())
    db = d / "app.db"
    db.write_bytes(b"live")
    (d / "app.db.pre-v29").write_bytes(b"x")
    _prune_snapshots(db)
    assert (d / "app.db.pre-v29").exists(), "a single snapshot must be kept"


def test_a_failed_migration_applies_nothing_and_leaves_the_version_alone():
    """THE REGRESSION: `executescript` runs statements sequentially with no
    transaction, and the user_version bump used to be a SEPARATE execute after
    it. Most shipped migrations are multi-statement, so a failure part-way
    (disk full, an OOM kill, a container stopped mid-`up -d`) left the earlier
    statements APPLIED with user_version un-bumped -- and every later boot
    re-ran the whole migration and died on "duplicate column name", forever,
    against the one irreplaceable database.

    Asserts the pair: the first statement must NOT survive, and user_version
    must not move. Without the BEGIN/COMMIT wrapper the column IS present and
    this fails."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("CREATE TABLE t (id INTEGER);")
    before = con.execute("PRAGMA user_version").fetchone()[0]
    bad = (1, "ALTER TABLE t ADD COLUMN a INTEGER;\n"
              "ALTER TABLE does_not_exist ADD COLUMN b INTEGER;")
    try:
        _apply_migrations(con, [bad])
        raise AssertionError("a failing migration must propagate, not pass silently")
    except sqlite3.OperationalError:
        pass
    cols = {r["name"] for r in con.execute("PRAGMA table_info(t)")}
    assert "a" not in cols, (
        "the first statement of a failed migration survived -- the migration is "
        f"not atomic; columns on t: {sorted(cols)}")
    assert con.execute("PRAGMA user_version").fetchone()[0] == before, \
        "user_version moved despite the migration failing"
    con.close()


def test_a_successful_migration_still_commits_and_leaves_no_open_transaction():
    """The atomicity wrapper must not break the happy path: the DDL lands, the
    version bumps, and the connection is left usable (an open transaction here
    would deadlock the very next writer)."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("CREATE TABLE t (id INTEGER);")
    _apply_migrations(con, [(1, "ALTER TABLE t ADD COLUMN a INTEGER;")])
    cols = {r["name"] for r in con.execute("PRAGMA table_info(t)")}
    assert "a" in cols, f"migration did not apply; columns: {sorted(cols)}"
    assert con.execute("PRAGMA user_version").fetchone()[0] == 1, "version not bumped"
    assert not con.in_transaction, "migration left an open transaction"
    con.close()


def _boot_db():
    """A migrated in-memory app.db, ready for _bootstrap_admins."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    _apply_migrations(con, MIGRATIONS)
    return con


def _is_admin(con, email):
    row = con.execute("SELECT is_admin FROM users WHERE email=?", (email,)).fetchone()
    return bool(row and row["is_admin"])


def _allowlisted(con, email):
    return con.execute(
        "SELECT 1 FROM allowlist WHERE email=?", (email,)).fetchone() is not None


def test_removing_a_bootstrap_admin_survives_a_restart():
    """THE REGRESSION: init_db runs on EVERY boot and re-ran the grant
    unconditionally (`ON CONFLICT(email) DO UPDATE SET is_admin=1`). So
    offboarding a departed admin -- demote, then remove -- held only until the
    next container restart (an image upgrade, a host reboot,
    `restart: unless-stopped`), which silently restored BOTH their allowlist row
    and their admin bit. They could then request a fresh magic link and walk
    back in with full access to every user's usage, the allowlist, and imports.

    Simulates: fresh boot grants, an admin offboards them, the next boot must
    leave them gone."""
    con = _boot_db()
    _bootstrap_admins(con, ["founder@example.edu"])
    assert _is_admin(con, "founder@example.edu"), "fresh install must bootstrap"

    # Offboard, exactly as the admin router does.
    con.execute("DELETE FROM allowlist WHERE email=?", ("founder@example.edu",))
    con.execute("DELETE FROM users WHERE email=?", ("founder@example.edu",))

    _bootstrap_admins(con, ["founder@example.edu"])  # the restart
    assert not _allowlisted(con, "founder@example.edu"), \
        "a restart re-allowlisted a removed admin"
    assert not _is_admin(con, "founder@example.edu"), \
        "a restart restored a removed admin's is_admin bit"
    con.close()


def test_a_demoted_bootstrap_admin_is_not_re_promoted_by_a_restart():
    """The narrower half: the account is still a legitimate USER (kept on the
    allowlist), only its admin bit was taken away. `DO UPDATE SET is_admin=1`
    put it straight back on the next boot."""
    con = _boot_db()
    _bootstrap_admins(con, ["founder@example.edu"])
    con.execute("UPDATE users SET is_admin=0 WHERE email=?", ("founder@example.edu",))

    _bootstrap_admins(con, ["founder@example.edu"])
    assert not _is_admin(con, "founder@example.edu"), \
        "a restart re-promoted a deliberately demoted admin"
    assert _allowlisted(con, "founder@example.edu"), \
        "demotion must not remove their access entirely"
    con.close()


def test_a_corrupt_bootstrap_marker_does_not_brick_startup():
    """The warning in _bootstrap_admins TELLS the admin to hand-edit this JSON
    ("remove just its entry from the JSON list in the '%s' row"), and init_db
    is deliberately un-caught in lifespan -- so an unguarded json.loads meant a
    dropped comma bricked every subsequent boot with a raw JSONDecodeError,
    remediable only by another hand-edit of the same row.

    Fails OPEN, matching skills.muted_categories and _applied_seed_keys. The
    direction is also the safe one: an empty `applied` against an ESTABLISHED
    allowlist takes the record-only branch, so the corrupt marker cannot cause
    a re-grant of an admin someone removed -- which this asserts, because
    failing open in the WRONG direction would be a worse bug than the crash."""
    con = _boot_db()
    # Established deployment, with an offboarded admin -- the state a re-grant
    # would damage.
    con.execute("INSERT INTO allowlist(email, added_by, added_at) VALUES (?,?,?)",
                ("colleague@example.edu", "admin", 0))
    con.execute("INSERT INTO users(email, is_admin, created_at) VALUES (?,0,?)",
                ("departed@example.edu", 0))
    # Every shape an operator can leave behind. Three of these PARSE fine, and
    # that is the point -- a successful json.loads is not a usable marker:
    #   ''      -> `raw or "[]"` made it a legitimate empty list
    #   '{}'    -> an empty set, so nothing reads as applied
    #   '"a@b"' -> a JSON string, whose set() is its CHARACTERS
    # each leaving `marked` true with `applied` missing the address, which
    # selects the grant branch. Only the trailing-comma shape raises, and a
    # guard that catches only the raise (the first version of this fix) let the
    # other three through.
    for raw in ('["departed@example.edu",]',      # raises
                '',                                # blank
                '   ',                             # whitespace
                '{}',                              # not a list
                '"departed@example.edu"',          # a string, not a list
                '5'):                              # not a list
        con.execute("UPDATE users SET is_admin=0 WHERE email=?",
                    ("departed@example.edu",))
        con.execute("DELETE FROM allowlist WHERE email=?", ("departed@example.edu",))
        set_meta(con, _BOOTSTRAP_APPLIED_KEY, raw)
        con.commit()

        _bootstrap_admins(con, ["departed@example.edu"])       # must not raise

        assert not _is_admin(con, "departed@example.edu"), \
            f"marker {raw!r} re-granted an admin who was removed"
        assert not _allowlisted(con, "departed@example.edu"), \
            f"marker {raw!r} restored the allowlist row of a removed admin"
        # `is not None` would pass on the corrupt string itself, which is
        # exactly what this needs to rule out: the marker must be REWRITTEN as
        # a usable JSON list, or the next boot re-enters the same branch.
        rewritten = get_meta(con, _BOOTSTRAP_APPLIED_KEY)
        assert isinstance(json.loads(rewritten), list), \
            f"marker {raw!r} was left unusable: {rewritten!r}"
    con.close()


def test_an_established_db_does_not_re_grant_on_the_upgrade_hop():
    """The migration point. On a database that predates the marker, granting
    'one last time' would restore precisely the admin someone had already
    removed -- reproducing the bug on the way to fixing it. An established
    deployment records the current list instead."""
    con = _boot_db()
    # Established: an allowlist exists, and the departed admin has been
    # OFFBOARDED -- which is a specific state, not merely "absent". _remove_user
    # deletes the allowlist row and KEEPS the users row with is_admin=0, and
    # that surviving row is what distinguishes a removal from an address this
    # deployment has never seen (which SHOULD be granted; see the hop test
    # above). The original version of this fixture inserted neither row, so it
    # was asserting the right thing about the wrong state.
    con.execute("INSERT INTO allowlist(email, added_by, added_at) VALUES (?,?,?)",
                ("colleague@example.edu", "admin", 0))
    con.execute("INSERT INTO users(email, is_admin, created_at) VALUES (?,0,?)",
                ("departed@example.edu", 0))
    con.commit()
    assert get_meta(con, _BOOTSTRAP_APPLIED_KEY) is None, "marker should be absent"

    _bootstrap_admins(con, ["departed@example.edu"])
    assert not _allowlisted(con, "departed@example.edu"), \
        "the upgrade hop re-granted a previously removed admin"
    assert get_meta(con, _BOOTSTRAP_APPLIED_KEY) is not None, \
        "the marker must be recorded so later boots are decided by it"
    con.close()


def test_an_address_added_to_admin_emails_later_is_still_granted():
    """Bootstrap-once must not become bootstrap-never: an operator who adds a
    NEW address to ADMIN_EMAILS on an established deployment still gets it,
    because it was never in the applied set."""
    con = _boot_db()
    _bootstrap_admins(con, ["founder@example.edu"])          # fresh install
    _bootstrap_admins(con, ["founder@example.edu", "new@example.edu"])
    assert _is_admin(con, "new@example.edu"), \
        "a newly listed ADMIN_EMAILS address was never granted"
    con.close()


def test_the_upgrade_hop_still_grants_an_address_this_deployment_has_never_seen():
    """THE REGRESSION (found by a second review pass): the record-only upgrade
    hop was applied to EVERY listed address, so an operator who edits .env and
    pulls a new image in one `docker compose up -d` -- the ordinary upgrade --
    got their newly added admin silently swallowed, AND recorded as applied so
    the next restart would not grant it either. README said the opposite.

    An offboarded admin and a never-seen address are distinguishable:
    `_remove_user` deletes the allowlist row but KEEPS the users row with
    is_admin=0. Asserts both halves in one established, pre-marker database."""
    con = _boot_db()
    # Established, no marker. `departed` was offboarded (users row survives,
    # allowlist row gone); `dean` has never been seen here at all.
    con.execute("INSERT INTO allowlist(email, added_by, added_at) VALUES (?,?,?)",
                ("colleague@example.edu", "admin", 0))
    con.execute("INSERT INTO users(email, is_admin, created_at) VALUES (?,0,?)",
                ("departed@example.edu", 0))
    con.commit()
    assert get_meta(con, _BOOTSTRAP_APPLIED_KEY) is None, "marker should be absent"

    _bootstrap_admins(con, ["departed@example.edu", "dean@example.edu"])

    assert not _is_admin(con, "departed@example.edu"), \
        "the upgrade hop restored a deliberately offboarded admin"
    assert _is_admin(con, "dean@example.edu"), \
        "the upgrade hop swallowed an address this deployment had never seen"
    # And it stays granted, rather than depending on a second restart.
    _bootstrap_admins(con, ["departed@example.edu", "dean@example.edu"])
    assert _is_admin(con, "dean@example.edu"), "the new admin did not persist"
    assert not _is_admin(con, "departed@example.edu"), "a later boot restored them"
    con.close()


def run():
    print("app.db migration contract:")
    check("the upgrade hop grants a NEVER-SEEN address but not an offboarded one",
          test_the_upgrade_hop_still_grants_an_address_this_deployment_has_never_seen)
    check("removing a bootstrap admin SURVIVES a restart",
          test_removing_a_bootstrap_admin_survives_a_restart)
    check("a demoted bootstrap admin is not re-promoted by a restart",
          test_a_demoted_bootstrap_admin_is_not_re_promoted_by_a_restart)
    check("a corrupt bootstrap marker does not brick startup",
          test_a_corrupt_bootstrap_marker_does_not_brick_startup)
    check("an established db does not re-grant on the upgrade hop",
          test_an_established_db_does_not_re_grant_on_the_upgrade_hop)
    check("an address added to ADMIN_EMAILS later is still granted",
          test_an_address_added_to_admin_emails_later_is_still_granted)
    check("a FAILED migration applies nothing and leaves user_version alone",
          test_a_failed_migration_applies_nothing_and_leaves_the_version_alone)
    check("a successful migration commits and leaves no open transaction",
          test_a_successful_migration_still_commits_and_leaves_no_open_transaction)
    check("a db from a NEWER build is refused, not silently accepted",
          test_a_db_from_a_newer_build_is_refused)
    check("an up-to-date db is not mistaken for a downgrade",
          test_an_equal_version_is_not_refused)
    check("pre-migration snapshots are capped at the newest two", test_snapshots_are_capped)
    check("pruning is safe below the cap", test_pruning_is_safe_with_fewer_snapshots_than_the_cap)
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
    check("migration 25 adds usage_log.table_grounding + cell counts",
          test_migration_25_adds_usage_log_table_grounding_columns)
    check("migration 26 adds messages.duration_ms (nullable)",
          test_migration_26_adds_messages_duration_ms)
    check("migration 27 adds usage_log.exhaustion (nullable)",
          test_migration_27_adds_usage_log_exhaustion_column)
    check("migration 28 adds chat_request_attempts (SEC-3 throttle)",
          test_migration_28_adds_chat_request_attempts_table)
    check("migration 32 drops the dead messages.feedback column (fresh == upgraded)",
          test_migration_32_drops_the_dead_messages_feedback_column)
    check("migration 33 adds messages table-grounding columns (nullable)",
          test_migration_33_adds_messages_table_grounding_columns)
    check("fresh db advances to the baseline version with all new objects",
          test_fresh_db_advances_to_baseline_version_with_all_new_objects)
    check("migration 6 rewrites terse seed lessons, leaves admin edits alone",
          test_migration_6_rewrites_terse_seed_lessons)
    check("migration 6 is idempotent and a no-op on a fresh (unseeded) db",
          test_migration_6_is_idempotent_and_noop_on_fresh_db)
    check("migration 7 adds skills.headline (nullable)",
          test_migration_7_adds_headline_column)
    check("migration 36 relabels historical retry-suppressed figures",
          test_migration_36_relabels_historical_suppressed_figures)
    check("migration 35 adds skills.category + the lesson_rejections tombstone table",
          test_migration_35_adds_category_and_lesson_rejections_table)
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
