"""App-state database (`app.db`) — everything that is NOT survey data.

Kept separate from ipeds.db so rebuilding/atomic-swapping the survey data never
touches users, skills, or chat history. Plain sqlite3 with WAL; the schema is
created idempotently on startup.
"""
from __future__ import annotations

import sqlite3
import time

from app.config import get_settings
from app.seeds import SEED_LESSON_REWRITES

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    last_login    REAL
);

-- Source of truth for who may request a magic link.
CREATE TABLE IF NOT EXISTS allowlist (
    email      TEXT PRIMARY KEY,
    note       TEXT,
    added_by   TEXT,
    added_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS access_requests (
    id         INTEGER PRIMARY KEY,
    email      TEXT NOT NULL,
    reason     TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|denied
    created_at REAL NOT NULL
);

-- Single-use magic-link tokens (only the hash is stored).
CREATE TABLE IF NOT EXISTS login_tokens (
    token_hash TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used_at    REAL
);

-- One row per magic-link/access request, used for sliding-window rate limiting.
CREATE TABLE IF NOT EXISTS auth_request_attempts (
    email      TEXT NOT NULL,
    ip         TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_attempts_created ON auth_request_attempts(created_at);

-- Long-lived sessions (only the hash is stored; the cookie holds the raw token).
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    title      TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_conv_user ON conversations(user_id, updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    role            TEXT NOT NULL,        -- user|assistant
    content         TEXT NOT NULL,
    sql_log         TEXT,                 -- JSON list of executed SQL
    model_used      TEXT,
    tokens          INTEGER,
    feedback        INTEGER,              -- +1 / -1 / NULL
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_msg_conv ON messages(conversation_id, id);

-- Validated NL->SQL exemplars ("skills") retrieved as few-shot context.
CREATE TABLE IF NOT EXISTS skills (
    id            INTEGER PRIMARY KEY,
    question      TEXT NOT NULL,
    canonical_sql TEXT NOT NULL,
    notes         TEXT,
    embedding     BLOB,                   -- float32 vector
    tags          TEXT,
    upvotes       INTEGER NOT NULL DEFAULT 0,
    downvotes     INTEGER NOT NULL DEFAULT 0,
    hits          INTEGER NOT NULL DEFAULT 0,
    verified      INTEGER NOT NULL DEFAULT 0,
    created_by    TEXT,
    created_at    REAL NOT NULL
);

-- Semantic cache of recent answers (reuse SQL when a near-identical Q recurs).
CREATE TABLE IF NOT EXISTS query_cache (
    id           INTEGER PRIMARY KEY,
    question     TEXT NOT NULL,
    embedding    BLOB,
    final_sql    TEXT,
    answer_md    TEXT,
    data_version INTEGER NOT NULL,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER,
    question     TEXT,
    model_used   TEXT,
    escalated    INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    ok           INTEGER,
    cached       INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_time ON usage_log(created_at);

CREATE TABLE IF NOT EXISTS import_jobs (
    id          INTEGER PRIMARY KEY,
    filename    TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|running|checks|passed|failed|swapped
    log         TEXT,
    report      TEXT,
    created_by  TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

-- Small key/value for app metadata (e.g. data_version bumped on each import).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _sql_quote(s: str) -> str:
    """Escape a Python string for embedding as a single-quoted SQL literal."""
    return "'" + s.replace("'", "''") + "'"


def _seed_rewrite_ddl() -> str:
    """Build migration 6's UPDATE statements from the shared seed-rewrite map, so
    the frozen migration text and the live seeds can never drift. Only rows still
    bearing the exact original terse lesson are touched (admin edits are safe)."""
    stmts = [
        f"UPDATE skills SET lesson={_sql_quote(new)}, notes={_sql_quote(new)} "
        f"WHERE created_by='seed' AND lesson={_sql_quote(old)};"
        for old, new in SEED_LESSON_REWRITES
    ]
    return "\n".join(stmts)


# Ordered schema migrations, keyed by an increasing integer version tracked in
# `PRAGMA user_version`. Migration 1 is the full baseline schema — every
# statement is CREATE ... IF NOT EXISTS, so it is a safe no-op on a database that
# predates this system (it simply advances an existing db to version 1). Add each
# future schema change as a new (version, ddl) tuple with the next integer; never
# edit or renumber a shipped migration.
MIGRATIONS: list[tuple[int, str]] = [
    (1, SCHEMA),
    # Per-request cost (USD), for the admin spend dashboard. Provider-reported
    # (usage.cost in the /chat/completions response) and OpenRouter-specific —
    # on another LLM_BASE_URL provider this column stays 0.
    (2, "ALTER TABLE usage_log ADD COLUMN cost REAL NOT NULL DEFAULT 0;"),
    # Skills become "lessons": a human-readable RULE (the transferable knowledge)
    # is now the primary payload, with the SQL kept only as an optional worked
    # example. Backfill from the seed `notes`, which already read as rules.
    (3, "ALTER TABLE skills ADD COLUMN lesson TEXT;\n"
        "UPDATE skills SET lesson=notes "
        "WHERE lesson IS NULL AND notes IS NOT NULL AND trim(notes) != '';"),
    # Per-year provenance (which release was integrated, and how) — lets the
    # Imports catalog offer a Provisional->Final "update" re-integration.
    (4, "CREATE TABLE IF NOT EXISTS year_provenance("
        "start_year INTEGER PRIMARY KEY, end_year INTEGER NOT NULL, "
        "release TEXT, source TEXT, updated_at REAL NOT NULL);"),
    # Structured per-year JSON progress for a running import job (polled by
    # the Imports tab's per-file progress rows).
    (5, "ALTER TABLE import_jobs ADD COLUMN progress TEXT;"),
    # Rewrite the original terse seed lessons ("Year-matched hd join; control=1
    # public; …") into full human-readable sentences so an admin reading the
    # lessons list understands them. Matches on created_by='seed' AND the exact
    # old text, so a seed an admin has edited is left untouched. Lesson text is
    # not embedded (embeddings key off the question), so no re-embed is needed.
    (6, _seed_rewrite_ddl()),
    # Generalized structured lessons: a short HEADLINE (the rule title) now
    # leads each lesson, alongside the existing `lesson` column (repurposed as
    # the longer generalized description). Nullable — backfilled by the
    # idempotent Python passes `upgrade_seed_lessons`/`reembed_skills_if_needed`
    # at startup (app/main.py lifespan), since a pure-SQL migration can't
    # recompute embeddings.
    (7, "ALTER TABLE skills ADD COLUMN headline TEXT;"),
    # is_denied() (app/auth.py) runs a per-address lookup on access_requests on
    # EVERY unauthenticated POST /api/auth/request. The table is unbounded in
    # principle (an attacker rotating in-domain addresses can grow it — see the
    # open access-request-DDOS item), so index the lookup rather than leave a
    # full scan on an unauth hot path.
    (8, "CREATE INDEX IF NOT EXISTS idx_access_requests_email "
        "ON access_requests(email);"),
    # Canonical form for DENYLIST matching (app.auth.canon_email/is_denied):
    # exact-string matching is fail-OPEN for a denylist — a "+tag" or case
    # variant of a denied address was previously left completely unblocked,
    # a real bypass (Gmail/Workspace/M365 all deliver user+tag@domain to the
    # same mailbox as user@domain). Lowercase + `+tag` local-part suffix
    # stripped, deliberately NOT dot-stripped (dots can be a different real
    # person on many mail systems — see app.auth.canon_email's docstring).
    # Backfills every pre-existing row so an address denied before this
    # migration ran is still found by the canonical lookup; new rows are
    # populated at insert time by app.auth.request_login.
    (9, "ALTER TABLE access_requests ADD COLUMN canon_email TEXT;\n"
        "UPDATE access_requests SET canon_email = LOWER(\n"
        "    CASE WHEN INSTR(email, '+') > 0 AND INSTR(email, '+') < INSTR(email, '@')\n"
        "      THEN SUBSTR(email, 1, INSTR(email, '+') - 1) || SUBSTR(email, INSTR(email, '@'))\n"
        "      ELSE email\n"
        "    END\n"
        ") WHERE canon_email IS NULL;\n"
        "CREATE INDEX IF NOT EXISTS idx_access_requests_canon_email "
        "ON access_requests(canon_email);"),
    # Round 3 (.plan-undeny.md, fold-in fix 2): is_denied() (app/auth.py)
    # wraps the column in COALESCE(canon_email, LOWER(email)), and migration
    # 9's idx_access_requests_canon_email is a PLAIN column index -- SQLite
    # cannot match a plain index to an expression, so the lookup that
    # migration 9 was written to protect still full-table-SCANs on every
    # unauthenticated POST /api/auth/request (verified with EXPLAIN QUERY
    # PLAN). An index on the EXPRESSION is what that predicate can actually
    # use; COALESCE and LOWER are deterministic, so it's a legal index
    # expression. Keep this expression textually identical to
    # app.auth.is_denied's / admin.py's deny/undo/add_allowlist predicates,
    # or the planner silently falls back to a scan again. Do NOT drop or
    # renumber migrations 8 or 9 -- never edit a shipped migration; 9's plain
    # index still serves nothing harmful, and removing it is a separate call.
    (10, "CREATE INDEX IF NOT EXISTS idx_access_requests_canon_expr "
         "ON access_requests(COALESCE(canon_email, LOWER(email)));"),
    # The admin Blocked-users table shows WHEN a request was rejected, kept
    # separate from `created_at` (when it was REQUESTED) — the two are distinct
    # facts and neither should overwrite the other. Deny (admin.py) stamps this;
    # pre-existing denied rows keep NULL (rendered "—"), which is honest: the app
    # genuinely never recorded their denial time.
    (11, "ALTER TABLE access_requests ADD COLUMN denied_at REAL;"),
    # The assistant's progress trace (status/reasoning/SQL/tool events, as a JSON
    # list of {kind,text} items) — persisted alongside sql_log so the "Thinking"
    # disclosure survives a reload/reopen, not just the live in-session turn.
    (12, "ALTER TABLE messages ADD COLUMN thinking TEXT;"),
    # The answer's signature "figure" — a structured hero statistic
    # ({value,unit?,label,source?} JSON) parsed server-side from the model's
    # ```figure fence, persisted so it survives a reload like sql_log/thinking.
    (13, "ALTER TABLE messages ADD COLUMN figure TEXT;"),
    # Cache the figure too, so a repeated (cache-hit) question shows the SAME hero
    # statistic the fresh answer did — no jarring "figure the first time, none the
    # second". JSON, like the messages.figure column above.
    (14, "ALTER TABLE query_cache ADD COLUMN figure TEXT;"),
    # Drill-down follow-up questions (a JSON array of strings), persisted like the
    # figure so the "you might also ask" chips survive a reload AND a cache-hit
    # repeat — on the message and in the answer cache.
    (15, "ALTER TABLE messages ADD COLUMN suggestions TEXT;"),
    (16, "ALTER TABLE query_cache ADD COLUMN suggestions TEXT;"),
    # Per-admin "logs seen" marker for the Admin → Logs attention badge. The badge
    # counts log problems (WARNING/ERROR/CRITICAL) newer than an admin's seen_ts, so
    # it clears when they open the Logs tab and re-appears only for later problems.
    # Keyed by email so one admin acknowledging the logs doesn't clear the badge for
    # another; no row ⇒ seen_ts treated as 0 ("never looked"). Lives in app.db even
    # though the logs themselves are in the separate logs.db — this is app state, not
    # a log record.
    (17, "CREATE TABLE IF NOT EXISTS admin_log_seen("
         "email TEXT PRIMARY KEY, seen_ts REAL NOT NULL);"),
    # Prompt tokens the LLM provider served from ITS OWN prefix cache (the big
    # static SCHEMA.md prefix), per request — lets the Usage dashboard show a
    # prompt-cache-hit rate. Provider-reported (OpenRouter's
    # prompt_tokens_details.cached_tokens / DeepSeek's prompt_cache_hit_tokens);
    # stays 0 on a provider that reports neither. Distinct from `cached`, which
    # flags our own semantic answer-cache short-circuits.
    (18, "ALTER TABLE usage_log ADD COLUMN cached_prompt_tokens INTEGER NOT NULL DEFAULT 0;"),
    # The FIRST LLM call of a turn, split out from the blended totals above so the
    # dashboard can show a SCHEMA-PREFIX cache rate (cross-question reuse of the big
    # static prefix) distinct from the blended prompt-cache rate (which later tool
    # rounds inflate). Provider-reported, same source as cached_prompt_tokens; 0 when
    # unreported. See app/llm.py AgentResult.first_call_* for why the first call is
    # the clean signal.
    (19, "ALTER TABLE usage_log ADD COLUMN first_call_prompt_tokens "
         "INTEGER NOT NULL DEFAULT 0;\n"
         "ALTER TABLE usage_log ADD COLUMN first_call_cached_prompt_tokens "
         "INTEGER NOT NULL DEFAULT 0;"),
    # The disambiguation "clarify" turn's structured {question, options[]} payload
    # (parsed server-side from the model's ```clarify fence), persisted on the
    # assistant message like figure/suggestions — so a reload shows the same
    # clarifying question + chips, not just the live in-session turn. Deliberately
    # NO query_cache.clarify column: a clarify turn is never written to the answer
    # cache (see app/routers/chat.py).
    (20, "ALTER TABLE messages ADD COLUMN clarify TEXT;"),
    # Whether the turn's hero figure could be reproduced from the query results
    # the turn actually ran (app/grounding.py): 'exact' | 'rounded' | 'derived' |
    # 'ungrounded' | 'no_figure' | 'unchecked'. NULL = a turn that predates this
    # column, or one that never ran the check (an answer-cache hit runs no query,
    # so there is nothing to ground against). OBSERVE-ONLY — it feeds the
    # Admin -> Usage rate and gates nothing.
    (21, "ALTER TABLE usage_log ADD COLUMN figure_grounding TEXT;"),
    # HOW the figure's number was reproduced, e.g. "pct_change(q1.awards)" —
    # the op, which retained result, which column. The status alone can't
    # distinguish a real derivation from a lucky collision across the searched
    # ops, which is the whole question the observe-only period exists to answer;
    # without this an 'exact' and a coincidental 'derived' look identical in the
    # data. NULL whenever nothing matched (an 'ungrounded' turn) or the turn was
    # never checked.
    (22, "ALTER TABLE usage_log ADD COLUMN figure_derivation TEXT;"),
    # Each turn's run_sql results (JSON list of {columns, rows}, capped), so a
    # LATER turn can ground a figure against an EARLIER turn's data — the fix for
    # figures recited from conversation context that turn-scoped grounding could
    # only mark 'unchecked'. Backend-only (never surfaced to the client), NULL on
    # a turn that ran no query (cache hit / refusal / clarify) or predates it.
    (23, "ALTER TABLE messages ADD COLUMN results TEXT;"),
    # Structured-emission telemetry (PR-1): `emit_mode` = 'structured'/'forced'
    # (finished via the emit_answer tool, voluntarily or via a forced re-emit) |
    # 'fence' (free-typed, or the feature is off); `answer_leaked` = 1 when the
    # scrubber CAUGHT AND REMOVED residual fence/JSON debris from the prose before
    # it shipped (a scrub rate, not a ship rate). Together they show structured
    # emission holds the leak rate near 0. NULL/0 on turns that predate.
    (24, "ALTER TABLE usage_log ADD COLUMN emit_mode TEXT;\n"
         "ALTER TABLE usage_log ADD COLUMN answer_leaked INTEGER NOT NULL DEFAULT 0;"),
    # Table grounding (app/grounding.py, observe-only): `table_grounding` = the
    # per-turn status ('matched'/'partial'/'unmatched'/'no_table'/'unchecked');
    # `table_cells_checked`/`table_cells_matched` = the numeric-cell counts that
    # drive Admin -> Usage's cell-level rate. no_table/unchecked carry 0 counts so
    # they self-exclude from the SUM ratio. NULL/0 on turns that predate.
    (25, "ALTER TABLE usage_log ADD COLUMN table_grounding TEXT;\n"
         "ALTER TABLE usage_log ADD COLUMN table_cells_checked INTEGER NOT NULL DEFAULT 0;\n"
         "ALTER TABLE usage_log ADD COLUMN table_cells_matched INTEGER NOT NULL DEFAULT 0;"),
    # Turn duration (ms) on the ASSISTANT message — the "Thought for N seconds"
    # display. Can't be derived from timestamps: _persist stamps the user + the
    # assistant row with one `now`. Nullable; NULL on cache-hit/refusal/predating
    # rows (the UI shows the line only for a real answer).
    (26, "ALTER TABLE messages ADD COLUMN duration_ms INTEGER;"),
    # Tool-budget exhaustion (app/llm.py, S5 path): the per-turn status of the
    # tool-budget-exhausted synthesis path. NULL = the turn did NOT exhaust its
    # step budget; 'answered' = it exhausted and shipped a synthesized answer;
    # 'degraded' = it exhausted AND its numbers were wholly ungrounded, so the
    # grounding gate replaced them with an honest "couldn't finish" message. Drives
    # Admin -> Usage's "Exhausted" count (with a degraded breakdown). NULL on
    # cache-hit/refusal/predating rows.
    (27, "ALTER TABLE usage_log ADD COLUMN exhaustion TEXT;"),
]


def connect() -> sqlite3.Connection:
    s = get_settings()
    con = sqlite3.connect(str(s.app_db_path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _apply_migrations(con: sqlite3.Connection,
                      migrations: list[tuple[int, str]] = MIGRATIONS) -> int:
    """Apply every migration whose version exceeds the db's current
    `user_version`, in order, bumping `user_version` after each. Returns the
    resulting version. Idempotent: already-applied migrations are skipped."""
    current = con.execute("PRAGMA user_version").fetchone()[0]
    for version, ddl in sorted(migrations):
        if version > current:
            con.executescript(ddl)
            # user_version can't be parameterized; version is our own trusted int.
            con.execute(f"PRAGMA user_version = {int(version)}")
            con.commit()
            current = version
    return current


def init_db() -> None:
    """Run pending migrations (idempotent) and bootstrap admins + data_version."""
    s = get_settings()
    s.app_db_path.parent.mkdir(parents=True, exist_ok=True)
    con = connect()
    try:
        _apply_migrations(con)
        # data_version starts at 1 (bumped by each successful import swap)
        con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('data_version', '1')")
        # Bootstrap admin accounts + allowlist from ADMIN_EMAILS.
        now = time.time()
        for email in s.admin_email_list:
            con.execute(
                "INSERT INTO allowlist(email, note, added_by, added_at) "
                "VALUES (?, 'bootstrap admin', 'system', ?) "
                "ON CONFLICT(email) DO NOTHING", (email, now))
            con.execute(
                "INSERT INTO users(email, is_admin, created_at) VALUES (?, 1, ?) "
                "ON CONFLICT(email) DO UPDATE SET is_admin=1", (email, now))
        con.commit()
    finally:
        con.close()


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO meta(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def data_version(con: sqlite3.Connection) -> int:
    return int(get_meta(con, "data_version", "1"))
