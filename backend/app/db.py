"""App-state database (`app.db`) — everything that is NOT survey data.

Kept separate from ipeds.db so rebuilding/atomic-swapping the survey data never
touches users, skills, or chat history. Plain sqlite3 with WAL; the schema is
created idempotently on startup.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

from app.config import get_settings
from app.seeds import SEED_LESSON_REWRITES

log = logging.getLogger("ipeds.db")

# How many pre-migration app.db snapshots to keep (backend/app/db.py
# _snapshot_before_migrating). Two lets you step back across the upgrade you
# just did and the one before it; older ones are the operator's volume
# backups to keep, not the app's.
SNAPSHOTS_KEPT = 2

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
    feedback        INTEGER,              -- DEAD: dropped by migration 32.
                                          -- Stays here because SCHEMA is frozen
                                          -- (it runs as migration 1); removing it
                                          -- would diverge fresh from upgraded dbs.
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
    # prompt_tokens_details.cached_tokens, or a native prompt_cache_hit_tokens);
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
    # Per-user chat throttle (app/ratelimit.py enforce_chat_rate_limit): one row
    # per POST /api/chat/stream turn, for sliding-window rate limiting so an
    # allowlisted user's runaway loop can't burn unbounded provider spend. Mirrors
    # the auth_request_attempts table above; the index keeps the window sweep cheap.
    (28, "CREATE TABLE IF NOT EXISTS chat_request_attempts (\n"
         "    user_id    INTEGER NOT NULL,\n"
         "    created_at REAL NOT NULL\n"
         ");\n"
         "CREATE INDEX IF NOT EXISTS idx_chat_attempts_created "
         "ON chat_request_attempts(created_at);"),
    # 29: scope the answer cache to its author, and give it an index.
    # cache_lookup had NO user predicate, so user B asking within 0.93 cosine of
    # user A's question was served A's stored answer text verbatim — the same
    # attributable leak /api/admin/usage goes out of its way to prevent by never
    # returning question text. Existing rows get user_id NULL and are therefore
    # unreachable by the new lookup (fail closed, not shared-by-default); the
    # retention sweep clears them out. The index also gives the per-lookup scan
    # something to stand on — the table previously had no index at all.
    (29, "ALTER TABLE query_cache ADD COLUMN user_id INTEGER;\n"
         "CREATE INDEX IF NOT EXISTS idx_query_cache_lookup "
         "ON query_cache(data_version, user_id);"),
    # 30: did this turn's SQL return more rows than the model was shown?
    # run_sql cuts at sql_row_cap_model (200) and QueryResult.truncated records
    # it, but get_conversation never selected results at all — so the browser
    # had NO structured signal, and a 200-row page of an 834-row result was
    # byte-identical on screen to a complete 200-row result. The only
    # disclosure was whatever prose the model remembered to write. (to_storage()
    # now carries the flag too — see its docstring — because a LATER turn's
    # grounding check needs it, not because this column needed it.) Booleans
    # stored as 0/1; NULL on pre-migration rows.
    (30, "ALTER TABLE messages ADD COLUMN results_truncated INTEGER;"),
    # 31: two halves of the same gap — the grounding chain, and what it can say.
    #
    # query_cache.results: a cache hit persisted results=NULL, so every LATER
    # turn in that conversation had nothing to ground against (_load_prior_results
    # selects only non-NULL rows) and silently graded `unchecked`. The cached rows
    # ARE the evidence for that answer — the prose is byte-identical to the turn
    # that produced them — so storing and replaying them is correctness, not a
    # shortcut. results_truncated rides along for the same reason.
    #
    # messages.figure_grounding: the server already grades every hero figure
    # exact/rounded/derived/ungrounded, but only usage_log ever saw it, so the
    # person reading the number learned nothing. Persisting the STATUS (never the
    # derivation string — that stays backend telemetry) lets a reproduced figure
    # carry a quiet "verified" mark that survives a reload. NULL on pre-migration
    # rows reads as "not known", which correctly renders no mark.
    (31, "ALTER TABLE query_cache ADD COLUMN results TEXT;\n"
         "ALTER TABLE query_cache ADD COLUMN results_truncated INTEGER;\n"
         "ALTER TABLE messages ADD COLUMN figure_grounding TEXT;"),
    # 32: drop messages.feedback — the 👍/👎 column, dead since the thumbs
    # feature was removed (nothing reads or writes it; the lesson pipeline now
    # mines the critic and the feedback distiller instead, which is a different
    # mechanism that never touched this column).
    #
    # This is the FIRST destructive migration here, so two notes for the next one:
    #  - It is deliberately a MIGRATION rather than an edit to the frozen baseline
    #    SCHEMA. SCHEMA runs as migration 1 on a fresh install, so editing it
    #    would leave a fresh install without the column and an upgraded install
    #    with it — a silent divergence, since both answer every query fine.
    #    Adding the DROP here converges both.
    #  - It is one-way. The values are from a removed feature and nothing can use
    #    them again, but the pre-migration snapshot (_snapshot_before_migrating)
    #    is the escape hatch if that judgement is ever wrong. That file is
    #    `app.db.pre-v31`, NOT pre-v32 — the snapshot is named for the version it
    #    HOLDS (the one before the upgrade), not the one being applied.
    #    DROP COLUMN rewrites the table; it needs sqlite >= 3.35 (2021), well
    #    under the floor every supported build already ships.
    (32, "ALTER TABLE messages DROP COLUMN feedback;"),
    # 33: table grounding, per message, so the READER sees it.
    #
    # grounding.check_table already graded every numeric MEASURE cell of an
    # answer's tables against the rows the turn actually queried — on every turn,
    # for free — but the verdict landed only on usage_log, driving the Admin →
    # Usage "Grounded cells" stat and nothing else. The person about to copy those
    # numbers into a report learned nothing. Exactly the gap migration 31 closed
    # for the hero figure, and the shape is deliberately identical: STATUS +
    # counts only. There is no per-cell detail to store (check_table flattens
    # every cell into one list and returns counts), and derivation-style
    # provenance stays backend telemetry.
    #
    # Nullable: a cache hit and a refusal grade nothing, and NULL is what renders
    # no mark at all — the same "not known" reading figure_grounding relies on.
    (33, "ALTER TABLE messages ADD COLUMN table_grounding TEXT;\n"
         "ALTER TABLE messages ADD COLUMN table_cells_checked INTEGER;\n"
         "ALTER TABLE messages ADD COLUMN table_cells_matched INTEGER;"),
    # 34 — was this turn's spend BILLED by the provider, or ESTIMATED by us?
    #
    # usage_log.cost is one number with two very different provenances: the
    # provider's own per-request charge (OpenRouter reports usage.cost) or our
    # list-price estimate for a provider that reports nothing (DeepSeek direct,
    # most self-hosted gateways). Admin → Usage presented both as a plain dollar
    # figure, so an estimate — which can be off by multiples, since list prices
    # drift from what a vendor actually bills — read exactly like a real invoice.
    #
    # It cannot be derived after the fact: cost>0 with prices configured is
    # ambiguous, and a deployment that SWITCHES providers (as this one just did)
    # has both kinds of row inside a single window, so no config-derived flag can
    # describe them. Hence a per-row stamp, written from llm.cost_is_estimated.
    #
    # DEFAULT 0 = "reported", which is correct for every existing row: they
    # predate the switch and carry OpenRouter's billed figure.
    (34, "ALTER TABLE usage_log ADD COLUMN cost_estimated INTEGER NOT NULL DEFAULT 0;"),
    # 35 (A2: lesson-rejection memory) -- rejecting a lesson is a hard DELETE
    # with no trace, so app.skills._find_duplicate can never suppress the same
    # proposal recurring: the evidence a rejection should have left behind is
    # destroyed by the rejection itself.
    #
    # skills.category: a nullable classification (the critic's closed
    # app.lessoncats token) so a lesson can be grouped and, from the admin UI,
    # muted as a whole category. Pre-existing/seed/feedback rows stay NULL --
    # only the critic path populates it going forward.
    #
    # lesson_rejections: one tombstone row per DELETEd lesson (written by
    # app.routers.admin.delete_skill BEFORE the DELETE), carrying enough to
    # recognize + attribute a future near-duplicate: the rejected headline +
    # description, the embedding (reused from the deleted skill row, not
    # recomputed), its category, its source, and whether it had been verified.
    # `hits` counts how many times a later candidate matched this tombstone
    # (mirrors skills.hits); `skill_id` is kept for provenance/display only.
    #
    # Deliberately NO foreign key on skill_id: the referenced skill row is gone
    # BY DEFINITION (that is the whole point of a tombstone), and db.connect()
    # sets PRAGMA foreign_keys=ON -- a real FK here would make every DELETE
    # .../skills/{id} that writes a tombstone fail outright.
    (35, "ALTER TABLE skills ADD COLUMN category TEXT;\n"
         "CREATE TABLE lesson_rejections (\n"
         "    id INTEGER PRIMARY KEY,\n"
         "    headline TEXT,\n"
         "    description TEXT,\n"
         "    embedding BLOB,\n"
         "    category TEXT,\n"
         "    created_by TEXT,\n"
         "    skill_id INTEGER,\n"
         "    was_verified INTEGER NOT NULL DEFAULT 0,\n"
         "    hits INTEGER NOT NULL DEFAULT 0,\n"
         "    created_at REAL NOT NULL\n"
         ");\n"
         "CREATE INDEX ix_lesson_rejections_created "
         "ON lesson_rejections(created_at DESC);"),
    # A figure the retry forced, found ungrounded and WITHHELD used to be
    # recorded as a plain `ungrounded` figure, which put it in the
    # Grounded-figures denominator as a miss even though the turn shipped no
    # figure at all. `grounding.SUPPRESSED` fixed that going forward — but only
    # going forward, and the historical rows are exactly identifiable, because
    # `figure_derivation` has recorded `retry:suppressed` all along. Without
    # this, an admin looking at any window covering pre-upgrade history still
    # reads the wrong rate while `figures_suppressed` reads 0 for that same
    # period, i.e. the new "· N suppressed" tail is absent on precisely the data
    # that motivated it. Measured on a real app.db: 10 rows, which is the whole
    # of the evidence the fix was argued from.
    (36, "UPDATE usage_log SET figure_grounding='retry_suppressed' "
         "WHERE figure_grounding='ungrounded' "
         "AND figure_derivation='retry:suppressed';"),
    # API keys for the MCP endpoint (v0.5.0). Three tables' worth of change,
    # kept in one migration because none of it is useful without the rest.
    #
    # `api_keys` deliberately mirrors `sessions`: only the SHA-256 hash is
    # stored, so a dump of app.db cannot be replayed as a credential, and the
    # raw key exists exactly once — in the HTTP response that minted it. The
    # input is 32 bytes from secrets.token_urlsafe, not a guessable password,
    # so sha256 is the right primitive and a slow KDF would only add a cost to
    # every MCP request while buying nothing against an attacker who cannot
    # enumerate the space anyway.
    #
    # `last4` is for the UI. A user with three keys has to be able to tell
    # which one they are revoking, and the label alone is not enough once
    # someone names two keys "laptop". Four characters of a 43-character
    # secret identifies without meaningfully narrowing a guess.
    #
    # Revocation sets `revoked_at` instead of deleting the row. A key that was
    # used against the data and then withdrawn is exactly the thing an
    # administrator needs to still be able to see afterwards.
    #
    # `mcp_request_attempts` mirrors `chat_request_attempts` (migration 28) so
    # the per-key rate limiter can reuse the shape ratelimit.py already uses
    # twice. No foreign key on key_id on purpose: the limiter writes on the
    # request path and must not be able to fail a request because a key row
    # was revoked and swept between the auth check and the insert.
    #
    # usage_log.source separates MCP spend from chat spend on Admin -> Usage.
    # Nullable with no default so every historical row keeps reading as the
    # chat traffic it actually was, rather than being retroactively relabelled.
    (37, "CREATE TABLE api_keys (\n"
         "    id           INTEGER PRIMARY KEY,\n"
         "    user_id      INTEGER NOT NULL REFERENCES users(id),\n"
         "    key_hash     TEXT NOT NULL UNIQUE,\n"
         "    last4        TEXT NOT NULL,\n"
         "    label        TEXT,\n"
         "    created_at   REAL NOT NULL,\n"
         "    created_by   TEXT,\n"
         "    last_used_at REAL,\n"
         "    revoked_at   REAL\n"
         ");\n"
         "CREATE INDEX ix_api_keys_user ON api_keys(user_id);\n"
         "CREATE TABLE mcp_request_attempts (\n"
         "    key_id     INTEGER NOT NULL,\n"
         "    created_at REAL NOT NULL\n"
         ");\n"
         "CREATE INDEX ix_mcp_attempts_created "
         "ON mcp_request_attempts(created_at);\n"
         "ALTER TABLE usage_log ADD COLUMN source TEXT;"),
]


def connect() -> sqlite3.Connection:
    s = get_settings()
    con = sqlite3.connect(str(s.app_db_path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


class SchemaTooNewError(RuntimeError):
    """`app.db` was written by a newer build than this one. Refusing to run."""


def _apply_migrations(con: sqlite3.Connection,
                      migrations: list[tuple[int, str]] = MIGRATIONS) -> int:
    """Apply every migration whose version exceeds the db's current
    `user_version`, in order, bumping `user_version` after each. Returns the
    resulting version. Idempotent: already-applied migrations are skipped.

    Raises SchemaTooNewError when the db is AHEAD of this build. Migrations are
    forward-only, so a `user_version` past our newest matched no branch and the
    loop simply did nothing — the app then ran, and WROTE, against a schema it
    does not understand: silently, with no log line and no exception. That is a
    routine situation (pinning IPEDS_TAG back after an upgrade, or restoring a
    newer app.db backup into an older image), and app.db is the irreplaceable
    store, so it has to be loud and fatal rather than best-effort."""
    current = con.execute("PRAGMA user_version").fetchone()[0]
    newest = max((v for v, _ in migrations), default=0)
    if current > newest:
        msg = (f"app.db is at schema version {current}, but this build only knows "
               f"up to {newest}. It was written by a NEWER version of the app. "
               f"Refusing to start rather than write against a schema this build "
               f"does not understand. Upgrade the image (or restore an app.db "
               f"backup taken before the upgrade — see app.db.pre-v*).")
        log.critical(msg)
        raise SchemaTooNewError(msg)
    for version, ddl in sorted(migrations):
        if version > current:
            # ONE atomic script: the DDL *and* the version bump, or neither.
            #
            # `executescript` runs its statements sequentially with no
            # transaction of its own, and the bump used to be a separate
            # `execute` after it. Most shipped migrations are multi-statement,
            # so a failure part-way (disk full, an OOM kill, a container stopped
            # mid-`up -d`) left the earlier statements APPLIED with
            # `user_version` un-bumped -- and every later boot then re-ran the
            # whole migration and died on "duplicate column name", permanently,
            # against the one irreplaceable database. `_snapshot_before_migrating`
            # was added to make that recoverable; this makes it not happen.
            #
            # The BEGIN must be INSIDE the script: `executescript` issues an
            # implicit COMMIT before it runs, so a `con.execute("BEGIN")`
            # beforehand is discarded. `PRAGMA user_version` is itself
            # transactional, so it reverts with the DDL.
            #
            # Verified both ways: without the wrapper a two-statement migration
            # whose second statement fails leaves the first applied; with it,
            # nothing is applied and `user_version` is unchanged.
            try:
                # user_version can't be parameterized; version is our own trusted int.
                con.executescript(
                    f"BEGIN;\n{ddl}\nPRAGMA user_version = {int(version)};\nCOMMIT;")
            except Exception:
                # The failing statement aborts the script, leaving the
                # transaction open; roll it back so the connection is usable
                # and nothing half-applied survives.
                con.rollback()
                raise
            current = version
    return current


def _snapshot_before_migrating(con: sqlite3.Connection, db_path) -> None:
    """Copy app.db aside before the first migration of an upgrade.

    `docker compose pull && up -d` runs N migrations against the one
    irreplaceable database with nothing taken first, and several shipped
    migrations are multi-statement `executescript` blocks that are not atomic —
    a failure part-way leaves columns added but `user_version` un-bumped, and
    every subsequent boot then dies on "duplicate column name" with no way back.
    This makes an upgrade reversible.

    Uses sqlite3's online backup API (consistent under WAL, no quiesce) — the
    same mechanism as scripts/backup_app_db.py, inlined rather than imported so
    `app/` doesn't reach into `scripts/`. Never fatal: a failed snapshot logs
    and lets the migration proceed, because refusing to boot over a missing
    backup would be a worse failure than the one it guards against."""
    current = con.execute("PRAGMA user_version").fetchone()[0]
    newest = max((v for v, _ in MIGRATIONS), default=0)
    if current >= newest:
        return  # nothing pending — no upgrade in progress, nothing to protect
    dest = db_path.with_name(f"{db_path.name}.pre-v{current}")
    if dest.exists():
        return  # already snapshotted at this version (a retried/crashed boot)
    try:
        dst = sqlite3.connect(dest)
        try:
            con.backup(dst)
        finally:
            dst.close()
        log.info("app.db snapshot before migrating v%d -> v%d: %s", current, newest, dest)
        _prune_snapshots(db_path)
    except Exception as e:  # noqa: BLE001 — a snapshot must never block an upgrade
        log.warning("pre-migration snapshot skipped (%s)", e)


def _prune_snapshots(db_path, keep: int = SNAPSHOTS_KEPT) -> None:
    """Keep only the newest `keep` pre-migration snapshots.

    One is written per upgrade and nothing removed them, so a long-lived
    deployment accumulated a full copy of app.db per version forever. Two is
    enough to step back across the upgrade you just did and the one before it;
    anything older is a job for the operator's own volume backups (see the
    README's Self-hosting section — scheduled backups are deliberately NOT the
    app's responsibility). Newest-first by mtime, matching
    scripts/backup_app_db.py's _prune."""
    snaps = sorted(db_path.parent.glob(f"{db_path.name}.pre-v*"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for old in snaps[max(keep, 1):]:
        try:
            old.unlink()
            log.info("pruned old app.db snapshot %s", old.name)
        except OSError as e:
            log.warning("could not prune %s (%s)", old.name, e)


def init_db() -> None:
    """Run pending migrations (idempotent) and bootstrap admins + data_version."""
    s = get_settings()
    s.app_db_path.parent.mkdir(parents=True, exist_ok=True)
    con = connect()
    try:
        _snapshot_before_migrating(con, s.app_db_path)
        _apply_migrations(con)
        # data_version starts at 1 (bumped by each successful import swap)
        con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('data_version', '1')")
        _bootstrap_admins(con, s.admin_email_list)
        con.commit()
    finally:
        con.close()


_BOOTSTRAP_APPLIED_KEY = "bootstrap_admins_applied"


def _bootstrap_admins(con: sqlite3.Connection, emails: list[str]) -> None:
    """Grant each ADMIN_EMAILS address allowlist + admin ONCE, not on every boot.

    `init_db` runs on every startup, and this used to re-run the grant
    unconditionally -- `ON CONFLICT(email) DO UPDATE SET is_admin=1`. So
    offboarding a departed admin (demote, then remove) held only until the next
    container restart: an image upgrade, a host reboot, or `restart:
    unless-stopped` silently restored their allowlist row AND their admin bit,
    and they could request a fresh magic link and walk back in. The README has
    always described this as applying on FIRST boot; the code did not, and the
    docs agreeing with the intent is why review never caught it.

    Each address is now recorded in `meta` once applied (the same marker pattern
    `skills.seed_from_schema_examples` uses for seeds), so a REMOVAL is a
    decision later boots respect, while a genuinely new address added to
    ADMIN_EMAILS still gets picked up.

    The migration point matters: on an established database the marker is
    absent, and re-granting "one last time" would reproduce the bug for exactly
    the admin someone had already removed. So a database that already has an
    allowlist is treated as having applied the current list -- recorded, not
    re-granted. Only a genuinely empty allowlist (a fresh install) bootstraps.
    """
    raw = get_meta(con, _BOOTSTRAP_APPLIED_KEY)
    # Fails OPEN on a corrupt marker, mirroring skills.muted_categories and
    # _applied_seed_keys. Not defensive tidiness: the warning 40 lines below
    # TELLS the admin to hand-edit this JSON ("remove just its entry from the
    # JSON list in the '%s' row"), and init_db is deliberately un-caught in
    # lifespan -- so a dropped comma bricked startup with a raw JSONDecodeError,
    # remediable only by another hand-edit of the same row. Failing open is
    # also the safe direction here: an empty `applied` against an ESTABLISHED
    # allowlist takes the record-only branch, so nothing is re-granted.
    # A marker counts only if it is PRESENT, NON-BLANK, and a JSON list of
    # strings. Each clause is load-bearing, and the first version of this guard
    # had only the parse: `json.loads` succeeding is not the same as the marker
    # being usable, and three shapes that parse fine re-granted an offboarded
    # admin (measured) --
    #   ''    -> `raw or "[]"` made it a legitimate empty list
    #   "{}"  -> an empty set, so nothing reads as applied
    #   '"a@b"' -> a JSON STRING, whose set() is its CHARACTERS
    # ...because `marked` was true while `applied` did not contain the address,
    # selecting the "grant anything not yet applied" branch. The blank one is
    # the plausible one: clearing the cell in a SQLite browser produces exactly
    # it, and the warning below is what sends an operator into this row.
    applied: set[str] = set()
    marked = False
    if raw is not None and raw.strip():
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                raise ValueError("bootstrap marker is not a JSON list")
            applied = {e for e in parsed if isinstance(e, str)}
            marked = True
        except (ValueError, TypeError):
            log.warning("unreadable %s marker; treating it as absent",
                        _BOOTSTRAP_APPLIED_KEY)
    fresh = con.execute("SELECT COUNT(*) AS n FROM allowlist").fetchone()["n"] == 0
    now = time.time()

    # Three cases, and the middle one is the whole point:
    #   fresh install (no allowlist at all)  -> grant; this is the real bootstrap
    #   established, marker absent           -> record only. This is the upgrade
    #       hop. Granting here would restore exactly the admin someone had
    #       already removed -- reproducing the bug once on the way to fixing it.
    #   established, marker present          -> grant anything NOT yet applied,
    #       so an address the operator ADDS to ADMIN_EMAILS later still works.
    for email in emails:
        if email in applied:
            continue
        # On the upgrade hop (`not fresh and not marked`) the conservative
        # choice is record-only, so a previously offboarded admin is not
        # restored. But applied blindly that ALSO swallows an address this
        # deployment has never seen -- and editing `.env` while pulling a new
        # image is the ordinary upgrade, so "add a new admin" silently did
        # nothing, twice over (the marker then recorded it as applied).
        #
        # The two cases are distinguishable: `_remove_user` DELETEs the
        # allowlist row but KEEPS the users row with is_admin=0, so an
        # offboarded admin is still KNOWN here while a genuinely new address is
        # in neither table. Grant only the latter.
        known = con.execute(
            "SELECT 1 FROM users WHERE email=? "
            "UNION ALL SELECT 1 FROM allowlist WHERE email=? LIMIT 1",
            (email, email)).fetchone() is not None
        if fresh or marked or not known:
            con.execute(
                "INSERT INTO allowlist(email, note, added_by, added_at) "
                "VALUES (?, 'bootstrap admin', 'system', ?) "
                "ON CONFLICT(email) DO NOTHING", (email, now))
            con.execute(
                "INSERT INTO users(email, is_admin, created_at) VALUES (?, 1, ?) "
                "ON CONFLICT(email) DO UPDATE SET is_admin=1", (email, now))
        applied.add(email)
    set_meta(con, _BOOTSTRAP_APPLIED_KEY, json.dumps(sorted(applied)))

    # An ADMIN_EMAILS address that is no longer an admin is a legitimate state
    # (someone offboarded them) -- but it is also what a lockout looks like, and
    # silence is what made the old behaviour invisible. Say it once at boot,
    # naming the recovery, so neither case is a surprise.
    for email in emails:
        row = con.execute(
            "SELECT is_admin FROM users WHERE email=?", (email,)).fetchone()
        if row is None or not row["is_admin"]:
            # The remedy must name ONE address. The marker is a single JSON list
            # covering every ADMIN_EMAILS entry, so "delete the meta row" -- what
            # this used to advise -- makes the next boot re-grant ALL of them,
            # including a colleague who was deliberately offboarded. That is
            # precisely the restart-restores-a-removed-admin behaviour this
            # function exists to stop, prescribed by its own warning.
            log.warning(
                "ADMIN_EMAILS lists %s but it is not an admin on this deployment. "
                "That is expected if the account was deliberately removed; nothing "
                "is re-granted on restart. To bootstrap THIS address again, remove "
                "just its entry from the JSON list in the '%s' row of the meta "
                "table in app.db and restart -- do not delete the whole row, which "
                "would also restore any other listed address that was removed.",
                email, _BOOTSTRAP_APPLIED_KEY)


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO meta(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def data_version(con: sqlite3.Connection) -> int:
    return int(get_meta(con, "data_version", "1"))
