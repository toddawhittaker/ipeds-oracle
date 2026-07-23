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

from typing import Any

import httpx

DEFAULT_TIMEOUT = 120.0  # full agent turns (tool-calling rounds)
PROBE_TIMEOUT = 30.0     # cheap guard / critic classification calls


def cached_tokens(usage: dict) -> int:
    """Prompt tokens the provider served from ITS OWN prompt cache, for this
    response. OpenRouter normalizes to `prompt_tokens_details.cached_tokens`;
    DeepSeek-native reports `prompt_cache_hit_tokens`. Returns 0 on a provider
    that reports neither — so the metric degrades to "no reuse observed" rather
    than raising. (This is the LLM provider's prefix cache — distinct from our
    own semantic answer cache in query_cache.)"""
    details = usage.get("prompt_tokens_details") or {}
    return details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0


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
    "function":{"name":"emit_answer"}}`. NOTE (tested 2026-07-23): forcing a
    specific function (or `"required"`) is REJECTED by DeepSeek/Kimi while
    reasoning is on — pair it with `reasoning={"enabled": False}`.

    `reasoning` (OpenRouter's unified param) is omitted by default → the
    provider's own default (thinking ON for DeepSeek v4). Pass
    `{"enabled": False}` to turn thinking off for this call."""
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
