"""Shared LLM transport contract (backend/app/llmhttp.py).

This is the RED-phase spec for the provider-neutral transport helper that
unifies the copy-pasted POST logic currently in backend/app/llm.py's `_chat`,
backend/app/guard.py's `classify`, and backend/app/critic.py's `review`.

Design contract asserted here:
  - `chat_completion(client, *, model, messages, temperature, tools=None,
    timeout=DEFAULT_TIMEOUT, settings=None)` builds the URL + headers + JSON
    payload, POSTs on the CALLER-SUPPLIED client, calls `raise_for_status()`,
    and returns `.json()` — it catches NOTHING (fail-open belongs to the
    caller, not the transport).
  - `provider_headers(s)` builds the header dict from a settings object.
  - The base URL is never hardcoded to openrouter.ai anywhere — a custom
    LLM_BASE_URL must be honored (the provider-neutrality regression test).
  - `client` is REQUIRED (never created inside the helper) so tests can
    substitute a fake transport with zero risk of a real, billed network call.
  - `settings` is passed BY THE CALLER (never fetched via get_settings()
    inside the helper), so a test that patches a module's own get_settings
    still controls what the helper sees.

No API key, no app.config import needed — this module tests backend/app/llmhttp.py in
isolation with fully local fake settings objects and a recording fake client.
"""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app import llmhttp  # noqa: E402

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")
    except Exception as e:  # noqa: BLE001 -- surface import/attr errors as failures too
        FAILURES.append(name)
        print(f"  ✗ {name}: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _settings(**overrides):
    base = dict(
        llm_api_key="test-key-123",
        llm_base_url="https://openrouter.ai/api/v1",
        app_public_url="http://localhost:8000",
        llm_app_title="IPEDS Query",
        llm_temperature=0.0,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _RecordingClient:
    """Records every POST call's url/json/headers/timeout and returns (or
    raises) one canned item. The helper must call `.post()` directly on this
    object — never wrap it in its own `async with httpx.AsyncClient()`."""

    def __init__(self, item):
        self._item = item
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers,
                           "timeout": timeout})
        if isinstance(self._item, BaseException):
            raise self._item
        return self._item

    @property
    def last(self):
        return self.calls[-1]


def _json_response(data, status=200):
    return httpx.Response(status, json=data,
                          request=httpx.Request("POST", "http://x/chat/completions"))


def _run(coro):
    return asyncio.run(coro)


_OK_BODY = {"choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


# ---------------------------------------------------------------------------
# 1. URL construction — no double slash on a trailing-slash base URL.
# ---------------------------------------------------------------------------

def test_url_is_base_plus_chat_completions():
    client = _RecordingClient(_json_response(_OK_BODY))
    s = _settings(llm_base_url="https://openrouter.ai/api/v1")
    _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=0.0,
                                 settings=s))
    assert client.last["url"] == "https://openrouter.ai/api/v1/chat/completions", client.last


def test_trailing_slash_in_base_url_does_not_double_slash():
    client = _RecordingClient(_json_response(_OK_BODY))
    s = _settings(llm_base_url="https://openrouter.ai/api/v1/")
    _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=0.0,
                                 settings=s))
    url = client.last["url"]
    assert url == "https://openrouter.ai/api/v1/chat/completions", url
    assert "//chat/completions" not in url.split("://", 1)[1], url


# ---------------------------------------------------------------------------
# 2/3/4. Headers — Authorization, attribution headers, omission when empty.
# ---------------------------------------------------------------------------

def test_authorization_header_bears_the_api_key():
    client = _RecordingClient(_json_response(_OK_BODY))
    s = _settings(llm_api_key="sk-super-secret")
    _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=0.0,
                                 settings=s))
    assert client.last["headers"]["Authorization"] == "Bearer sk-super-secret", \
        client.last["headers"]


def test_attribution_headers_map_to_public_url_and_app_title():
    client = _RecordingClient(_json_response(_OK_BODY))
    s = _settings(app_public_url="https://ipeds.example.edu", llm_app_title="My IPEDS App")
    _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=0.0,
                                 settings=s))
    h = client.last["headers"]
    assert h["HTTP-Referer"] == "https://ipeds.example.edu", h
    assert h["X-Title"] == "My IPEDS App", h


def test_attribution_headers_omitted_when_public_url_empty():
    client = _RecordingClient(_json_response(_OK_BODY))
    s = _settings(app_public_url="", llm_app_title="My IPEDS App")
    _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=0.0,
                                 settings=s))
    h = client.last["headers"]
    assert "HTTP-Referer" not in h, h
    assert h["X-Title"] == "My IPEDS App", h


