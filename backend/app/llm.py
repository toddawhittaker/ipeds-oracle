"""LLM orchestration: an async tool-calling loop against an OpenAI-compatible
LLM provider (OpenRouter by default; see LLM_BASE_URL).

`stream_agent` yields progress events (tool calls, executed SQL, the final
answer) so the UI can render live status. `run_agent` drives it to completion
and returns an AgentResult (used by the eval harness).

The cheap default model handles most turns; if it keeps producing failing SQL,
the loop escalates to a stronger model for the remainder of the turn. Everything
is model-agnostic via the OpenAI-compatible /chat/completions API.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from app import critic, grounding
from app.config import get_settings
from app.llmhttp import DEFAULT_TIMEOUT, cached_tokens, chat_completion
from app.prompt import build_system_prompt
from app.tools import registry
from app.tools.sql import QueryResult

log = logging.getLogger("ipeds.llm")

_FAIL_MARKERS = ("SQL REJECTED", "SQL ERROR", "SQL TIMEOUT", "ERROR")

# A user-facing answer NEVER addresses "the reviewer" or "this review" — that
# phrasing only appears when the model's critique-round rebuttal leaks into the
# answer (see backend/tests/test_critic.py + memory critic-revision-leak). Match
# reviewer-referential meta only, so a genuine correction — which changes the
# number and never speaks to a reviewer — is never suppressed.
_REVIEW_LEAK_RE = re.compile(r"\breviewer\b|\b(?:the|this|automated)\s+review\b",
                             re.IGNORECASE)


# Injected just before the question on FOLLOW-UP turns only (see stream_agent).
# Deliberately a POINTER to the rules, not a restatement of them: step 6's
# (i)/(ii) body is ~35 lines, and duplicating it here would both bloat every
# follow-up request and risk drifting out of sync with prompt.INSTRUCTIONS, which
# stays the single source of truth.
_TURN_REMINDER = (
    "Reminder — this is a FOLLOW-UP turn, and every rule in your instructions "
    "still applies to it in full. A follow-up is a complete answer, not a chat "
    "aside:\n"
    "- LEAD with the ```figure fence (step 6). It is REQUIRED unless your answer "
    "contains no number at all, you cannot answer, or you are asking a "
    "clarifying question. That a number appeared earlier in this conversation, "
    "or appears in your own table below, is NOT a reason to omit it.\n"
    "- END with the ```followups fence (step 7).\n"
    "Answer the question below."
)


def _leaks_review_meta(text: str) -> bool:
    """True if `text` references the critique conversation (reviewer/review) —
    the tell of a leaked rebuttal that must not reach the user."""
    return bool(_REVIEW_LEAK_RE.search(text or ""))


def _stamp_grounding(res: AgentResult, raw_answer: str = "") -> None:
    """Record whether the figure's number is reproducible from the turn's own
    query results. Observe-only: it never edits the figure or the answer.

    Deliberately not gated on a setting — it is pure local arithmetic over data
    already in memory (no DB, no LLM, no network), so there is nothing to switch
    off, and a status missing from usage_log would silently bias the very rate
    this exists to measure.

    `raw_answer` is the answer BEFORE the fences were stripped, and it splits an
    otherwise ambiguous outcome: `_extract_figure` returns None both when the
    model emitted no figure at all AND when it emitted one whose JSON didn't
    parse. Those look identical downstream but call for opposite fixes — a
    prompt problem vs. a format problem — so a fence that was present but
    unusable is recorded as `malformed`, not `no_figure`."""
    if res.figure is None and raw_answer and _FIGURE_BLOCK_RE.search(raw_answer):
        res.figure_grounding = grounding.MALFORMED
        res.figure_derivation = ""
        return
    check = grounding.check_figure(res.figure, res.results)
    res.figure_grounding = check.status
    res.figure_derivation = check.derivation.describe() if check.derivation else ""


def effective_cost(reported_cost: float, prompt_tokens: int,
                   completion_tokens: int, s=None) -> float:
    """The USD cost to record for a turn. Prefer the provider-reported cost
    (OpenRouter's usage.cost); when that's absent/zero, fall back to an estimate
    from the admin-configured per-Mtok list prices — so a provider that doesn't
    report cost still yields a spend figure instead of 0. Both prices default to
    0, in which case there's no estimate and the reported (0) cost stands.

    The estimate prices EVERY prompt token at the input rate — it does not
    discount cached-prefix tokens (we can't know the provider's cached rate), so
    it slightly over-states spend on cache-heavy traffic. It's a stand-in for a
    real per-request bill, not a substitute for one."""
    if reported_cost and reported_cost > 0:
        return reported_cost
    s = s or get_settings()
    return (prompt_tokens * s.llm_input_cost_per_mtok
            + completion_tokens * s.llm_output_cost_per_mtok) / 1_000_000


# The model emits an OPTIONAL ```figure {…}``` fence when a single headline number
# answers the question (prompt INSTRUCTIONS step 6, modeled on the chart fence).
# We parse + strip it server-side so the frontend gets structured data, never raw
# JSON in the prose — mirrors frontend/src/figure.js normalizeFigure.
# The figure comes as a ```figure fenced block — but some models emit an HTML
# <figure>{json}</figure> tag instead; accept BOTH forms (and strip both), so the
# hero statistic is captured either way and raw JSON never reaches the user.
_FIGURE_BLOCK_RE = re.compile(
    r"```figure[ \t]*\r?\n(.*?)```"
    r"|<figure[^>]*>\s*(\{.*?\})\s*</figure>",
    re.DOTALL,
)
_FIGURE_KEYS = ("value", "unit", "label", "source")


def _extract_figure(answer: str) -> tuple[str, dict | None]:
    """Pull the figure out of `answer` — a ```figure fence OR an HTML <figure> tag.
    Returns (answer_without_any_figure_block, figure_or_None). ALWAYS strips every
    such block (so raw JSON never reaches the user, even on a parse failure);
    returns a figure dict only when the first is valid JSON with value AND label."""
    matches = list(_FIGURE_BLOCK_RE.finditer(answer or ""))
    if not matches:
        return answer, None
    clean = _FIGURE_BLOCK_RE.sub("", answer).strip()
    m = matches[0]
    raw = m.group(1) if m.group(1) is not None else m.group(2)
    try:
        data = json.loads((raw or "").strip())
    except (json.JSONDecodeError, ValueError):
        return clean, None
    if not isinstance(data, dict):
        return clean, None
    out = {}
    for k in _FIGURE_KEYS:
        v = data.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            out[k] = s
    return clean, (out if out.get("value") and out.get("label") else None)


# The model MAY end with a ```followups fence — a JSON array of 2-3 drill-down
# questions (prompt INSTRUCTIONS step 7). Parsed + stripped server-side like the
# figure, surfaced as clickable chips.
_FOLLOWUPS_FENCE_RE = re.compile(r"```followups[ \t]*\r?\n(.*?)```", re.DOTALL)


def _extract_suggestions(answer: str) -> tuple[str, list | None]:
    """Pull a ```followups fence (a JSON array of drill-down questions) out of the
    answer. ALWAYS strips every followups fence; returns up to 3 non-empty, trimmed
    question strings, or None (no fence / bad JSON / not a list of strings)."""
    matches = _FOLLOWUPS_FENCE_RE.findall(answer or "")
    if not matches:
        return answer, None
    clean = _FOLLOWUPS_FENCE_RE.sub("", answer).strip()
    try:
        data = json.loads(matches[0].strip())
    except (json.JSONDecodeError, ValueError):
        return clean, None
    if not isinstance(data, list):
        return clean, None
    out = [str(q).strip() for q in data if str(q).strip()][:3]
    return clean, (out or None)


# The model MAY, instead of answering, emit a ```clarify {"question":"...",
# "options":[...]}``` fence when the request is MATERIALLY ambiguous (prompt
# INSTRUCTIONS' leading "Before you answer" step) — one short clarifying question
# plus 2-4 short answer-phrase chips. Parsed + stripped server-side like the
# figure/followups fences, so raw JSON never reaches the user.
_CLARIFY_FENCE_RE = re.compile(r"```clarify[ \t]*\r?\n(.*?)```", re.DOTALL)
_MAX_CLARIFY_OPTIONS = 4
# Length caps mirror the _MAX_* precedent in critic.py/feedback.py: the model is
# asked for "one line" / "short phrases", but nothing stops a runaway or
# adversarial value from ignoring that, and an unbounded string would flow
# straight into a rendered chip label. Plain-slice truncation, same as
# critic._MAX_ANSWER_CHARS / feedback._MAX_FEEDBACK_CHARS.
_MAX_CLARIFY_QUESTION_CHARS = 200
_MAX_CLARIFY_OPTION_CHARS = 80


def _extract_clarify(answer: str) -> tuple[str, dict | None]:
    """Pull a ```clarify fence (a JSON object {question, options[]}) out of the
    answer. ALWAYS strips every clarify fence first (so raw JSON never reaches the
    user, even on a parse failure); returns a {question, options} dict only when
    the first fence is valid JSON with a non-empty question and >=1 non-empty
    option (deduped, capped at 4 options, and length-capped per string)."""
    matches = _CLARIFY_FENCE_RE.findall(answer or "")
    if not matches:
        return answer, None
    clean = _CLARIFY_FENCE_RE.sub("", answer).strip()
    try:
        data = json.loads(matches[0].strip())
    except (json.JSONDecodeError, ValueError):
        return clean, None
    if not isinstance(data, dict):
        return clean, None
    question = str(data.get("question") or "").strip()[:_MAX_CLARIFY_QUESTION_CHARS].strip()
    if not question:
        return clean, None
    seen = set()
    options = []
    for o in data.get("options") or []:
        s = str(o).strip()[:_MAX_CLARIFY_OPTION_CHARS].strip()
        if s and s not in seen:
            seen.add(s)
            options.append(s)
        if len(options) >= _MAX_CLARIFY_OPTIONS:
            break
    if not options:
        return clean, None
    return clean, {"question": question, "options": options}


@dataclass
class AgentResult:
    answer: str = ""
    model_used: str = ""
    escalated: bool = False
    iterations: int = 0
    sql_log: list[str] = field(default_factory=list)
    last_result: QueryResult | None = None
    # EVERY run_sql result of the turn, in call order. `last_result` alone was
    # overwritten per call, so a multi-query brief discarded the very result a
    # headline figure was derived from — leaving the server unable to check it.
    # app/grounding.py consumes this.
    results: list[QueryResult] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0  # prompt tokens the provider served from its prefix cache
    # The FIRST LLM call of the turn only — its prompt is schema-prefix + prior-turn
    # history + question, with NO in-turn tool rounds accumulated yet. So its cache
    # rate isolates cross-question SCHEMA-PREFIX reuse, distinct from the blended
    # `cached_prompt_tokens` above (which later tool rounds inflate by re-caching the
    # growing in-turn conversation). See build_system_prompt's cache-contract note.
    first_call_prompt_tokens: int = 0
    first_call_cached_prompt_tokens: int = 0
    cost: float = 0.0  # summed OpenRouter cost (USD) across the turn's calls
    critic_revised: bool = False    # the critic flagged the draft and forced a revision
    critic_headline: str = ""       # the critic's finding, headline (candidate lesson title)
    critic_description: str = ""    # the critic's finding, description (candidate lesson body)
    figure: dict | None = None      # structured hero statistic from the answer's figure fence
    # Whether the figure's number could be reproduced from the retained results
    # (app/grounding.py). OBSERVE-ONLY: recorded on usage_log, surfaced on
    # Admin -> Usage, and blocks/alters nothing. "" means never checked.
    figure_grounding: str = ""
    figure_derivation: str = ""     # the derivation that matched, e.g. "sum(q2.awards)"
    suggestions: list | None = None  # drill-down questions from the followups fence
    clarify: dict | None = None     # {question, options[]} from a disambiguation fence
    error: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


async def _chat(client: httpx.AsyncClient, model: str, messages: list[dict],
                tools: list[dict] | None = None) -> dict:
    s = get_settings()
    return await chat_completion(client, model=model, messages=messages,
                                 temperature=s.llm_temperature, tools=tools,
                                 settings=s, timeout=DEFAULT_TIMEOUT)


async def stream_agent(question: str, *, history: list[dict] | None = None,
                       skills_block: str = "") -> AsyncIterator[dict]:
    """Yield event dicts:
      {"type":"status", "text":...}         human-readable progress
      {"type":"sql", "sql":...}             a query about to run
      {"type":"tool", "name":..., "ok":...} a tool finished
      {"type":"answer", "text":...}         final markdown answer
      {"type":"done", "result": AgentResult}
      {"type":"error", "text":...}
    """
    s = get_settings()
    if not s.llm_api_key:
        yield {"type": "error", "text": "The LLM provider is not configured."}
        return

    # Per-request sink for run_sql results (no shared module state, so concurrent
    # turns can't clobber each other's data behind the answer): "result" is the
    # last one, "results" accumulates them all. See registry._tool_run_sql.
    last_sql_result: dict = {"result": None, "results": []}
    tools = registry.tool_specs()
    # Message ORDER preserves the provider prompt cache: the system prompt (the big
    # static schema prefix — see build_system_prompt) goes FIRST so it stays the
    # cacheable prefix, then the per-request-dynamic parts (history, the question)
    # follow. Keep it that way — never prepend anything per-request ahead of the
    # system prompt, or the cached prefix collapses and every schema token bills at
    # full price.
    messages: list[dict] = [{"role": "system", "content": build_system_prompt(skills_block)}]
    if history:
        messages.extend(history)
        # RECENCY, not wording. Measured over a live 10-turn conversation, the
        # figure appeared on turns 1-2 and then stopped; followups held to turn 7
        # and then stopped. Turn 6 asked "how many nursing bachelor's degrees
        # were awarded nationally" — structurally identical to turn 1, the
        # canonical case step 6(i) most explicitly mandates — and emitted
        # nothing. Same question shape, deeper position, opposite outcome: the
        # failure tracks conversation DEPTH, not question type.
        #
        # The cause is structural. The system prompt must come FIRST to stay the
        # cacheable prefix (see build_system_prompt's cache contract), so by turn
        # 10 its rules sit behind ten turns of conversation. Rewording buried
        # text does not make it less buried — that was tried, and follow-up
        # emission moved only 0/9 -> 1/9. This puts a short pointer back to those
        # rules next to the question being answered.
        #
        # Placement is load-bearing twice over: AFTER the prefix (ahead of it
        # would collapse cache reuse and bill every schema token at full price),
        # and BEFORE the question (so the rules are the last thing read before
        # the task). Follow-ups only — first turns already comply, and the rules
        # are already adjacent there. Built per request and never persisted, so
        # it cannot accumulate in history.
        messages.append({"role": "system", "content": _TURN_REMINDER})
    messages.append({"role": "user", "content": question})

    res = AgentResult()
    model = s.model_default
    consecutive_fails = 0
    critiqued = False  # the post-answer critic runs at most once per turn
    draft_answer = ""          # the clean pre-critique draft, re-emitted when the
    sql_count_at_critique = 0  # revision round argues instead of re-querying

    async with httpx.AsyncClient() as client:
        for i in range(s.llm_max_tool_iters):
            res.iterations = i + 1
            try:
                data = await _chat(client, model, messages, tools)
            except httpx.HTTPStatusError as e:
                # The upstream body can carry provider/proxy detail; log it
                # server-side but return only the status to the client — never
                # reflect an unbounded upstream body into the UI or logs stream.
                log.warning("LLM API error %s: %s", e.response.status_code,
                            e.response.text[:300])
                res.error = f"LLM API error ({e.response.status_code})"
                yield {"type": "error", "text": res.error}
                yield {"type": "done", "result": res}
                return
            except httpx.HTTPError as e:
                # Same policy as the status branch above: a transport error's
                # string can carry connection/host detail (the provider URL, a
                # proxy). Log it server-side, but return only a generic message —
                # never reflect the exception text into the UI.
                log.warning("LLM request failed: %s", e)
                res.error = "LLM request failed."
                yield {"type": "error", "text": res.error}
                yield {"type": "done", "result": res}
                return

            usage = data.get("usage") or {}
            res.prompt_tokens += usage.get("prompt_tokens", 0)
            res.completion_tokens += usage.get("completion_tokens", 0)
            res.cached_prompt_tokens += cached_tokens(usage)
            if i == 0:  # first call only — the clean schema-prefix cache signal
                res.first_call_prompt_tokens = usage.get("prompt_tokens", 0)
                res.first_call_cached_prompt_tokens = cached_tokens(usage)
            res.cost += usage.get("cost") or 0

            msg = (data.get("choices") or [{}])[0].get("message") or {}
            tool_calls = msg.get("tool_calls") or []
            # OpenRouter-specific: no other provider returns `reasoning`, so the
            # thinking events just never fire on other providers.
            reasoning = msg.get("reasoning")
            if reasoning:
                yield {"type": "thinking", "text": reasoning}
            messages.append({
                "role": "assistant", "content": msg.get("content") or "",
                **({"tool_calls": tool_calls} if tool_calls else {}),
            })

            if not tool_calls:
                answer = msg.get("content") or ""
                # Disambiguation: the model may ask ONE clarifying question instead
                # of answering (prompt INSTRUCTIONS' leading "Before you answer"
                # step). Extract it FIRST, ahead of the critic — a clarify turn has
                # no data claim to sanity-check, and must never carry a figure or
                # followups (those belong to a real answer). Terminates the turn.
                clarify_answer, clarify = _extract_clarify(answer)
                if clarify:
                    res.answer = clarify_answer
                    res.clarify = clarify
                    res.model_used = model
                    res.last_result = last_sql_result["result"]
                    res.results = last_sql_result["results"]
                    yield {"type": "clarify", "clarify": clarify}
                    yield {"type": "answer", "text": res.answer}
                    yield {"type": "done", "result": res}
                    return
                # Post-answer critic: once per turn, and only for answers built
                # from SQL (a plain refusal has nothing to sanity-check). If it
                # flags a likely error, feed the critique back for ONE revision
                # round instead of returning the suspect number. A clarify fence
                # (handled above) always skips this — there is no data claim yet.
                if s.critic_enabled and not critiqued and res.sql_log and answer.strip():
                    critiqued = True
                    crit = await critic.review(question, res.sql_log, answer,
                                               last_sql_result["results"])
                    res.prompt_tokens += crit.prompt_tokens
                    res.completion_tokens += crit.completion_tokens
                    res.cached_prompt_tokens += crit.cached_prompt_tokens
                    res.cost += crit.cost
                    if not crit.ok:
                        # Capture the clean draft + the SQL count NOW, before the
                        # revision round, so we can tell a real correction (it runs
                        # new SQL) from a rebuttal (it doesn't) once it returns.
                        res.critic_headline = crit.headline
                        res.critic_description = crit.description
                        draft_answer = answer
                        sql_count_at_critique = len(res.sql_log)
                        yield {"type": "status", "text": "Double-checking the result…"}
                        messages.append({"role": "user", "content": critic.revision_instruction(
                            crit.headline, crit.description)})
                        continue
                # If a revision round ran (draft_answer is set), decide what to
                # emit. A genuine correction both re-queries AND lands on a
                # different answer; anything else is treated as no correction:
                #  - re-queried AND the answer actually changed AND it carries no
                #    reviewer-directed meta -> keep the new answer and mark the
                #    turn revised (so chat.py records the critic's finding as a
                #    lesson).
                #  - otherwise -> re-emit the clean pre-critique draft and leave
                #    critic_revised False. This covers a reviewer-directed rebuttal
                #    (no new run_sql), a re-query that merely confirmed the same
                #    number (a critic false alarm — no lesson should be stored),
                #    an empty revision, AND a rebuttal that DID re-query to
                #    confirm but leaked "the reviewer"/"this review" meta into its
                #    prose (the observed regression: a confirm dressed up as a
                #    correction — same number, different text — which requeried+
                #    changed alone can't tell from a real fix). So no
                #    meta-commentary leaks to the user and no spurious lesson is
                #    recorded. Trade-off: an interpretive fix the model makes
                #    WITHOUT re-querying isn't distinguishable from a rebuttal, so
                #    it falls back to the draft too; the hardened
                #    revision_instruction steers real corrections to re-run run_sql.
                if draft_answer:
                    requeried = len(res.sql_log) > sql_count_at_critique
                    changed = bool(answer.strip()) and answer.strip() != draft_answer.strip()
                    if requeried and changed and not _leaks_review_meta(answer):
                        res.critic_revised = True
                    else:
                        answer = draft_answer
                # Extract the structured blocks from the FINAL answer (after the
                # critic revert settles it), so they always match the winning prose.
                raw_answer = answer
                answer, res.figure = _extract_figure(answer)
                answer, res.suggestions = _extract_suggestions(answer)
                res.answer = answer
                res.model_used = model
                res.last_result = last_sql_result["result"]
                res.results = last_sql_result["results"]
                _stamp_grounding(res, raw_answer)
                if res.figure:
                    yield {"type": "figure", "figure": res.figure}
                if res.suggestions:
                    yield {"type": "suggestions", "suggestions": res.suggestions}
                yield {"type": "answer", "text": res.answer}
                yield {"type": "done", "result": res}
                return

            turn_had_fail = False
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                args = fn.get("arguments", "{}")
                if name == "run_sql":
                    try:
                        parsed = json.loads(args) if isinstance(args, str) else args
                        sql = (parsed or {}).get("sql", "")
                        if sql:
                            res.sql_log.append(sql)
                            yield {"type": "sql", "sql": sql}
                    except json.JSONDecodeError:
                        pass
                else:
                    yield {"type": "status", "text": f"Looking up {name.replace('_', ' ')}…"}
                result = registry.dispatch(name, args, result_sink=last_sql_result)
                ok = not any(result.startswith(m) for m in _FAIL_MARKERS)
                turn_had_fail = turn_had_fail or not ok
                yield {"type": "tool", "name": name, "ok": ok}
                messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "content": result})

            if turn_had_fail:
                consecutive_fails += 1
                if (consecutive_fails >= 2 and not res.escalated
                        and s.model_escalation and s.model_escalation != model):
                    model = s.model_escalation
                    res.escalated = True
                    yield {"type": "status", "text": "Escalating to a stronger model…"}
            else:
                consecutive_fails = 0

        # Tool budget exhausted. Rather than discard the data already gathered,
        # make one final pass with tools disabled so the model MUST answer from
        # the query results it has collected.
        yield {"type": "status", "text": "Summarizing results…"}
        messages.append({"role": "user", "content":
            "You've reached the tool-call limit — do NOT call any more tools. "
            "Give your best final answer now using the query results above, "
            "noting briefly if anything is incomplete."})
        try:
            data = await _chat(client, model, messages, tools=None)
            usage = data.get("usage") or {}
            res.prompt_tokens += usage.get("prompt_tokens", 0)
            res.completion_tokens += usage.get("completion_tokens", 0)
            res.cached_prompt_tokens += cached_tokens(usage)
            res.cost += usage.get("cost") or 0
            final = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except httpx.HTTPError:
            final = ""

        res.model_used = model
        res.last_result = last_sql_result["result"]
        res.results = last_sql_result["results"]
        if final.strip():
            final, clarify = _extract_clarify(final)
            if clarify:
                res.answer = final
                res.clarify = clarify
                yield {"type": "clarify", "clarify": clarify}
                yield {"type": "answer", "text": res.answer}
                yield {"type": "done", "result": res}
                return
            raw_final = final
            final, res.figure = _extract_figure(final)
            final, res.suggestions = _extract_suggestions(final)
            res.answer = final
            _stamp_grounding(res, raw_final)
            if res.figure:
                yield {"type": "figure", "figure": res.figure}
            if res.suggestions:
                yield {"type": "suggestions", "suggestions": res.suggestions}
            yield {"type": "answer", "text": res.answer}
            yield {"type": "done", "result": res}
            return

        res.error = "Reached max tool iterations without a final answer."
        yield {"type": "error", "text": res.error}
        yield {"type": "done", "result": res}


async def generate_title(question: str, answer: str) -> str:
    """Ask the cheap model for a short conversation title. Returns "" on any
    failure so titling never blocks or breaks a chat turn."""
    s = get_settings()
    if not s.llm_api_key:
        return ""
    prompt = [
        {"role": "system", "content":
            "You write a concise 3–6 word title for a chat about U.S. college "
            "data. Reply with ONLY the title — no quotes, no trailing period."},
        {"role": "user", "content":
            f"Question: {question}\n\nAnswer: {answer[:500]}\n\nTitle:"},
    ]
    try:
        async with httpx.AsyncClient() as client:
            data = await _chat(client, s.model_default, prompt, tools=None)
    except httpx.HTTPError:
        return ""
    title = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return title.strip().strip('"').strip().rstrip(".")[:80]


async def run_agent(question: str, *, history: list[dict] | None = None,
                    skills_block: str = "") -> AgentResult:
    """Drive stream_agent to completion and return the final AgentResult."""
    result = AgentResult(error="no result")
    async for ev in stream_agent(question, history=history, skills_block=skills_block):
        if ev["type"] == "done":
            result = ev["result"]
    return result
