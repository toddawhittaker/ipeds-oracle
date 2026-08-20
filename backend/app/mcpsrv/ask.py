"""The `ask` tool: the whole agent, as one MCP tool call.

The seven data tools hand an MCP client the same primitives the chat agent uses
and leave it to drive them. `ask` is the other end of that: one question in, the
answer the web app would have given out — the guardrail, the answer cache, the
learned lessons, the tool loop, the SQL linter, the critic and the grounding
checks, all of it, in the order `app/routers/chat.py` runs them.

STATELESS. It writes no `conversations` row and no `messages` row, so there is
no thread, no history, and no follow-up. Everything the chat path does to serve
a CONVERSATION is therefore skipped, and each omission is a consequence of that
one decision rather than a separate judgement call:

  * conversation titling — there is no conversation to name;
  * the feedback distiller — it mines a correction made on a LATER turn;
  * result persistence for cross-turn grounding — the rows are retained for the
    NEXT turn in a thread, and there is no next turn. `ask` still grounds this
    turn's own figure, which is what the returned status reports.

A CLARIFYING QUESTION COMES BACK AS THE ANSWER. When the model judges a question
ambiguous enough that the reading changes the headline, it asks instead of
querying; the chat UI renders that as chips to click, and there is nothing to
click here. The caller gets the question as prose and re-asks a narrower one,
which is the same escape hatch the composer always offers. Such a turn is not
cached, exactly as in chat — a stale disambiguation replayed verbatim is worse
than asking again.

Two more, which are NOT consequences of statelessness and so are named
deliberately here:

  * NO CRITIC-DERIVED LESSON. When the critic catches the model in a real
    mistake, the chat path files that finding as an unverified lesson for an
    admin to review. `ask` does not, because a key holder reaching this endpoint
    from the internet would otherwise be able to steer what lands in that queue
    just by choosing questions, and the queue is a human's attention. Chat is
    the door where a lesson is earned. This is worth revisiting once MCP traffic
    is something an operator can see; it is not an oversight.
  * NO SPEND GOES UNBILLED. Every path out of here that costs an LLM call —
    the guard's refusal, a cache hit's guard call, a full turn — writes its
    `usage_log` row with `source='mcp'` before returning.

The per-user chat rate limit is charged here ON PURPOSE, the same limiter and
the same table the web chat uses. One person's spend is capped whichever door
they came through; the per-key MCP limit in `app/mcpsrv/auth.py` applies on top
of it, not instead of it.
"""
from __future__ import annotations

import json
import logging
import re
import time

import mcp.types as types
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from app import guard, ratelimit, skills
from app.db import connect, record_usage
from app.llm import cost_is_estimated, effective_cost, stream_agent
from app.mcpsrv.auth import current_caller
from app.routers.chat import MAX_QUESTION_LEN, _results_for_storage
from app.tools.sql import ipeds_years

log = logging.getLogger("ipeds.mcp")

TOOL_NAME = "ask"

# Deliberately NOT app/routers/chat.py's NO_DATA_USER/NO_DATA_ADMIN. Those tell a
# person which admin screen to open; an MCP client has no browser and no admin
# console, so the useful thing to say is what is wrong and who fixes it.
NO_DATA = ("No IPEDS dataset is loaded yet, so there is nothing to query. An "
           "administrator of this deployment needs to import a year first.")

DESCRIPTION = (
    "Ask a natural-language question about U.S. postsecondary education and get "
    "a written answer grounded in the IPEDS data, with the SQL run for you. Use "
    "this when you want an answer; use `run_sql` and the lookup tools when you "
    "want to drive the queries yourself. One question per call — this tool keeps "
    "no conversation, so each call must stand on its own."
)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "A complete, self-contained question about U.S. "
                           "postsecondary education, e.g. 'How many nursing "
                           "bachelor's degrees did Ohio public universities "
                           "award in 2023?'",
            "maxLength": MAX_QUESTION_LEN,
        },
    },
    "required": ["question"],
}

