"""Chat API: streaming NL→answer, conversation history, CSV export."""
from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import logging
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app import feedback, guard, skills
from app.auth import current_user
from app.config import get_settings
from app.db import connect
from app.llm import effective_cost, generate_title, stream_agent
from app.tools.sql import QueryResult, SQLValidationError, ipeds_years, run_sql

log = logging.getLogger("ipeds.chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])

HISTORY_TURNS = 6  # prior messages fed back to the model for context
# Per-turn caps on the result rows persisted for cross-turn grounding
# (messages.results). Grounding needs the numbers, not the whole table, and a
# wide brief could otherwise bloat app.db — so cap rows per result and the total
# serialized size, dropping the largest results first when over budget.
RESULT_STORE_MAX_ROWS = 200
RESULT_STORE_MAX_BYTES = 64_000

# Fire-and-forget async tasks (the feedback distiller below) need a strong
# reference kept somewhere until they finish, or asyncio can garbage-collect a
# still-pending Task and log "Task was destroyed but it is pending". A
# module-level set + a done-callback that discards itself is the standard
# pattern for this.
_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _record_feedback_lesson(history: list[dict], question: str) -> None:
    """Mine corrective feedback on a follow-up turn into a candidate lesson —
    run as a background task (see _fire_and_forget), NOT awaited from gen(), so
    the SSE stream's body closes and the composer re-enables the instant the
    answer finishes rendering, rather than staying disabled for an extra
    PROBE_TIMEOUT-bounded LLM round-trip after the user already has their
    answer. The answer is already persisted by the time this runs, so a
    failure here only costs a missed lesson, never a broken turn — caught and
    logged rather than left to surface as an "exception never retrieved"
    warning. Like generate_title's title call, this call's own token/cost
    usage is intentionally NOT recorded in usage_log (a cheap probe call, not
    part of the billed turn)."""
    try:
        fb = await feedback.distill_feedback(history, question)
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
    return blobs or None


def _load_prior_results(con: sqlite3.Connection, conv_id: int,
                        before_id: int | None = None) -> list:
    """Recent turns' persisted run_sql results, flattened, for CONVERSATION-SCOPED
    figure grounding (app/grounding.py). Mirrors _load_history's before_id window
    EXACTLY, so an edit/rerun grounds only against results that will survive the
    pending delete — never against messages about to be dropped. Malformed/empty
    JSON is skipped, never raised: this reads persisted data and must not break a
    live turn."""
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
                user_msg_id, msg_id = await run_in_threadpool(
                    _persist, user["id"], conv_id, question, answer,
                    sql_log=[], model="guard", tokens=verdict.tokens,
                    cached=False, ok=True, delete_from_id=edit_from)
                yield _sse({"type": "done", "refused": True, "message_id": msg_id,
                            "user_message_id": user_msg_id})
                return

            # 1) Semantic cache: reuse SQL for a near-identical past question.
            # Only a valid shortcut for a fresh, first-turn question — a follow-up
            # inside an existing conversation depends on prior context, so it must
            # never be served a cached answer from a different conversation.
            cached = await run_in_threadpool(skills.cache_lookup, question) if not history else None
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
                user_msg_id, msg_id = await run_in_threadpool(
                    _persist, user["id"], conv_id, question, answer,
                    sql_log=[cached["final_sql"]] if cached["final_sql"] else [],
                    model="cache", tokens=0, cached=True, ok=True,
                    thinking=[{"kind": "status", "text": status}], figure=figure,
                    suggestions=suggestions, delete_from_id=edit_from)
                done = {"type": "done", "cached": True, "message_id": msg_id,
                        "user_message_id": user_msg_id}
                if is_new and answer:
                    title = await generate_title(question, answer)
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
                # The turn produced nothing to persist (transport error, or the
                # client disconnected mid-stream). Leave the DB untouched — the
                # edit DELETE never fired (it lives in _persist), and a new
                # conversation is reversed by _delete_if_empty in `finally`.
                yield _sse({"type": "done"})
                return

            user_msg_id, msg_id = await run_in_threadpool(
                _persist, user["id"], conv_id, question, answer or (result.error or ""),
                sql_log=result.sql_log, model=result.model_used,
                tokens=result.total_tokens, cached=False,
                ok=result.error is None, escalated=result.escalated,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cached_prompt_tokens=result.cached_prompt_tokens,
                first_call_prompt_tokens=result.first_call_prompt_tokens,
                first_call_cached_prompt_tokens=result.first_call_cached_prompt_tokens,
                cost=effective_cost(result.cost, result.prompt_tokens,
                                    result.completion_tokens),
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
                delete_from_id=edit_from)

            # 4) Cache the successful answer for reuse (first-turn, context-free only).
            # A clarify turn is NEVER cached — it has no data claim to reuse, and
            # caching it would replay a stale disambiguation question verbatim.
            if (not history and result.error is None and answer and result.sql_log
                    and clarify is None):
                await run_in_threadpool(skills.cache_store, question, result.sql_log[-1],
                                        answer, result.figure, result.suggestions)

            # 4b) If the critic caught a real mistake and forced a correction, capture
            # its finding as an unverified lesson (self-learning from actual errors).
            # First-turn only (like the cache above): a follow-up's bare question
            # ("and for Ohio?") is a context-less, useless retrieval key. A clarify
            # turn never reaches here as a critic-revised turn (the critic never runs
            # on one — see app/llm.py), but the guard is explicit for clarity/safety.
            if (not history and result.critic_revised
                    and (result.critic_headline or result.critic_description)
                    and result.error is None and result.sql_log
                    and clarify is None):
                await run_in_threadpool(
                    skills.record_lesson_from_critic, question,
                    result.sql_log[-1], result.critic_headline, result.critic_description)

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
                _fire_and_forget(_record_feedback_lesson(history, question))

            done = {"type": "done", "escalated": result.escalated,
                    "model": result.model_used, "tokens": result.total_tokens,
                    "message_id": msg_id, "user_message_id": user_msg_id}
            # 5) Let the model name a brand-new conversation (better than the raw query).
            if is_new and result.error is None and answer:
                title = await generate_title(question, answer)
                if title:
                    await run_in_threadpool(_update_title, conv_id, title)
                    done["title"] = title
            yield _sse(done)
        finally:
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


def _persist(user_id, conv_id, question, answer, *, sql_log, model, tokens,
             cached, ok, escalated=False, prompt_tokens=0, completion_tokens=0,
             cached_prompt_tokens=0, first_call_prompt_tokens=0,
             first_call_cached_prompt_tokens=0, cost=0.0, thinking=None, figure=None,
             suggestions=None, clarify=None, figure_grounding=None,
             figure_derivation=None, results=None, delete_from_id=None):
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
        cur = con.execute(
            "INSERT INTO messages(conversation_id, role, content, sql_log, "
            "thinking, figure, suggestions, clarify, results, model_used, tokens, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (conv_id, "assistant", answer, json.dumps(sql_log),
             json.dumps(thinking or []),
             json.dumps(figure) if figure else None,
             json.dumps(suggestions) if suggestions else None,
             json.dumps(clarify) if clarify else None,
             json.dumps(results) if results else None, model, tokens, now))
        assistant_id = cur.lastrowid
        con.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        con.execute(
            "INSERT INTO usage_log(user_id, question, model_used, escalated, "
            "prompt_tokens, completion_tokens, cached_prompt_tokens, "
            "first_call_prompt_tokens, first_call_cached_prompt_tokens, "
            "ok, cached, cost, figure_grounding, figure_derivation, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, question, model, int(escalated), prompt_tokens,
             completion_tokens, cached_prompt_tokens, first_call_prompt_tokens,
             first_call_cached_prompt_tokens, int(ok), int(cached),
             float(cost), figure_grounding or None, figure_derivation or None, now))
        con.commit()
        return user_msg_id, assistant_id
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
            "SELECT id, role, content, sql_log, thinking, figure, suggestions, clarify, "
            "model_used, created_at "
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


@router.get("/messages/{message_id}/download.csv")
def download_csv(message_id: int, request: Request, user: sqlite3.Row = Depends(current_user)):
    """Re-execute the answer's final SQL (higher row cap) and stream a CSV.

    Re-running is intentional: it guarantees the download reflects current data
    and avoids relying on any per-request in-memory result.
    """
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
        result = run_sql(sql_list[-1], limit=get_settings().sql_row_cap_download)
    except SQLValidationError as e:
        raise HTTPException(400, str(e)) from e

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(result.columns)
    w.writerows(result.rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="ipeds_result_{message_id}.csv"'})
