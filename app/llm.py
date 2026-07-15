"""LLM orchestration: an async tool-calling loop against OpenRouter.

`stream_agent` yields progress events (tool calls, executed SQL, the final
answer) so the UI can render live status. `run_agent` drives it to completion
and returns an AgentResult (used by the eval harness).

The cheap default model handles most turns; if it keeps producing failing SQL,
the loop escalates to a stronger model for the remainder of the turn. Everything
is model-agnostic via OpenRouter's OpenAI-compatible API.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from app.config import get_settings
from app.prompt import build_system_prompt
from app.tools import registry
from app.tools.sql import QueryResult

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
    error: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


async def _chat(client: httpx.AsyncClient, model: str, messages: list[dict],
                tools: list[dict]) -> dict:
    s = get_settings()
    payload = {
        "model": model, "messages": messages, "tools": tools,
        "tool_choice": "auto", "temperature": s.llm_temperature,
    }
    headers = {
        "Authorization": f"Bearer {s.openrouter_api_key}",
        "HTTP-Referer": s.app_public_url, "X-Title": s.app_title,
    }
    r = await client.post(f"{s.openrouter_base_url}/chat/completions",
                          json=payload, headers=headers, timeout=120.0)
    r.raise_for_status()
    return r.json()


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
    if not s.openrouter_api_key:
        yield {"type": "error", "text": "OPENROUTER_API_KEY is not configured."}
        return

    registry.reset_last_result()
    tools = registry.tool_specs()
    messages: list[dict] = [{"role": "system", "content": build_system_prompt(skills_block)}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})

    res = AgentResult()
    model = s.model_default
    consecutive_fails = 0

    async with httpx.AsyncClient() as client:
        for i in range(s.llm_max_tool_iters):
            res.iterations = i + 1
            try:
                data = await _chat(client, model, messages, tools)
            except httpx.HTTPStatusError as e:
                res.error = f"LLM API error ({e.response.status_code}): {e.response.text[:300]}"
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

            msg = (data.get("choices") or [{}])[0].get("message") or {}
            tool_calls = msg.get("tool_calls") or []
            messages.append({
                "role": "assistant", "content": msg.get("content") or "",
                **({"tool_calls": tool_calls} if tool_calls else {}),
            })

            if not tool_calls:
                res.answer = msg.get("content") or ""
                res.model_used = model
                res.last_result = registry.LAST_RESULT["result"]
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
                result = registry.dispatch(name, args)
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

        res.error = "Reached max tool iterations without a final answer."
        res.model_used = model
        res.last_result = registry.LAST_RESULT["result"]
        yield {"type": "error", "text": res.error}
        yield {"type": "done", "result": res}


async def run_agent(question: str, *, history: list[dict] | None = None,
                    skills_block: str = "") -> AgentResult:
    """Drive stream_agent to completion and return the final AgentResult."""
    result = AgentResult(error="no result")
    async for ev in stream_agent(question, history=history, skills_block=skills_block):
        if ev["type"] == "done":
            result = ev["result"]
    return result