# A ```chart fence is a RENDERING DIRECTIVE for the web app's Chart.jsx, not
# prose — `llm.py::_reconstruct_answer` writes it into the answer text on purpose
# and `frontend/src/Markdown.jsx` turns it back into a figure. That contract is
# the web UI's, and it does not travel: forwarded verbatim over MCP the spec
# reaches the caller as undeclared JSON in the middle of a sentence, where a
# model reads it as part of the answer and a chat client renders it as an opaque
# block of code.
#
# So this boundary DECODES it — MCP has a field channel the browser does not, and
# `output_schema` is where a payload like this is supposed to live. That is not
# the regex-on-prose trap: the server wrote this fence with `json.dumps`, so
# reading it back is decoding a format we defined, not guessing at a model's
# quirks. The unparseable case exists only because the fence FALLBACK path
# (structured emission off, or a model that cannot call tools) lets the model
# write it.
#
# The newline is required so this can never match a ```chartjs block, the same
# guard `frontend/src/Chat.jsx`'s own chart regex carries.
_CHART_FENCE_RE = re.compile(r"```chart[ \t]*\r?\n(.*?)```", re.DOTALL)


def split_chart(answer: str) -> tuple[str, dict | None]:
    """`(answer_without_any_chart_fence, chart_spec_or_None)`.

    ALWAYS strips every chart fence, even one it cannot parse — the same rule
    `llm.py`'s figure and followups extractors follow, and for the same reason:
    a block we could not read is still not something to hand a caller as prose.
    Returns a spec only when the fence holds a JSON object.
    """
    matches = _CHART_FENCE_RE.findall(answer or "")
    if not matches:
        return answer or "", None
    clean = _CHART_FENCE_RE.sub("", answer).strip()
    try:
        spec = json.loads(matches[0].strip())
    except (json.JSONDecodeError, ValueError):
        return clean, None
    return clean, (spec if isinstance(spec, dict) and spec else None)


OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string",
                   "description": "The answer, as Markdown. Any chart the answer "
                                  "came with is in `chart`, not inline."},
        "figure": {
            "type": ["object", "null"],
            "description": "The answer's hero statistic — the one number it "
                           "leads with — or null when no single number "
                           "summarizes the result.",
            "properties": {
                "value": {"description": "The number itself."},
                "unit": {"type": "string"},
                "label": {"type": "string"},
                "source": {"type": "string"},
            },
        },
        "chart": {
            "type": ["object", "null"],
            "description": "A chart the answer offered to illustrate itself — "
                           "`type` (line/bar/…), the axis keys, and the plotted "
                           "`data` rows — or null when it offered none. The "
                           "numbers in it are the same ones the prose cites; "
                           "render it, ignore it, or read the rows directly.",
        },
        "figure_grounding": {
            "type": ["string", "null"],
            "description": "Whether the server could reproduce `figure.value` "
                           "from the rows the query actually returned: 'exact', "
                           "'rounded' or 'derived' means yes and the number is "
                           "checked; 'ungrounded' means the model's number "
                           "could not be reproduced and should be treated as "
                           "unverified; 'no_figure'/'unchecked' mean there was "
                           "nothing to check. Null on a cached or refused "
                           "answer, which runs no query.",
        },
    },
    "required": ["answer", "figure", "chart", "figure_grounding"],
}

TOOL = types.Tool(name=TOOL_NAME, description=DESCRIPTION,
                  input_schema=INPUT_SCHEMA, output_schema=OUTPUT_SCHEMA)


def _answer(text: str, figure: dict | None = None,
            figure_grounding: str | None = None) -> types.CallToolResult:
    """A successful `ask`, in both channels MCP gives us: the Markdown for a
    model to read, and the same thing as fields for the caller's own code.

    The chart is split out HERE rather than at the call sites, because there are
    two of them — a fresh turn and a cache replay — and the cache stores the
    answer with its fence still in it (the web app needs it there). One place
    means the replay path cannot drift away from the live one.
    """
    prose, chart = split_chart(text)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=prose)],
        structured_content={"answer": prose, "figure": figure, "chart": chart,
                            "figure_grounding": figure_grounding})


