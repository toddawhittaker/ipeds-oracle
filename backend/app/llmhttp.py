"""Shared LLM transport: the provider-neutral OpenAI-compatible POST call
used by app/llm.py (the agent loop), app/guard.py (topical gate), and
app/critic.py (post-answer review).

Contract: the CALLER owns the httpx client and the settings object; this
module owns only the wire protocol (URL, headers, JSON payload). It catches
NOTHING — `raise_for_status()`/transport errors propagate so fail-open
semantics stay entirely in the caller (guard/critic fail open; the agent loop
surfaces the error to the user). `client` and `settings` are never created or
fetched here, so tests can substitute a fake transport / fake settings with
zero risk of a real, billed network call.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_TIMEOUT = 120.0  # full agent turns (tool-calling rounds)
PROBE_TIMEOUT = 30.0     # cheap guard / critic classification calls

# What a call through `chat_completion` can raise, as ONE list.
#
# `ValueError` is not decoration: it covers a **200 whose body isn't JSON** — an
# endpoint fronted by a proxy, captive portal, or gateway answering with an HTML
# error page. `Response.json()` raises `json.JSONDecodeError`, a ValueError, NOT
# an `httpx.HTTPError`, so an `except httpx.HTTPError` does not see it.
#
# This existed as a hand-copied tuple in five places and was MISSING from the
# five that matter most — the agent loop's own call sites. There, an escaping
# ValueError killed the SSE generator mid-response: no terminal `done`, so
# `_persist` never ran (the turn's answer AND its spend both lost), the new
# conversation was reversed by `_delete_if_empty`, an unhandled traceback landed
# in logs.db, and the user got a blank assistant bubble that never resolved.
#
# One name so the sixth call site cannot get it wrong.
CHAT_ERRORS = (httpx.HTTPError, ValueError)


def cached_tokens(usage: dict) -> int:
    """Prompt tokens the provider served from ITS OWN prompt cache, for this
    response. OpenRouter normalizes to `prompt_tokens_details.cached_tokens`;
    some providers report `prompt_cache_hit_tokens` natively instead. Both
    shapes are read. Returns 0 on a provider
    that reports neither — so the metric degrades to "no reuse observed" rather
    than raising. (This is the LLM provider's prefix cache — distinct from our
    own semantic answer cache in query_cache.)"""
    details = usage.get("prompt_tokens_details") or {}
    return details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0


@dataclass
class Usage:
    """What one LLM call cost, in the four numbers usage_log records.

    Every probe in the app (guard, critic, figure-retry, title, feedback) needs
    the same extraction off a chat-completions response, and it was written out
    per-probe -- so a provider adding a key, or a probe forgetting `cost`, drifted
    silently. `from_response` is the single definition; the carrier is optional
    (critic.Critique and llm._FigureRetry keep their own flat fields and just
    populate them from here, which is what makes adopting this behaviour-neutral).

    Lives beside `cached_tokens` because this module owns the wire format -- it is
    the only place that knows a provider might say `prompt_cache_hit_tokens`
    instead of `prompt_tokens_details.cached_tokens`."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0
    cost: float = 0.0

    @classmethod
    def from_response(cls, data: dict) -> Usage:
        """Read the usage block off a chat-completions response. A response that
        carries none (an error shape, or a provider that omits it) yields all
        zeros rather than raising -- every caller is on a fail-open path."""
        usage = (data or {}).get("usage") or {}
        return cls(prompt_tokens=usage.get("prompt_tokens", 0),
                   completion_tokens=usage.get("completion_tokens", 0),
                   cached_prompt_tokens=cached_tokens(usage),
                   cost=usage.get("cost") or 0.0)


def provider_headers(s: Any) -> dict[str, str]:
    """Build the request headers from a settings object: bearer auth plus the
    optional attribution headers (HTTP-Referer/X-Title), each omitted when its
    source setting is empty."""
    headers = {"Authorization": f"Bearer {s.llm_api_key}"}
    if s.app_public_url:
        headers["HTTP-Referer"] = s.app_public_url
    if s.llm_app_title:
        headers["X-Title"] = s.llm_app_title
    return headers


async def chat_completion(client: httpx.AsyncClient, *, model: str, messages: list[dict],
                          temperature: float, settings: Any,
                          tools: list[dict] | None = None,
                          tool_choice: str | dict | None = None,
                          reasoning: dict | None = None,
                          timeout: float = DEFAULT_TIMEOUT) -> dict:
    """POST a /chat/completions request on the caller-supplied client and
    return the parsed JSON body. Raises on any transport or HTTP-status error
    — callers decide how to handle failure.

    `tool_choice` defaults to `"auto"` when tools are present (the model
    decides); pass an explicit value to FORCE a tool — e.g. `{"type":"function",
    "function":{"name":"emit_answer"}}`. NOTE (tested 2026-07-23): several
    reasoning models REJECT a forced specific function (or `"required"`) while
    thinking is on, with a 400 — pair it with `reasoning={"enabled": False}`.

    `reasoning` (OpenRouter's unified param) is omitted by default → whatever
    the provider does on its own (thinking is ON by default on most reasoning
    models). Pass `{"enabled": False}` to turn thinking off for this call."""
    payload: dict = {"model": model, "messages": messages, "temperature": temperature}
    # Omitting tools entirely (rather than tool_choice="none") forces a plain
    # text answer more portably across OpenAI-compatible providers — used for
    # the agent loop's final synthesis pass.
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"
    if reasoning is not None:
        payload["reasoning"] = reasoning
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    r = await client.post(url, json=payload, headers=provider_headers(settings),
                          timeout=timeout)
    r.raise_for_status()
    return r.json()