def test_attribution_headers_omitted_when_app_title_empty():
    client = _RecordingClient(_json_response(_OK_BODY))
    s = _settings(app_public_url="https://ipeds.example.edu", llm_app_title="")
    _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=0.0,
                                 settings=s))
    h = client.last["headers"]
    assert h["HTTP-Referer"] == "https://ipeds.example.edu", h
    assert "X-Title" not in h, h


def test_provider_headers_helper_matches_chat_completion_headers():
    s = _settings(llm_api_key="k", app_public_url="https://x.edu", llm_app_title="T")
    h = llmhttp.provider_headers(s)
    assert h["Authorization"] == "Bearer k", h
    assert h["HTTP-Referer"] == "https://x.edu", h
    assert h["X-Title"] == "T", h


def test_provider_headers_helper_omits_empty_attribution():
    s = _settings(app_public_url="", llm_app_title="")
    h = llmhttp.provider_headers(s)
    assert "HTTP-Referer" not in h, h
    assert "X-Title" not in h, h
    assert "Authorization" in h, h


# ---------------------------------------------------------------------------
# 5. Provider-neutrality regression: a non-OpenRouter base URL is honored.
# ---------------------------------------------------------------------------

def test_custom_non_openrouter_base_url_is_honored():
    client = _RecordingClient(_json_response(_OK_BODY))
    s = _settings(llm_base_url="https://api.some-other-llm-provider.com/v1")
    _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=0.0,
                                 settings=s))
    url = client.last["url"]
    assert url == "https://api.some-other-llm-provider.com/v1/chat/completions", url
    assert "openrouter" not in url, \
        f"the OpenRouter host must not be hardcoded anywhere: {url}"


# ---------------------------------------------------------------------------
# 6. tools payload contract: present+tool_choice=auto when given, ABSENT
#    (neither key) when tools=None — load-bearing for the final synthesis pass.
# ---------------------------------------------------------------------------

def test_tools_present_sets_tool_choice_auto():
    client = _RecordingClient(_json_response(_OK_BODY))
    s = _settings()
    tools = [{"type": "function", "function": {"name": "run_sql"}}]
    _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=0.0,
                                 tools=tools, settings=s))
    payload = client.last["json"]
    assert payload["tools"] == tools, payload
    assert payload["tool_choice"] == "auto", payload


def test_tools_none_omits_both_tools_and_tool_choice_keys():
    client = _RecordingClient(_json_response(_OK_BODY))
    s = _settings()
    _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=0.0,
                                 tools=None, settings=s))
    payload = client.last["json"]
    assert "tools" not in payload, payload
    assert "tool_choice" not in payload, payload


def test_empty_tools_list_also_omits_both_keys():
    # Mirrors llm.py's `if tools:` truthiness check — an empty list is falsy.
    client = _RecordingClient(_json_response(_OK_BODY))
    s = _settings()
    _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=0.0,
                                 tools=[], settings=s))
    payload = client.last["json"]
    assert "tools" not in payload, payload
    assert "tool_choice" not in payload, payload


# ---------------------------------------------------------------------------
# 7. temperature forwarded verbatim — guard/critic pass a literal 0.0, llm
#    passes s.llm_temperature; these must NOT be unified into one constant.
# ---------------------------------------------------------------------------

def test_temperature_forwarded_verbatim():
    for temp in (0.0, 0.7, 1.0):
        client = _RecordingClient(_json_response(_OK_BODY))
        s = _settings()
        _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=temp,
                                     settings=s))
        assert client.last["json"]["temperature"] == temp, (temp, client.last["json"])


# ---------------------------------------------------------------------------
# 8. timeout forwarded verbatim — 120s agent default vs 30s guard/critic probe.
# ---------------------------------------------------------------------------

def test_timeout_forwarded_verbatim_default():
    client = _RecordingClient(_json_response(_OK_BODY))
    s = _settings()
    _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=0.0,
                                 settings=s))
    assert client.last["timeout"] == llmhttp.DEFAULT_TIMEOUT, client.last["timeout"]


def test_timeout_forwarded_verbatim_probe():
    client = _RecordingClient(_json_response(_OK_BODY))
    s = _settings()
    _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=0.0,
                                 timeout=llmhttp.PROBE_TIMEOUT, settings=s))
    assert client.last["timeout"] == llmhttp.PROBE_TIMEOUT, client.last["timeout"]


# ---------------------------------------------------------------------------
# 9. The helper catches NOTHING — raise_for_status()/transport errors propagate.
# ---------------------------------------------------------------------------

def test_http_status_error_propagates():
    client = _RecordingClient(_json_response({"error": "boom"}, status=500))
    s = _settings()
    try:
        _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=0.0,
                                     settings=s))
        raise AssertionError("expected HTTPStatusError to propagate")
    except httpx.HTTPStatusError:
        pass