def _refuse(text: str) -> types.CallToolResult:
    """A failed `ask`. `is_error` is what makes a client render this as a failed
    tool rather than as an answer, which matters most for the cases that LOOK
    like prose — 'no dataset loaded' read as an answer is a wrong answer."""
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                is_error=True)


def _bill(*, user_id: int, question: str, model_used: str, usage=None,
          result=None, cached: bool = False) -> None:
    """Write this call's `usage_log` row. Blocking; call it in a thread pool.

    Two shapes of turn bill through here. A refusal and a cache hit have no
    AgentResult — their only spend is the guard's own call, passed as `usage`.
    A full turn passes `result`, which has already had the guard's spend folded
    into it by the caller, exactly as the chat path folds it.
    """
    now = time.time()
    con = connect()
    try:
        if result is not None:
            record_usage(
                con, user_id=user_id, question=question,
                model_used=result.model_used, source="mcp",
                escalated=result.escalated, ok=result.error is None,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cached_prompt_tokens=result.cached_prompt_tokens,
                first_call_prompt_tokens=result.first_call_prompt_tokens,
                first_call_cached_prompt_tokens=result.first_call_cached_prompt_tokens,
                cost=effective_cost(result.cost, result.prompt_tokens,
                                    result.completion_tokens,
                                    cached_prompt_tokens=result.cached_prompt_tokens),
                cost_estimated=cost_is_estimated(result.cost),
                figure_grounding=result.figure_grounding,
                figure_derivation=result.figure_derivation,
                emit_mode=result.emit_mode, answer_leaked=result.leaked,
                table_grounding=result.table_grounding,
                table_cells_checked=result.table_cells_checked,
                table_cells_matched=result.table_cells_matched,
                exhaustion=("degraded" if result.exhaustion_degraded
                            else "answered" if result.exhausted else None),
                created_at=now)
        else:
            record_usage(
                con, user_id=user_id, question=question, model_used=model_used,
                source="mcp", cached=cached,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cached_prompt_tokens=usage.cached_prompt_tokens,
                cost=effective_cost(usage.cost, usage.prompt_tokens,
                                    usage.completion_tokens,
                                    cached_prompt_tokens=usage.cached_prompt_tokens),
                cost_estimated=cost_is_estimated(usage.cost),
                created_at=now)
        con.commit()
    finally:
        con.close()


