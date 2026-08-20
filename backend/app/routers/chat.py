"""Chat API: streaming NL→answer, conversation history, CSV export."""
from __future__ import annotations

import asyncio
import contextlib
import csv
import functools
import io
import json
import logging
import re
import sqlite3
import time
import typing

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app import feedback, guard, lessoncats, ratelimit, skills
from app.auth import current_user
from app.config import get_settings
from app.db import connect, record_usage
from app.llm import cost_is_estimated, effective_cost, generate_title, stream_agent
from app.tools.sql import (
    QueryResult,
    SQLResultTooLargeError,
    SQLTimeoutError,
    SQLValidationError,
    ipeds_years,
    run_sql,
)

log = logging.getLogger("ipeds.chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])

# The LIMIT both recent-context loaders apply. The name says TURNS and the
# comment says MESSAGES, which is half the reason the two loaders below drifted
# apart — READ THEIR DOCSTRINGS before reusing this. _load_history counts
# MESSAGES (6 = ~3 turns); _load_prior_results counts ROWS THAT HAVE RESULTS,
# which are assistant-only (6 = ~6 turns). The windows are therefore roughly 2x
# apart. Renaming this constant does not fix that — only changing one of the
# queries would, and that is a deliberate open question (see _load_prior_results).
HISTORY_TURNS = 6
# Per-turn caps on the result rows persisted for cross-turn grounding
# (messages.results). Grounding needs the numbers, not the whole table, and a
# wide brief could otherwise bloat app.db — so cap rows per result and the total
# serialized size, dropping the largest results first when over budget.
RESULT_STORE_MAX_ROWS = 200
RESULT_STORE_MAX_BYTES = 64_000

# How long ONE candidate probe in _select_table_sql may run. `LIMIT 1` bounds
# the ROWS a probe returns, never the WORK it does, so without this each probe
# could burn the full sql_timeout_seconds (25s) — and sql_log records every
# attempt the agent made, failures included, so 5-8 candidates is routine and
# one CSV request could hold a threadpool worker for two to three minutes.
#
# Deliberately generous: a probe re-runs a query that ALREADY executed during
# the turn, so seconds is plenty. Do not tune this down — a probe timeout is
# swallowed by the candidate-skipping `except` below, so an over-tight value
# turns a slow-but-valid table query into "No runnable query for this answer."
# The winning re-run keeps the full default budget; it is what the user asked
# for.
CSV_PROBE_TIMEOUT_SECONDS = 3.0

# ---------------------------------------------------------------------------
# The persisted-answer field list, in ONE place.
#
# Adding a displayable field to an assistant message used to mean hand-editing
# ~10 sites: a migration, _persist's signature, the INSERT's column list, its
# values tuple, the `?` count, the agent-path call, the cache-hit call, the
# `done` SSE dict, get_conversation's SELECT, and three spots in Chat.jsx.
# Nothing checked any pair against any other, and it has already shipped TWO
# defects — `results_truncated` (missed in the SELECT) and `table_cells_matched`
# (missed on the live path).
#
# The failure is asymmetric, which is what makes it nasty: the reload path
# inherits new fields for free (Chat.jsx spreads `...m`) while the live `done`
# path enumerated them by hand. So a miss looked CORRECT after a refresh and
# wrong only during the turn that produced it — the hardest shape to notice.
#
# MESSAGE_TURN_COLUMNS / MESSAGE_READ_COLUMNS drive the INSERT and the SELECT,
# and a test asserts them against the ACTUAL messages schema (see
# test_every_persisted_turn_field_reaches_the_reader_and_the_done_event). A new
# migration column therefore fails a test until it is either wired up or
# explicitly excluded — a deliberate, reviewable act rather than a remembered
# one.
#
# DONE_EVENT_FIELDS used to be a THIRD hand-typed literal, free to drift from
# the other two exactly the way it did (that literal is how table_cells_matched
# went missing on the live path in the first place). It is now DERIVED as an
# OPT-OUT of MESSAGE_READ_COLUMNS: everything a reload gets, the live `done`
# event gets too, unless it is named below with a reason. That flips the
# default for the NEXT field — a new migration column now rides `done`
# automatically, with no line to remember to add — and it flips what needs
# review: a column that is genuinely reload-only (there is none today) now
# needs an EXPLICIT exclusion, or it silently bloats every `done` frame with a
# value nothing on the live path reads. That failure is at least a visible one
# (a fatter frame) rather than the old invisible one (a field quietly missing
# from the live render), which is why opt-out is the safer default here.

# Columns every persisted assistant turn writes, in INSERT order.
MESSAGE_TURN_COLUMNS: tuple[str, ...] = (
    "sql_log", "thinking", "figure", "suggestions", "clarify", "results",
    "model_used", "tokens", "duration_ms", "results_truncated",
    "figure_grounding", "table_grounding", "table_cells_checked",
    "table_cells_matched",
)

# Written but deliberately NOT returned to the browser.
#   results — the raw query rows, kept for cross-turn grounding only. Capped and
#             backend-only by contract; shipping them would put a second copy of
#             every table on the wire.
#   tokens  — billing telemetry, not answer content.
_BACKEND_ONLY: frozenset[str] = frozenset({"results", "tokens"})

# Row identity/plumbing — not per-turn answer content, so not in the lists above.
_STRUCTURAL: frozenset[str] = frozenset(
    {"id", "conversation_id", "role", "content", "created_at"})

# What get_conversation hands back for a reloaded turn.
MESSAGE_READ_COLUMNS: tuple[str, ...] = tuple(
    c for c in MESSAGE_TURN_COLUMNS if c not in _BACKEND_ONLY)

# Excluded from DONE_EVENT_FIELDS for TWO reasons at once, both load-bearing:
#   1. They already arrive as their own streamed events (the figure/
#      suggestions/clarify events, and the sql/thinking trace items) — a
#      second copy riding `done` would be redundant.
#   2. They are exactly the columns Chat.jsx's `hydrate()` JSON.parses on
#      reload. `_persist` stores each of them as JSON TEXT (see turn_values
#      below), so letting one ride `done` unparsed would hand the LIVE path a
#      raw JSON STRING where reload hands the SAME field a parsed object —
#      a real, structural asymmetry, not a style nit.
_OWN_STREAMED_EVENT: frozenset[str] = frozenset(
    {"sql_log", "thinking", "figure", "suggestions", "clarify"})

# Rides `done` under a different name (the agent-path dict has always used
# "model") rather than under its column name.
_RENAMED_ON_DONE: frozenset[str] = frozenset({"model_used"})

# What the `done` SSE event carries so a LIVE turn renders every affordance
# without waiting for a reload — MESSAGE_READ_COLUMNS minus the two exclusions
# above. Evaluates to exactly the same six names this used to be hand-typed
# as; see the module comment for what deriving it instead buys and costs.
DONE_EVENT_FIELDS: tuple[str, ...] = tuple(
    c for c in MESSAGE_READ_COLUMNS
    if c not in _OWN_STREAMED_EVENT | _RENAMED_ON_DONE)


def _done_extras(turn_values: dict) -> dict:
    """The ONLY consumer of DONE_EVENT_FIELDS: projects the same values
    `_persist` just wrote onto the message row into `done`-event keys, so a
    live turn and a reload of that same turn are reading the SAME dict rather
    than two hand-typed copies that can drift apart. Kept next to the tuples
    it reads, not near its call sites, since a reader auditing "what does
    `done` carry" needs this and DONE_EVENT_FIELDS in the same glance."""
    return {c: turn_values[c] for c in DONE_EVENT_FIELDS}

# Fire-and-forget async tasks (the feedback distiller below) need a strong
# reference kept somewhere until they finish, or asyncio can garbage-collect a
# still-pending Task and log "Task was destroyed but it is pending". A
# module-level set + a done-callback that discards itself is the standard
# pattern for this.
_background_tasks: set[asyncio.Task] = set()


def _guard_usage_kwargs(usage) -> dict:
    """The `_persist` accounting kwargs for a turn whose ONLY LLM call was the
    guard — a refusal, or an answer-cache hit.

    Neither branch has an AgentResult to accumulate into (both call `_persist`
    with literals), which is why this returns kwargs rather than mutating a
    result the way the agent path does. Resisting the urge to manufacture an
    empty AgentResult here is deliberate: it would be one short step from handing
    that object to `_persist` wholesale and writing emit_mode/figure_grounding
    defaults onto a replay row, undoing the NULLs the cache branch documents.

    first_call_* stays 0 — see the note at the agent-path fold."""
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "cached_prompt_tokens": usage.cached_prompt_tokens,
        "cost": effective_cost(usage.cost, usage.prompt_tokens,
                               usage.completion_tokens,
                               cached_prompt_tokens=usage.cached_prompt_tokens),
        "cost_estimated": cost_is_estimated(usage.cost),
    }