def test_generic_transport_error_propagates():
    client = _RecordingClient(httpx.ConnectError("connection refused"))
    s = _settings()
    try:
        _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=0.0,
                                     settings=s))
        raise AssertionError("expected the transport error to propagate")
    except httpx.ConnectError:
        pass


def test_non_json_200_response_raises_valueerror_and_propagates():
    """A provider that returns HTTP 200 with a non-JSON body (an HTML error
    page, captive portal, CDN interstitial, misconfigured LLM_BASE_URL hitting
    something that isn't the API at all) makes `r.json()` raise
    json.JSONDecodeError — a ValueError subclass, NOT an httpx.HTTPError.

    This helper's contract is "catches nothing": the ValueError must propagate
    uncaught here too, exactly like the HTTPStatusError/transport-error cases
    above. Fail-open for this case belongs in the CALLERS (guard.classify,
    critic.review), not in this transport helper — see backend/tests/test_guard.py and
    backend/tests/test_critic.py for those fail-open assertions."""
    resp = httpx.Response(200, text="<html><body>Service Unavailable</body></html>",
                          request=httpx.Request("POST", "http://x/chat/completions"))
    client = _RecordingClient(resp)
    s = _settings()
    try:
        _run(llmhttp.chat_completion(client, model="m", messages=[], temperature=0.0,
                                     settings=s))
        raise AssertionError("expected a ValueError (JSONDecodeError) to propagate")
    except httpx.HTTPError:
        raise AssertionError(
            "a non-JSON 200 body must NOT be swallowed as an httpx.HTTPError — "
            "it's a ValueError (json.JSONDecodeError), a different exception type"
        ) from None
    except ValueError:
        pass  # expected: json.JSONDecodeError, uncaught by the helper


# ---------------------------------------------------------------------------
# Model + messages passthrough (basic payload sanity, not explicitly called
# out above but load-bearing for every caller).
# ---------------------------------------------------------------------------

def test_model_and_messages_passthrough_and_returns_parsed_json():
    client = _RecordingClient(_json_response(_OK_BODY))
    s = _settings()
    msgs = [{"role": "user", "content": "hello"}]
    result = _run(llmhttp.chat_completion(client, model="deepseek/deepseek-v4-flash",
                                          messages=msgs, temperature=0.0, settings=s))
    payload = client.last["json"]
    assert payload["model"] == "deepseek/deepseek-v4-flash", payload
    assert payload["messages"] == msgs, payload
    assert result == _OK_BODY, result


def run():
    print("shared LLM transport contract (backend/app/llmhttp.py):")
    check("URL == base + /chat/completions", test_url_is_base_plus_chat_completions)
    check("a trailing slash in LLM_BASE_URL does not double-slash",
          test_trailing_slash_in_base_url_does_not_double_slash)
    check("Authorization header carries the API key",
          test_authorization_header_bears_the_api_key)
    check("HTTP-Referer/X-Title map to app_public_url/llm_app_title",
          test_attribution_headers_map_to_public_url_and_app_title)
    check("HTTP-Referer omitted when app_public_url is empty",
          test_attribution_headers_omitted_when_public_url_empty)
    check("X-Title omitted when llm_app_title is empty",
          test_attribution_headers_omitted_when_app_title_empty)
    check("provider_headers() builds the same header set",
          test_provider_headers_helper_matches_chat_completion_headers)
    check("provider_headers() omits empty attribution headers",
          test_provider_headers_helper_omits_empty_attribution)
    check("a custom non-OpenRouter LLM_BASE_URL is honored (provider-neutrality)",
          test_custom_non_openrouter_base_url_is_honored)
    check("tools present -> payload carries tools + tool_choice=auto",
          test_tools_present_sets_tool_choice_auto)
    check("tools=None -> neither tools nor tool_choice key present",
          test_tools_none_omits_both_tools_and_tool_choice_keys)
    check("tools=[] (falsy) -> neither tools nor tool_choice key present",
          test_empty_tools_list_also_omits_both_keys)
    check("temperature is forwarded verbatim", test_temperature_forwarded_verbatim)
    check("timeout forwarded verbatim (default)", test_timeout_forwarded_verbatim_default)
    check("timeout forwarded verbatim (probe)", test_timeout_forwarded_verbatim_probe)
    check("HTTPStatusError propagates uncaught", test_http_status_error_propagates)
    check("a generic transport error propagates uncaught",
          test_generic_transport_error_propagates)
    check("a non-JSON 200 body raises ValueError (JSONDecodeError), not HTTPError, and propagates",
          test_non_json_200_response_raises_valueerror_and_propagates)
    check("model/messages pass through; parsed JSON body is returned",
          test_model_and_messages_passthrough_and_returns_parsed_json)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL LLMHTTP TESTS PASSED")


if __name__ == "__main__":
    run()