async def run_ask(arguments: dict) -> types.CallToolResult:
    """One `ask` call, start to finish.

    Runs ON the event loop rather than in a worker thread, because the agent
    loop is an async generator that manages its own thread hops (app/llm.py
    dispatches every blocking tool call itself). Each blocking step HERE — the
    rate limiter, the dataset check, the cache, the lesson lookup, the billing
    write — takes its own trip to the pool.
    """
    caller = current_caller()
    if caller is None:
        # The gate admitted this request, so it knows who is calling; if that did
        # not reach us, the plumbing between them changed. Refuse rather than
        # guess — every step below spends money or reads data on somebody's
        # behalf, and there is no safe default for whose.
        log.error("ask: the admitted caller did not reach the tool handler")
        return _refuse("This deployment could not identify the caller.")

    question = str(arguments.get("question") or "").strip()
    if not question:
        return _refuse("Ask a question.")
    if len(question) > MAX_QUESTION_LEN:
        return _refuse(f"Question is too long (max {MAX_QUESTION_LEN:,} characters).")

    # 1) The SAME per-user budget the web chat charges, so one person's total
    #    spend is capped whichever door they came through.
    try:
        await run_in_threadpool(ratelimit.enforce_chat_rate_limit, caller.user_id)
    except HTTPException as e:
        return _refuse(str(e.detail))

    # 2) Nothing loaded means nothing to answer, and it costs no LLM call to say so.
    if not await run_in_threadpool(ipeds_years):
        return _refuse(NO_DATA)

    # 3) The topical guardrail, before any cache read or model work. This endpoint
    #    is reachable from the internet and spends money on every question it
    #    accepts, so the gate runs first — same call, same position, as chat.
    #    History is empty by construction: there is no conversation.
    verdict = await guard.classify(question, [])
    if not verdict.allowed:
        await run_in_threadpool(_bill, user_id=caller.user_id, question=question,
                                model_used="guard", usage=verdict.usage)
        # A refusal is an ANSWER, not a tool failure: the caller asked something
        # this assistant does not cover, and the text says so usefully.
        return _answer(guard.REFUSAL)

    # 4) The answer cache. A hit costs the guard call above and nothing more.
    #    Scoped to this user, like every other read of it.
    cached = await run_in_threadpool(skills.cache_lookup, question, caller.user_id)
    if cached:
        await run_in_threadpool(_bill, user_id=caller.user_id, question=question,
                                model_used="cache", usage=verdict.usage, cached=True)
        # No grounding status on a replay, the same call the chat path makes: the
        # stored prose was graded when it was produced, but re-grading it here
        # would move a measurement inside a replay path.
        return _answer(cached["answer_md"], cached.get("figure"))

    # 5) Learned lessons, as few-shot guidance.
    skills_block, skill_ids = await run_in_threadpool(
        skills.retrieve_skills_block, question)
    if skill_ids:
        await run_in_threadpool(skills.bump_hits, skill_ids)

    # 6) The agent. Consumed to completion — there is no stream to forward, so
    #    only the terminal `done` event's result and the answer text matter.
    result = None
    answer = ""
    error_text = ""
    async for ev in stream_agent(question, skills_block=skills_block):
        if ev["type"] == "done":
            result = ev["result"]
        elif ev["type"] == "answer":
            answer = ev["text"]
        elif ev["type"] == "error":
            error_text = ev["text"]

    if result is None:
        # The one path that yields an error and no `done`: no LLM provider is
        # configured. Nothing was spent, so nothing is billed. The data tools
        # keep working — they need no provider at all — which is why this is a
        # readable tool error rather than anything louder.
        return _refuse(error_text or "The agent produced no answer.")

    # Fold the guard's spend into the turn it gated, before the cost estimate
    # below reads the token counts. first_call_* is deliberately untouched: it
    # measures the AGENT's schema-prefix cache reuse, and the guard's prompt is
    # a different prefix.
    result.prompt_tokens += verdict.usage.prompt_tokens
    result.completion_tokens += verdict.usage.completion_tokens
    result.cached_prompt_tokens += verdict.usage.cached_prompt_tokens
    result.cost += verdict.usage.cost

    await run_in_threadpool(_bill, user_id=caller.user_id, question=question,
                            model_used=result.model_used, result=result)

    # 7) Cache a successful answer, on the same terms as chat — and WITH its
    #    result rows. The cache is shared with the web app, so a row stored
    #    without them would hand a later chat hit an answer it cannot ground a
    #    recited number against, which is the gap migration 31 exists to close.
    #
    #    Guarded, and NOT for the reason chat's own post-answer writes are. By
    #    the time chat caches, it has already streamed the answer to the client,
    #    so a throw here costs the tail of a stream the user has already read.
    #    Here the answer has NOT been returned yet: an unguarded throw would
    #    propagate to the handler, come back as a tool error, and lose an answer
    #    that was finished and already billed. A caching hiccup must cost the
    #    cache entry and nothing else.
    if result.error is None and answer and result.sql_log and result.clarify is None:
        try:
            await run_in_threadpool(
                skills.cache_store, question, result.sql_log[-1], answer,
                result.figure, result.suggestions,
                _results_for_storage(result.results),
                any(r.truncated for r in (result.results or [])),
                user_id=caller.user_id)
        except Exception:
            log.exception("ask: caching the answer failed")

    if result.error:
        return _refuse(result.error)
    return _answer(answer, result.figure, result.figure_grounding or None)