def _add_usage(usage_log_id: int, usage) -> None:
    """Add a probe's spend to an ALREADY-COMMITTED usage_log row.

    Two calls a turn causes finish after `_persist` has run: the title call and
    the detached feedback distiller. Neither can be folded in at insert time, and
    neither should be moved ahead of the write — `_persist` is the statement that
    saves the user's answer, and putting a network probe in front of it would turn
    a slow title call into lost data. So they add to the row afterwards.

    cost_estimated uses MAX, not assignment: the flag means "any part of this row
    is an estimate", so a later estimated call must taint an otherwise
    provider-billed row, and a later billed call must not clear the flag.

    Best-effort by design — a lost update costs one probe's accounting, never a
    turn. Both callers already swallow their own failures."""
    if usage_log_id is None or not (usage.prompt_tokens or usage.completion_tokens
                                    or usage.cost):
        return
    con = connect()
    try:
        con.execute(
            "UPDATE usage_log SET prompt_tokens = prompt_tokens + ?, "
            "completion_tokens = completion_tokens + ?, "
            "cached_prompt_tokens = cached_prompt_tokens + ?, "
            "cost = cost + ?, cost_estimated = MAX(cost_estimated, ?) WHERE id = ?",
            (usage.prompt_tokens, usage.completion_tokens, usage.cached_prompt_tokens,
             effective_cost(usage.cost, usage.prompt_tokens, usage.completion_tokens,
                            cached_prompt_tokens=usage.cached_prompt_tokens),
             int(cost_is_estimated(usage.cost)), usage_log_id))
        con.commit()
    finally:
        con.close()


