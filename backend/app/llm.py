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
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from app import critic
from app.config import get_settings
from app.llmhttp import DEFAULT_TIMEOUT, chat_completion
from app.prompt import build_system_prompt
from app.tools import registry
from app.tools.sql import QueryResult

log = logging.getLogger("ipeds.llm")

_FAIL_MARKERS = ("SQL REJECTED", "SQL ERROR", "SQL TIMEOUT", "ERROR")


@dataclass
class AgentResult:
    answer: str = ""
    model_used: str = ""
    escalated: bool = False
    iterations: int = 0
    sql_log: list[str] = field(default_factory=list)
    last_result: QueryResult | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0  # summed OpenRouter cost (USD) across the turn's calls
    critic_revised: bool = False    # the critic flagged the draft and forced a revision
    critic_headline: str = ""       # the critic's finding, headline (candidate lesson title)
    critic_description: str = ""    # the critic's finding, description (candidate lesson body)
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

    # Per-request sink for the last run_sql result (no shared module state, so
    # concurrent turns can't clobber each other's data behind the answer).
    last_sql_result: dict = {"result": None}
    tools = registry.tool_specs()
    messages: list[dict] = [{"role": "system", "content": build_system_prompt(skills_block)}]
    if history:
        messages.extend(history)
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
                res.error = f"LLM request failed: {e}"
                yield {"type": "error", "text": res.error}
                yield {"type": "done", "result": res}
                return

            usage = data.get("usage") or {}
            res.prompt_tokens += usage.get("prompt_tokens", 0)
            res.completion_tokens += usage.get("completion_tokens", 0)
            res.cost += usage.get("cost") or 0

            msg = (data.get("choices") or [{}])[0].get("message") or {}
            tool_calls = msg.get("tool_calls") or []
            # OpenRouter-specific: no other provider returns `reasoning`, so the
            # thinking events just never fire there. See DEPLOY.md's model routing.
            reasoning = msg.get("reasoning")
            if reasoning:
                yield {"type": "thinking", "text": reasoning}
            messages.append({
                "role": "assistant", "content": msg.get("content") or "",
                **({"tool_calls": tool_calls} if tool_calls else {}),
            })

            if not tool_calls:
                answer = msg.get("content") or ""
                # Post-answer critic: once per turn, and only for answers built
                # from SQL (a plain refusal has nothing to sanity-check). If it
                # flags a likely error, feed the critique back for ONE revision
                # round instead of returning the suspect number.
                if s.critic_enabled and not critiqued and res.sql_log and answer.strip():
                    critiqued = True
                    crit = await critic.review(question, res.sql_log, answer)
                    res.prompt_tokens += crit.prompt_tokens
                    res.completion_tokens += crit.completion_tokens
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
                #  - re-queried AND the answer actually changed -> keep the new
                #    answer and mark the turn revised (so chat.py records the
                #    critic's finding as a lesson).
                #  - otherwise -> re-emit the clean pre-critique draft and leave
                #    critic_revised False. This covers a reviewer-directed rebuttal
                #    (no new run_sql), a re-query that merely confirmed the same
                #    number (a critic false alarm — no lesson should be stored),
                #    and an empty revision. So no meta-commentary leaks to the user
                #    and no spurious lesson is recorded. Trade-off: an interpretive
                #    fix the model makes WITHOUT re-querying isn't distinguishable
                #    from a rebuttal, so it falls back to the draft too; the
                #    hardened revision_instruction steers real corrections to
                #    re-run run_sql.
                if draft_answer:
                    requeried = len(res.sql_log) > sql_count_at_critique
                    changed = bool(answer.strip()) and answer.strip() != draft_answer.strip()
                    if requeried and changed:
                        res.critic_revised = True
                    else:
                        answer = draft_answer
                res.answer = answer
                res.model_used = model
                res.last_result = last_sql_result["result"]
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
            res.cost += usage.get("cost") or 0
            final = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except httpx.HTTPError:
            final = ""

        res.model_used = model
        res.last_result = last_sql_result["result"]
        if final.strip():
            res.answer = final
            yield {"type": "answer", "text": final}
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