def _fire_and_forget(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _record_feedback_lesson(history: list[dict], question: str,
                                  usage_log_id: int | None = None) -> None:
    """Mine corrective feedback on a follow-up turn into a candidate lesson —
    run as a background task (see _fire_and_forget), NOT awaited from gen(), so
    the SSE stream's body closes and the composer re-enables the instant the
    answer finishes rendering, rather than staying disabled for an extra
    PROBE_TIMEOUT-bounded LLM round-trip after the user already has their
    answer. The answer is already persisted by the time this runs, so a
    failure here only costs a missed lesson, never a broken turn — caught and
    logged rather than left to surface as an "exception never retrieved"
    warning.

    Its spend IS billed, via _add_usage onto the row `_persist` already wrote —
    running detached is why it can't be folded in at insert time, and is not a
    reason to leave a real LLM call unaccounted. Billed BEFORE the lesson is
    saved, so the call is recorded whether or not it found anything: "no lesson
    here" is the common outcome and costs exactly the same."""
    try:
        fb, usage = await feedback.distill_feedback(history, question)
        await run_in_threadpool(_add_usage, usage_log_id, usage)
        if fb:
            await run_in_threadpool(skills.record_lesson_from_feedback, question, fb[0], fb[1])
    except Exception:
        log.exception("feedback-distilled lesson recording failed")


# Fresh-deploy "no data" guard wording (module-level constants for testability
# -- see chat_stream). Admin wording routes to Admin -> Imports; non-admin
# wording just asks them to wait, and must never mention the admin-only UI.
NO_DATA_ADMIN = (
    "No IPEDS dataset is loaded yet. Open Admin → Imports to fetch a year "
    "from NCES (or upload an .accdb). Once a year is integrated, you can ask "
    "data questions here."
)
NO_DATA_USER = (
    "No IPEDS dataset is loaded yet. An administrator needs to load data "
    "before questions can be answered — please check back soon."
)


# A question is a question, not a payload. `BodyLimitMiddleware` caps the whole
# request at max_request_body_mb (10 MB), but 10 MB of "question" would still be
# written to app.db TWICE (the user message + usage_log.question) and sent to the
# provider as billed tokens. ~1,000 tokens is generous for anything a person
# actually asks. Enforced as a 400 with a human message rather than a pydantic
# Field(max_length=...), which 422s with a LIST detail — matching MAX_TITLE_LEN
# below, and keeping the error readable when it reaches the UI. Mirrored
# client-side by the composer's maxLength — keep the two in sync.
MAX_QUESTION_LEN = 4000


class ChatRequest(BaseModel):
    question: str
    conversation_id: int | None = None
    # When re-asking an edited/rerun prompt: drop this message and everything
    # after it first, so the new turn REPLACES the old exchange in place.
    edit_message_id: int | None = None


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _trace_item(ev: dict) -> dict | None:
    """Map a stream event to a persisted "Thinking" trace item, mirroring the
    frontend's live addThought() 1:1 so a reloaded trace renders identically to
    the in-session one (Chat.jsx ThinkingTrace). Non-trace events -> None."""
    t = ev.get("type")
    if t == "status":
        return {"kind": "status", "text": ev.get("text", "")}
    if t == "sql":
        return {"kind": "sql", "text": ev.get("sql", "")}
    if t == "thinking":
        return {"kind": "reason", "text": ev.get("text", "")}
    if t == "tool":
        return {"kind": "tool", "text": f"{ev.get('name', '')}{' ✓' if ev.get('ok') else ' ✗'}"}
    return None


def _results_for_storage(results) -> list | None:
    """QueryResults → a capped, JSON-able list for messages.results, or None when
    there's nothing to store. Caps rows per result, then enforces a total-byte
    ceiling by dropping the LARGEST results first (a headline usually derives from
    a compact table, so the small recent-years/ranking results are the ones worth
    keeping). Never raises — a storage-shaping hiccup must not fail a turn."""
    if not results:
        return None
    try:
        blobs = [r.to_storage(RESULT_STORE_MAX_ROWS) for r in results]
    except Exception:
        return None
    # Drop largest-first until under the byte ceiling (keep original order among
    # survivors so result_index stays meaningful for grounding provenance).
    while blobs and len(json.dumps(blobs)) > RESULT_STORE_MAX_BYTES and len(blobs) > 1:
        widest = max(range(len(blobs)), key=lambda i: len(json.dumps(blobs[i])))
        blobs.pop(widest)
    # The loop above stops at ONE blob and never measures it, so the ceiling used
    # to mean "at most one result may exceed it, unbounded" — `to_storage` caps
    # rows (200) but not WIDTH, and a single value may run to SQL_MAX_VALUE_BYTES
    # (1 MiB), so 200 rows of a wide SELECT * is comfortably megabytes. It is
    # written TWICE (messages.results and query_cache.results), and skills.py's
    # cache_store comment reasons from "already capped by the caller", which is
    # exactly the assumption that broke.
    #
    # The last blob can't be dropped the way the others were — it's the only
    # evidence the turn has — so SHRINK it instead: halve its rows until it fits.
    # Truncating rows can only cost grounding a match it would otherwise have
    # made, i.e. a false `ungrounded`/`partial`, never a false ✓ — the safe
    # direction, and the same trade `to_storage`'s 200-row cap already makes.
    if blobs and len(json.dumps(blobs)) > RESULT_STORE_MAX_BYTES:
        # Flag it BEFORE halving, not after: the halved blob's bytes must sit
        # INSIDE the ceiling this loop measures below, or a borderline blob
        # could slip back over RESULT_STORE_MAX_BYTES once the key is added.
        # A halved blob is exactly as unsound for grounding to aggregate over
        # as one run_sql itself flagged truncated — same reasoning, same flag.
        blobs[0]["truncated"] = True
    while blobs and len(json.dumps(blobs)) > RESULT_STORE_MAX_BYTES:
        rows = blobs[0]["rows"]
        blobs[0]["rows"] = rows[: len(rows) // 2]
        if not blobs[0]["rows"]:
            # Not even one row fits. Store NOTHING rather than a zero-row blob,
            # and the direction matters: a blob with columns and no rows reads
            # to grounding as "checked, and nothing reproduced" — an `unmatched`
            # verdict that raises the ⚠ caution on an answer that is CORRECT.
            # NULL reads as `unchecked`, which renders silently. When the choice
            # is between a false accusation and saying nothing, say nothing.
            return None
    return blobs or None


def _load_prior_results(con: sqlite3.Connection, conv_id: int,
                        before_id: int | None = None) -> list:
    """Recent turns' persisted run_sql results, flattened, for CONVERSATION-SCOPED
    figure grounding (app/grounding.py).

    The `before_id` semantics mirror _load_history exactly, so an edit/rerun
    grounds only against results that will survive the pending delete — never
    against messages about to be dropped.

    The WINDOW SIZE does not, and this used to claim it did. Both pass
    HISTORY_TURNS, but they count different things: _load_history LIMITs over ALL
    messages (6 = ~3 conversational turns), while this LIMITs over rows WHERE
    results IS NOT NULL — which only assistant rows ever are, and only when the
    turn ran SQL. So this reaches back roughly 2x further, and can borrow results
    from turns whose prose the model never had in context.

    That asymmetry is a KNOWN OPEN QUESTION, deliberately left as-is:

      * Narrowing it is defensible in principle — evidence the model could not
        have seen cannot explain its number, so it can only add coincidental
        matches, and both verdicts are now reader-facing (Figure's "verified"
        mark and TableTrust's caution).
      * But it was MEASURED and the measurement could not decide. Replaying
        every graded turn in the retained corpus under both windows changed no
        verdict — and 8 of the 9 turns were fed IDENTICAL inputs, because the
        conversations are too short for the windows to diverge. "No change" was
        therefore not evidence of safety.
      * Shrinking the pool can only turn a reproduction into ungrounded/partial,
        i.e. a FALSE caution on a correct answer — the most damaging way this
        measurement can be wrong (see grounding.py, and the wording rationale in
        frontend/src/tabletruth.js).

    Changing it needs a corpus with several 6+ turn conversations; re-run the
    both-windows replay before touching it. Until then the honest thing is an
    accurate docstring rather than an unmeasured change.

    Malformed/empty JSON is skipped, never raised: this reads persisted data and
    must not break a live turn."""
    if before_id is not None:
        rows = con.execute(
            "SELECT results FROM messages WHERE conversation_id=? AND id<? "
            "AND results IS NOT NULL ORDER BY id DESC LIMIT ?",
            (conv_id, before_id, HISTORY_TURNS)).fetchall()
    else:
        rows = con.execute(
            "SELECT results FROM messages WHERE conversation_id=? "
            "AND results IS NOT NULL ORDER BY id DESC LIMIT ?",
            (conv_id, HISTORY_TURNS)).fetchall()
    out = []
    for r in reversed(rows):  # chronological, matching _load_history
        try:
            for blob in json.loads(r["results"]) or []:
                out.append(QueryResult.from_storage(blob))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return out


def _load_history(con: sqlite3.Connection, conv_id: int,
                  before_id: int | None = None) -> list[dict]:
    """Recent turns fed back to the model. For an edit/rerun, `before_id` is the
    message being replaced: history is loaded as it will look AFTER that message
    (and everything after it) is dropped, WITHOUT deleting anything here — the
    actual delete is folded into _persist's transaction so an interrupted edit
    can never destroy the old exchange on its own."""
    if before_id is not None:
        rows = con.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? AND id<? "
            "ORDER BY id DESC LIMIT ?", (conv_id, before_id, HISTORY_TURNS)).fetchall()
    else:
        rows = con.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? "
            "ORDER BY id DESC LIMIT ?", (conv_id, HISTORY_TURNS)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


@router.post("/stream")
async def chat_stream(req: ChatRequest, user: sqlite3.Row = Depends(current_user)):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "Empty question.")
    if len(question) > MAX_QUESTION_LEN:
        raise HTTPException(
            400, f"Question is too long (max {MAX_QUESTION_LEN:,} characters).")

    # Per-user throttle (SEC-3): cap a single user's chat turns over a rolling
    # window so a runaway loop/script can't burn unbounded provider spend. Raises
    # 429 as a plain JSON error before any streaming/LLM work begins. Disabled
    # when chat_rate_max_per_user <= 0.
    ratelimit.enforce_chat_rate_limit(int(user["id"]))

    # Fresh-deploy "no data" guard: before touching app.db or the agent at
    # all, bail out with a friendly notice if there's no ipeds.db dataset
    # loaded yet. Creates no conversation, persists nothing, runs no agent.
    if not ipeds_years():
        msg = NO_DATA_ADMIN if bool(user["is_admin"]) else NO_DATA_USER

        async def _no_data_gen():
            yield _sse({"type": "answer", "text": msg})
            yield _sse({"type": "done", "no_data": True})

        return StreamingResponse(_no_data_gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    con = connect()
    is_new = not req.conversation_id
    try:
        conv_id = req.conversation_id
        if conv_id:
            owns = con.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?",
                               (conv_id, user["id"])).fetchone()
            if not owns:
                raise HTTPException(404, "Conversation not found.")
            # Editing/rerunning: load history as it will look once the edited
            # message and everything after it are dropped — but DON'T delete yet.
            # The DELETE is folded into _persist's transaction (delete_from_id
            # below) so an interrupted edit turn never destroys the old exchange.
            history = _load_history(con, conv_id, before_id=req.edit_message_id)
            # Recent turns' results, for conversation-scoped figure grounding —
            # same window/before_id semantics as history above.
            prior_results = _load_prior_results(con, conv_id, before_id=req.edit_message_id)
        else:
            # A brand-new conversation is created INSIDE gen() (see below), not
            # here, so a client that disconnects before the turn persists never
            # strands a titled, 0-message phantom in the sidebar.
            history = []
            prior_results = []
    finally:
        con.close()

    # For an edit/rerun, the replaced messages are deleted atomically with the
    # replacement, inside _persist's transaction — never on their own.
    edit_from = req.edit_message_id

    async def gen():
        nonlocal conv_id
        # Turn wall-clock start → the "Thought for N seconds" duration. monotonic
        # so it's immune to a clock adjustment mid-turn.
        t0 = time.monotonic()
        # Create the new conversation only now that the turn is actually running
        # (bug (a) fix): the row + its first message either both land (turn
        # persisted) or the row is reversed by _delete_if_empty in `finally`.
        if is_new:
            conv_id = await run_in_threadpool(
                _create_conversation, user["id"], question)
        try:
            yield _sse({"type": "conversation", "id": conv_id})

            # 0) Topical guardrail: refuse anything that isn't a good-faith IPEDS
            # question (off-topic requests, prompt-injection) BEFORE any cache or
            # model/tool work, so an adversarial message never drives the agent.
            verdict = await guard.classify(question, history)
            if not verdict.allowed:
                answer = guard.REFUSAL
                yield _sse({"type": "answer", "text": answer})
                persisted = await run_in_threadpool(
                    _persist, user["id"], conv_id, question, answer,
                    sql_log=[], model="guard", tokens=verdict.total_tokens,
                    cached=False, ok=True, delete_from_id=edit_from,
                    # A refusal costs exactly one LLM call — the guard's own.
                    **_guard_usage_kwargs(verdict.usage))
                yield _sse({"type": "done", "refused": True,
                            "message_id": persisted.message_id,
                            "user_message_id": persisted.user_message_id,
                            **_done_extras(persisted.turn_values)})
                return

            # 1) Semantic cache: reuse SQL for a near-identical past question.
            # Only a valid shortcut for a fresh, first-turn question — a follow-up
            # inside an existing conversation depends on prior context, so it must
            # never be served a cached answer from a different conversation.
            cached = (await run_in_threadpool(skills.cache_lookup, question, int(user["id"]))
                      if not history else None)
            if cached:
                answer = cached["answer_md"]
                figure = cached.get("figure")
                suggestions = cached.get("suggestions")
                status = "Matched a recent question — reusing its query."
                yield _sse({"type": "status", "text": status})
                if figure:
                    yield _sse({"type": "figure", "figure": figure})
                if suggestions:
                    yield _sse({"type": "suggestions", "suggestions": suggestions})
                yield _sse({"type": "answer", "text": answer})
                persisted = await run_in_threadpool(
                    _persist, user["id"], conv_id, question, answer,
                    sql_log=[cached["final_sql"]] if cached["final_sql"] else [],
                    model="cache", tokens=verdict.total_tokens, cached=True, ok=True,
                    thinking=[{"kind": "status", "text": status}], figure=figure,
                    suggestions=suggestions, delete_from_id=edit_from,
                    # A cache hit is NOT free: the guard ran before the lookup, so
                    # the row carries that one call's spend. `cached=True` still
                    # marks it a hit — the Answer-cache count is unaffected.
                    **_guard_usage_kwargs(verdict.usage),
                    # Replay the cached turn's own result rows onto this message
                    # (migration 31). Without them a cache hit wrote results=NULL
                    # and every LATER turn in the conversation lost the evidence
                    # it would have grounded a recited number against.
                    results=cached.get("results"),
                    results_truncated=cached.get("results_truncated", False))
                # NB no figure_grounding / table_grounding is passed above, so a
                # cache hit carries NEITHER mark. Deliberate, and the same call
                # #215 made: the rows above make re-grading possible now, but
                # doing it would move the Grounded-figures/cells denominators,
                # and a measurement shouldn't shift inside a replay path. The
                # replayed answer is byte-identical to a turn that WAS graded;
                # it just doesn't say so. That NULL is now explicit rather than
                # a second hand-typed list: `_persist` stores figure_grounding/
                # table_grounding as `None` because this call never passed
                # them, and `_done_extras` reads that same `None` back off
                # `turn_values` below — the omission is expressed by what got
                # PERSISTED, not by a separate field the `done` dict used to
                # hand-pick.
                done = {"type": "done", "cached": True,
                        "message_id": persisted.message_id,
                        "user_message_id": persisted.user_message_id,
                        **_done_extras(persisted.turn_values)}
                if is_new and answer:
                    title, title_usage = await generate_title(question, answer)
                    await run_in_threadpool(_add_usage, persisted.usage_id, title_usage)
                    if title:
                        await run_in_threadpool(_update_title, conv_id, title)
                        done["title"] = title
                yield _sse(done)
                return

            # 2) Retrieve learned skills as few-shot context.
            skills_block, skill_ids = await run_in_threadpool(
                skills.retrieve_skills_block, question)
            if skill_ids:
                await run_in_threadpool(skills.bump_hits, skill_ids)

            # 3) Run the agent, streaming progress. Accumulate the same trace the
            # frontend builds live, so it can be persisted and the "Thinking"
            # disclosure survives a reload (not just the in-session turn).
            result = None
            answer = ""
            figure = None
            suggestions = None
            clarify = None
            thinking: list[dict] = []
            async for ev in stream_agent(question, history=history,
                                         skills_block=skills_block,
                                         prior_results=prior_results):
                if ev["type"] == "done":
                    result = ev["result"]
                    continue
                if ev["type"] == "answer":
                    answer = ev["text"]
                elif ev["type"] == "figure":
                    # Structured hero statistic — pass through to the client (below)
                    # and persist alongside the answer, like sql_log/thinking.
                    figure = ev["figure"]
                elif ev["type"] == "suggestions":
                    # Drill-down follow-up questions — same pass-through + persist.
                    suggestions = ev["suggestions"]
                elif ev["type"] == "clarify":
                    # Disambiguation turn — the model asked a clarifying question
                    # instead of answering. Same pass-through + persist pattern.
                    clarify = ev["clarify"]
                item = _trace_item(ev)
                if item:
                    thinking.append(item)
                yield _sse(ev)

            if result is None:
                # The turn produced nothing to persist. Leave the DB untouched —
                # the edit DELETE never fired (it lives in _persist), and a new
                # conversation is reversed by _delete_if_empty in `finally`.
                #
                # NARROWER THAN IT LOOKS, and the old comment here named the wrong
                # causes ("transport error, or the client disconnected"). Neither
                # reaches this line. Every exit from stream_agent yields a terminal
                # `done` carrying the result — including both transport-error
                # branches, and the two that look like bare returns, which go
                # through _final_events. The ONE exception is its no-API-key early
                # return, and guard.classify short-circuits on the same setting, so
                # that path spent nothing and has nothing to bill. A client
                # disconnect doesn't arrive here at all: the generator unwinds at
                # the `yield`, skipping everything below, and only `finally` runs.
                #
                # So the real spend-loss path is CANCELLATION, not this branch —
                # see the note in `finally`.
                yield _sse({"type": "done"})
                return

            # Fold the guard's spend into the turn it gated. Must be AFTER the
            # `result is None` check above (there is no result to fold into before
            # it) and BEFORE effective_cost below, or the estimate misses it.
            result.prompt_tokens += verdict.usage.prompt_tokens
            result.completion_tokens += verdict.usage.completion_tokens
            result.cached_prompt_tokens += verdict.usage.cached_prompt_tokens
            result.cost += verdict.usage.cost
            # NB first_call_* is deliberately NOT touched: it measures the AGENT's
            # schema-prefix reuse, and the guard's prompt is a different prefix.

            duration_ms = round((time.monotonic() - t0) * 1000)
            persisted = await run_in_threadpool(
                _persist, user["id"], conv_id, question, answer or (result.error or ""),
                sql_log=result.sql_log, model=result.model_used,
                tokens=result.total_tokens, cached=False,
                ok=result.error is None, escalated=result.escalated,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cached_prompt_tokens=result.cached_prompt_tokens,
                first_call_prompt_tokens=result.first_call_prompt_tokens,
                first_call_cached_prompt_tokens=result.first_call_cached_prompt_tokens,
                # Cached-prefix tokens are a SUBSET of prompt_tokens and are priced
                # separately (a provider discounts a cache read steeply — DeepSeek
                # 50x — and this app runs ~78% cached), so passing them is what
                # keeps an estimated spend from reading several-fold high.
                cost=effective_cost(result.cost, result.prompt_tokens,
                                    result.completion_tokens,
                                    cached_prompt_tokens=result.cached_prompt_tokens),
                # ...and whether that number is the provider's bill or our estimate,
                # so Admin → Usage can say which. Same predicate effective_cost
                # branches on, never a second copy of the test.
                cost_estimated=cost_is_estimated(result.cost),
                thinking=thinking, figure=figure,
                suggestions=suggestions, clarify=clarify,
                # Observe-only figure-grounding status (app/grounding.py). Only a
                # real agent turn records one: an answer-cache hit and a guard
                # refusal run no query, so there is nothing to ground against and
                # the column stays NULL rather than diluting the measured rate.
                figure_grounding=result.figure_grounding,
                # ...and HOW it was reproduced ("pct_change(q1.awards)"), so a
                # real derivation is distinguishable from a lucky collision.
                figure_derivation=result.figure_derivation,
                # THIS turn's own results (capped), so a LATER turn can ground a
                # figure against them (app/grounding.py, conversation-scoped).
                results=_results_for_storage(result.results),
                results_truncated=any(r.truncated for r in (result.results or [])),
                # Structured-emission telemetry (PR-1): how the turn emitted, and
                # whether the sentinel found residual leak debris in the prose.
                emit_mode=result.emit_mode, leaked=result.leaked,
                # Observe-only table-grounding: how many of the answer table's
                # numeric cells reproduce from the retained rows (app/grounding.py).
                # NULL/0 on cache/refusal turns via the defaults, like figure_grounding.
                table_grounding=result.table_grounding,
                table_cells_checked=result.table_cells_checked,
                table_cells_matched=result.table_cells_matched,
                # Tool-budget exhaustion status (app/llm.py, S5): 'degraded' when the
                # grounding gate replaced fabricated numbers, else 'answered' when the
                # turn exhausted but shipped a synthesis, else NULL. Drives Admin ->
                # Usage's "Exhausted" count.
                exhaustion=("degraded" if result.exhaustion_degraded
                            else "answered" if result.exhausted else None),
                # Turn wall-clock (ms) → the "Thought for N seconds" display.
                duration_ms=duration_ms,
                delete_from_id=edit_from)

            # 4) Cache the successful answer for reuse (first-turn, context-free only).
            # A clarify turn is NEVER cached — it has no data claim to reuse, and
            # caching it would replay a stale disambiguation question verbatim.
            if (not history and result.error is None and answer and result.sql_log
                    and clarify is None):
                await run_in_threadpool(functools.partial(
                    skills.cache_store, question, result.sql_log[-1], answer,
                    result.figure, result.suggestions,
                    # Store the rows behind the answer too, so a hit can replay
                    # them and keep the conversation's grounding chain intact.
                    _results_for_storage(result.results),
                    any(r.truncated for r in (result.results or [])),
                    user_id=int(user["id"])))

            # 4b) If the critic caught a real mistake and forced a correction, capture
            # its finding as an unverified lesson (self-learning from actual errors).
            # First-turn only (like the cache above): a follow-up's bare question
            # ("and for Ohio?") is a context-less, useless retrieval key. A clarify
            # turn never reaches here as a critic-revised turn (the critic never runs
            # on one — see app/llm.py), but the guard is explicit for clarity/safety.
            #
            # lessoncats.is_learnable gates only WHICH findings get stored — never
            # whether the critic forces a revision, which already happened above
            # this point regardless of category (see app/lessoncats.py's module
            # docstring). UNGROUNDED_NUMBER is deliberately excluded: it's already
            # enforced deterministically, per turn, by app/grounding.py, so a stored
            # lesson for it can't fix anything and was the recurring "verify figures
            # before emitting them" lesson an admin kept rejecting.
            if (not history and result.critic_revised
                    and (result.critic_headline or result.critic_description)
                    and result.error is None and result.sql_log
                    and clarify is None
                    and lessoncats.is_learnable(result.critic_category)):
                # Runs AFTER the answer is already persisted, so a failure here must
                # cost only the lesson, never the stream — mirrors
                # _record_feedback_lesson's own try/except below.
                try:
                    await run_in_threadpool(
                        skills.record_lesson_from_critic, question,
                        result.sql_log[-1], result.critic_headline,
                        result.critic_description)
                except Exception:
                    log.exception("critic-derived lesson recording failed")

            # 4c) Mine corrective feedback on a follow-up turn into a candidate
            # lesson (symmetric to the critic above, but from the USER's own
            # correction rather than the model's mistake). Never on a clarify turn
            # (nothing to correct yet) or a refusal (result.error is not None).
            # Fire-and-forget (_record_feedback_lesson): distill_feedback is a
            # separate LLM call bounded by PROBE_TIMEOUT (30s) — awaiting it here
            # would hold the SSE response body open (and the composer disabled)
            # for that whole extra round-trip AFTER the answer has already fully
            # rendered, since the client finalizes the turn on body-close, not on
            # the `done` event alone. Scheduling it instead lets `done` + the
            # response close immediately while the lesson still gets recorded.
            #
            # Only SCHEDULE it when the distiller could actually record something —
            # it needs skills enabled AND a configured LLM key (distill_feedback
            # returns None otherwise). Gating HERE, not just inside the task, keeps
            # a key-free environment (CI/tests) from ever spawning a detached task:
            # a background task still pending when a test event loop tears down
            # stops this async generator finalizing cleanly, non-deterministically
            # dropping its coverage. No key → no task → deterministic.
            cfg = get_settings()
            if (history and clarify is None and result.error is None
                    and cfg.skills_enabled and cfg.llm_api_key):
                _fire_and_forget(_record_feedback_lesson(
                    history, question, persisted.usage_id))

            # duration_ms/results_truncated/figure_grounding/table_grounding/
            # table_cells_checked/table_cells_matched all come from the SAME
            # turn_values `_persist` just wrote (via _done_extras), rather than
            # six hand-picked copies off `result` — that duplication is exactly
            # how this event and the message row went out of step (a 0 here vs
            # a NULL there for an ungrounded turn's cell counts).
            done = {"type": "done", "escalated": result.escalated,
                    "model": result.model_used, "tokens": result.total_tokens,
                    "message_id": persisted.message_id,
                    "user_message_id": persisted.user_message_id,
                    **_done_extras(persisted.turn_values)}
            # 5) Let the model name a brand-new conversation (better than the raw query).
            if is_new and result.error is None and answer:
                title, title_usage = await generate_title(question, answer)
                # Runs after _persist committed, so its spend is added to that row
                # rather than folded in (see _add_usage). Moving the call ahead of
                # the write would put a network probe in front of the statement
                # that saves the answer.
                await run_in_threadpool(_add_usage, persisted.usage_id, title_usage)
                if title:
                    await run_in_threadpool(_update_title, conv_id, title)
                    done["title"] = title
            yield _sse(done)
        finally:
            # KNOWN GAP — a CANCELLED turn is billed nothing. When the client
            # disconnects (closed tab, dropped network, refresh) this generator
            # unwinds at whichever `yield` it was on, so _persist never runs and
            # the guard's spend plus however many tool rounds the agent had burned
            # are lost. Bounded by one turn, but a turn is big: an ordinary
            # question measured 55,605 prompt tokens. Every NORMAL path bills
            # every call (see _guard_usage_kwargs / _add_usage); this is the one
            # exception. NB "Stop generating" is NOT affected — it is
            # abandon-and-drain, so the request completes and bills normally.
            #
            # The fix, when it's worth doing: stream_agent builds one AgentResult
            # up front and mutates it in place all turn, so letting the caller
            # supply that object would give this block a live reference to the
            # accumulated usage even with no terminal `done` — and this `finally`
            # already survives cancellation (see the shield below). Needs a
            # did-we-already-persist guard, or a normal turn bills twice. A row
            # here would NOT distort `queries`: a cancelled turn is a real
            # question the user asked and paid for, unlike a title/feedback probe.
            #
            # Compensating cleanup (bug (a)): a brand-new conversation that never
            # received a message — interrupted turn, or the result-None return
            # above — must not linger as a phantom. _delete_if_empty is a no-op
            # once any turn persisted, so it can't clobber real history. Shielded
            # so it still completes if the turn was cancelled (client disconnect).
            if is_new:
                with contextlib.suppress(BaseException):
                    await asyncio.shield(run_in_threadpool(_delete_if_empty, conv_id))

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _create_conversation(user_id: int, question: str) -> int:
    """Insert a fresh conversation row and return its id. Called at the TOP of
    the stream generator (not before it), so a client that disconnects before
    the turn runs never creates a row, and _delete_if_empty reverses it if the
    turn then persists nothing (bug (a): no phantom, 0-message conversations)."""
    con = connect()
    try:
        now = time.time()
        cur = con.execute(
            "INSERT INTO conversations(user_id, title, created_at, updated_at) "
            "VALUES (?,?,?,?)", (user_id, question[:80], now, now))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _delete_if_empty(conv_id: int) -> None:
    """Remove a conversation only if it has no messages — the compensating
    cleanup for an interrupted first turn. The NOT EXISTS gate makes it a no-op
    for any conversation that persisted a turn, so it can never clobber real
    history regardless of when/whether _persist committed."""
    con = connect()
    try:
        con.execute(
            "DELETE FROM conversations WHERE id=? AND NOT EXISTS "
            "(SELECT 1 FROM messages WHERE conversation_id=?)", (conv_id, conv_id))
        con.commit()
    finally:
        con.close()


class _PersistResult(typing.NamedTuple):
    """_persist's return value: a NamedTuple, not a plain tuple, so the NEXT
    field a caller needs is reached by NAME (`persisted.turn_values`) instead
    of by position — a plain tuple is exactly how DONE_EVENT_FIELDS's old
    hand-typed literal could drift unnoticed.

    Every caller reaches `turn_values` by attribute, never by unpacking a 4th
    positional name — that friction is the point: the next field added here is
    an explicit, reviewed access rather than a silent positional slot."""
    user_message_id: int
    message_id: int
    usage_id: int
    turn_values: dict


def _persist(user_id, conv_id, question, answer, *, sql_log, model, tokens,
             cached, ok, escalated=False, prompt_tokens=0, completion_tokens=0,
             cached_prompt_tokens=0, first_call_prompt_tokens=0,
             first_call_cached_prompt_tokens=0, cost=0.0, cost_estimated=False,
             thinking=None, figure=None,
             suggestions=None, clarify=None, figure_grounding=None,
             figure_derivation=None, results=None, emit_mode=None, leaked=False,
             table_grounding=None, table_cells_checked=0, table_cells_matched=0,
             duration_ms=None, exhaustion=None, results_truncated=False,
             delete_from_id=None):
    """Persist the user + assistant messages and usage row. Returns the new
    assistant message id (so the stream can hand it to the client without a
    full conversation reload).

    For an edit/rerun, `delete_from_id` is the message being replaced: the old
    message and everything after it are DELETEd as the first statement of this
    same transaction, so the destructive delete and its replacement commit
    atomically — an interrupted edit turn never runs _persist, so the old
    exchange is left intact (bug (b))."""
    con = connect()
    try:
        now = time.time()
        if delete_from_id is not None:
            con.execute("DELETE FROM messages WHERE conversation_id=? AND id>=?",
                        (conv_id, delete_from_id))
        ucur = con.execute(
            "INSERT INTO messages(conversation_id, role, content, created_at) "
            "VALUES (?,?,?,?)", (conv_id, "user", question, now))
        user_msg_id = ucur.lastrowid
        # One mapping, keyed by column name, so the column list and the values
        # can't drift out of step and the `?` count is derived, never counted by
        # hand. Ordered by MESSAGE_TURN_COLUMNS below.
        turn_values = {
            "sql_log": json.dumps(sql_log),
            "thinking": json.dumps(thinking or []),
            "figure": json.dumps(figure) if figure else None,
            "suggestions": json.dumps(suggestions) if suggestions else None,
            "clarify": json.dumps(clarify) if clarify else None,
            "results": json.dumps(results) if results else None,
            "model_used": model,
            "tokens": tokens,
            "duration_ms": duration_ms,
            "results_truncated": int(bool(results_truncated)),
            # STATUS only — the derivation string stays backend telemetry on
            # usage_log. This is what lets a reproduced figure keep its
            # "verified" mark across a reload.
            "figure_grounding": figure_grounding or None,
            # Same idea for the table: status + the two counts, so the answer can
            # say how many of its numbers reproduced. NULL (not 0) when nothing
            # was graded — a cache hit and a refusal pass table_grounding=None,
            # and NULL is what renders no mark rather than a "0 values" claim.
            "table_grounding": table_grounding or None,
            "table_cells_checked": int(table_cells_checked) if table_grounding else None,
            "table_cells_matched": int(table_cells_matched) if table_grounding else None,
        }
        cols = ("conversation_id", "role", "content", *MESSAGE_TURN_COLUMNS, "created_at")
        cur = con.execute(
            f"INSERT INTO messages({', '.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            (conv_id, "assistant", answer,
             *(turn_values[c] for c in MESSAGE_TURN_COLUMNS), now))
        assistant_id = cur.lastrowid
        con.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        # Inside THIS transaction, on THIS connection — the billing row and the
        # messages it bills for commit together or not at all. `source` stays
        # NULL: that column exists to mark the MCP door (app/mcpsrv/ask.py), and
        # a chat turn is what every row without it has always been.
        usage_id = record_usage(
            con, user_id=user_id, question=question, model_used=model,
            escalated=escalated, ok=ok, cached=cached,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
            first_call_prompt_tokens=first_call_prompt_tokens,
            first_call_cached_prompt_tokens=first_call_cached_prompt_tokens,
            cost=cost, cost_estimated=cost_estimated,
            figure_grounding=figure_grounding, figure_derivation=figure_derivation,
            emit_mode=emit_mode, answer_leaked=leaked,
            table_grounding=table_grounding,
            table_cells_checked=table_cells_checked,
            table_cells_matched=table_cells_matched,
            exhaustion=exhaustion, created_at=now)
        con.commit()
        # The usage_log id comes back so a probe that finishes AFTER this commit
        # (the title call, the detached feedback distiller) can add its spend with
        # _add_usage instead of being silently unbilled. turn_values comes back
        # too, so a caller can project the `done` event's extras
        # (_done_extras) from the SAME values just written, rather than a
        # second hand-typed copy.
        return _PersistResult(user_msg_id, assistant_id, usage_id,
                              turn_values)
    finally:
        con.close()


def _update_title(conv_id: int, title: str) -> None:
    con = connect()
    try:
        con.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
        con.commit()
    finally:
        con.close()


@router.get("/conversations")
def list_conversations(user: sqlite3.Row = Depends(current_user)):
    con = connect()
    try:
        rows = con.execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "WHERE user_id=? ORDER BY updated_at DESC LIMIT 100", (user["id"],)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


@router.get("/conversations/{conv_id}")
def get_conversation(conv_id: int, user: sqlite3.Row = Depends(current_user)):
    con = connect()
    try:
        owns = con.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?",
                           (conv_id, user["id"])).fetchone()
        if not owns:
            raise HTTPException(404, "Not found.")
        rows = con.execute(
            "SELECT id, role, content, created_at, "
            f"{', '.join(MESSAGE_READ_COLUMNS)} "
            "FROM messages WHERE conversation_id=? ORDER BY id", (conv_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


class RenameRequest(BaseModel):
    title: str


# The UI truncates sidebar titles anyway; anything longer than this is
# noise (and an unbounded write). Mirrored client-side by the rename input's
# maxLength — keep the two in sync.
MAX_TITLE_LEN = 200


@router.patch("/conversations/{conv_id}")
def rename_conversation(conv_id: int, body: RenameRequest,
                        user: sqlite3.Row = Depends(current_user)):
    """Rename a conversation the caller owns.

    Metadata-only by contract: deliberately does NOT touch updated_at, so
    renaming an old chat never jumps it to the top of the recency-ordered
    sidebar (list_conversations orders by updated_at DESC)."""
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "Title can't be empty.")
    if len(title) > MAX_TITLE_LEN:
        raise HTTPException(400, f"Title is too long (max {MAX_TITLE_LEN} characters).")
    con = connect()
    try:
        owns = con.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?",
                           (conv_id, user["id"])).fetchone()
        if not owns:
            raise HTTPException(404, "Not found.")
        con.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "title": title}


@router.delete("/conversations/{conv_id}")
def delete_conversation(conv_id: int, user: sqlite3.Row = Depends(current_user)):
    con = connect()
    try:
        owns = con.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?",
                           (conv_id, user["id"])).fetchone()
        if not owns:
            raise HTTPException(404, "Not found.")
        con.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
        con.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        con.commit()
    finally:
        con.close()
    return {"ok": True}


def _select_table_sql(sql_list: list[str], cols: int | None, cap: int):
    """Return the FULL run of the SQL whose result IS the displayed table.

    NOT simply `sql_list[-1]`: under the "state the full COUNT(*)" listing rule the
    answer's LAST query is often a scalar count, not the ranking the user sees —
    re-running it hands back "total: 834" instead of the 834 rows. And NOT a full
    run of EVERY candidate: that materialized up to N × `cap` (100k) rows at once,
    a resource-exhaustion amplifier on a multi-query turn. Instead PROBE each
    candidate cheaply (LIMIT 1) for its column shape, pick the best match — an
    exact column-count match with the shown table first (the caller passes it),
    then any real (multi-column) table, ties broken by the LAST such query
    (closest to the answer, since the listing usually runs right before its
    COUNT) — and run ONLY that one at the full cap. A candidate that fails to
    validate OR times out is skipped; raise only if NOTHING ran (→ 400)."""
    best_i, best_key = None, None
    for i, sql in enumerate(sql_list):
        try:
            probe = run_sql(sql, limit=1, timeout=CSV_PROBE_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 — see below; a bad candidate is normal
            # ANY failure skips the candidate, not just validation/timeout.
            #
            # A FAILED query in sql_log is the normal case, not the exceptional
            # one: the agent runs a query, SQLite rejects it, the agent fixes it
            # and re-runs — and sql_log records EVERY attempt, errors included.
            # A real answer therefore routinely carries a query that cannot run.
            #
            # This used to catch only SQLValidationError/SQLTimeoutError, so a
            # plain sqlite3.OperationalError ("ambiguous column name: year",
            # from a JOIN the model corrected on its next attempt) escaped both
            # this loop AND the caller's try/except, and the export 500'd with
            # no detail — reaching the user as the generic "Couldn't build that
            # CSV. Try again in a moment." Waiting could never help: the failing
            # query is persisted, so it failed identically every time.
            continue
        key = (cols is not None and len(probe.columns) == cols, len(probe.columns) > 1)
        if best_key is None or key >= best_key:  # >= → a later tie wins (last match)
            best_i, best_key = i, key
    if best_i is None:
        raise SQLValidationError("No runnable query for this answer.")
    return run_sql(sql_list[best_i], limit=cap)


# A cell that parses as nothing but a plain signed integer/decimal — an
# ordinary negative number, which IPEDS data legitimately contains (a
# year-over-year delta) and must NOT be guarded, or every negative number in
# every export would grow a stray leading apostrophe.
_CSV_PLAIN_NEGATIVE_NUMBER_RE = re.compile(r"^-\d+(\.\d+)?$")


def _csv_guard(value):
    """Prefix a formula-injection-shaped STRING cell with a leading single
    quote so Excel/Sheets renders it as text instead of evaluating it as a
    formula (or, via DDE, an OS command) when the CSV is opened.

    Mirrors `toCsv`'s `esc` guard in frontend/src/tabledata.js — the two exist
    over different data (this one guards real query rows/aliases from
    `ipeds.db`; that one guards the model's Markdown-table transcription) and
    can't be unified, but the RULE must move together if it ever changes.

    A cell is guarded when its first character is one of =, +, @, TAB, or CR
    — none of those is ever a legitimate leading character in IPEDS data or a
    SQL alias. A leading '-' is guarded only when the WHOLE cell doesn't parse
    as a plain signed number (`_CSV_PLAIN_NEGATIVE_NUMBER_RE`): "-1234" is an
    ordinary negative and is left alone, but "-1+cmd|' /C calc'!A0" has the
    extra tokens a real DDE-injection payload needs and is guarded like the
    other trigger characters.

    Only `str` values are touched. `result.rows` also carries ints, floats,
    and `None` straight from sqlite3 — none of those can carry a
    formula-trigger character, and stringifying one here would change how
    `csv.writer` renders it (an int must stay unquoted; `None` must stay the
    empty field, not the text "None").
    """
    if not isinstance(value, str) or not value:
        return value
    first = value[0]
    if first in ("=", "+", "@", "\t", "\r"):
        return "'" + value
    if first == "-" and not _CSV_PLAIN_NEGATIVE_NUMBER_RE.match(value):
        return "'" + value
    return value


@router.get("/messages/{message_id}/download.csv")
def download_csv(message_id: int, request: Request, cols: int | None = None,
                 user: sqlite3.Row = Depends(current_user)):
    """Re-execute the answer's TABLE query (higher row cap) and stream a CSV.

    Re-running is intentional: it guarantees the download reflects current data
    and avoids relying on any per-request in-memory result. `cols` (the displayed
    table's column count, from the client) disambiguates WHICH of the answer's
    queries produced the table — see `_select_table_sql`.
    """
    # A download re-runs a query at the LARGE download cap, so throttle it the same
    # as a chat turn — otherwise it's a scriptable DB/memory-pressure vector.
    ratelimit.enforce_chat_rate_limit(int(user["id"]))
    con = connect()
    try:
        row = con.execute(
            "SELECT m.sql_log FROM messages m JOIN conversations c "
            "ON c.id=m.conversation_id WHERE m.id=? AND c.user_id=?",
            (message_id, user["id"])).fetchone()
    finally:
        con.close()
    if not row:
        raise HTTPException(404, "Message not found.")
    sql_list = json.loads(row["sql_log"] or "[]")
    if not sql_list:
        raise HTTPException(400, "No query is associated with this answer.")
    try:
        result = _select_table_sql(sql_list, cols, get_settings().sql_row_cap_download)
    except SQLValidationError as e:
        raise HTTPException(400, str(e)) from e
    except SQLTimeoutError as e:
        raise HTTPException(504, "The query took too long to export.") from e
    except SQLResultTooLargeError as e:
        # The whole-result byte budget (sql.py's SQL_MAX_RESULT_BYTES). This is
        # the path that budget exists for: the download cap is 100k rows, and
        # without a total ceiling one wide result could exhaust the container's
        # memory before a single CSV byte was written. 413 rather than 400 —
        # the request was well-formed, the RESULT is too big.
        raise HTTPException(413, str(e)) from e

    # Serialize row-by-row so a 100k-row CSV isn't also buffered whole as one
    # string on top of the already-materialized rows.
    def _csv_stream():
        buf = io.StringIO()
        w = csv.writer(buf)
        # Both the header (model-written SQL aliases) and the data rows are
        # guarded against formula injection before csv.writer's own RFC-4180
        # quoting runs — see `_csv_guard`.
        w.writerow([_csv_guard(c) for c in result.columns])
        yield buf.getvalue()
        for r in result.rows:
            buf.seek(0)
            buf.truncate(0)
            w.writerow([_csv_guard(v) for v in r])
            yield buf.getvalue()

    return StreamingResponse(
        _csv_stream(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="ipeds_result_{message_id}.csv"'})
