"""Feedback-distilled lessons (backend/app/feedback.py): symmetric to the critic
(app/critic.py) — the critic mines the MODEL's mistakes, this mines the USER's
corrective feedback on a follow-up turn ("you should have kept the bachelor's
scope") into a candidate lesson.

`distill_feedback(history, latest_user_msg) -> (headline, description) | None`:
- A cheap SEPARATE LLM call, gated on the existing `skills_enabled` setting (no
  new env var) — consistent with the rest of the self-learning pipeline (lesson
  retrieval, the semantic answer cache) already gating on it.
- Runs ONLY when `history` is non-empty: a first-turn question has no prior
  answer to give feedback ABOUT, so distilling it is a context-less, useless call.
- Fails OPEN (returns None) on no key, disabled, an empty/non-generalizable
  reply, or any transport error — mirroring app/critic.py's review() and
  app/guard.py's classify(): this is an enhancement, never something that can
  block or crash a chat turn.
- The verdict is REVISE-shaped, parsed via the critic's OWN `parse_verdict` (not
  a new parser): OK -> no generalizable feedback -> None; REVISE -> (headline,
  description), the same structured shape the critic already emits for lessons.
"""
import asyncio
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Must be set before app.config is imported (settings are cached).
os.environ["LLM_API_KEY"] = "test-key"

import httpx  # noqa: E402

from app import feedback  # noqa: E402
from app.config import get_settings  # noqa: E402

get_settings.cache_clear()
FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


_HISTORY = [
    {"role": "user", "content": "which undergraduate major produces the most graduates?"},
    {"role": "assistant", "content": "Nursing led with 147,345 bachelor's completions."},
]


# --- fail-open gating ------------------------------------------------------

def test_distill_returns_none_without_key():
    orig = feedback.get_settings
    feedback.get_settings = lambda: types.SimpleNamespace(
        skills_enabled=True, llm_api_key="")
    try:
        r = asyncio.run(feedback.distill_feedback(
            _HISTORY, "you should have asked me a clarifying question"))
    finally:
        feedback.get_settings = orig
    assert r is None, "no API key must fail open (None)"


def test_distill_returns_none_when_skills_disabled():
    orig = feedback.get_settings
    feedback.get_settings = lambda: types.SimpleNamespace(
        skills_enabled=False, llm_api_key="test-key")
    try:
        r = asyncio.run(feedback.distill_feedback(
            _HISTORY, "you should have asked me a clarifying question"))
    finally:
        feedback.get_settings = orig
    assert r is None, "skills_enabled=False must fail open (None), no extra network call"


def test_distill_returns_none_with_empty_history():
    # A first-turn question has no prior answer to correct -- must be a no-op
    # even with a configured key and skills on, and without ever calling out.
    orig_settings = feedback.get_settings
    feedback.get_settings = lambda: types.SimpleNamespace(
        skills_enabled=True, llm_api_key="test-key")

    async def _explode(*a, **k):
        raise AssertionError("must not call the LLM when history is empty")
    orig_client = feedback.httpx.AsyncClient
    feedback.httpx.AsyncClient = _explode
    try:
        r = asyncio.run(feedback.distill_feedback([], "some corrective feedback"))
    finally:
        feedback.get_settings = orig_settings
        feedback.httpx.AsyncClient = orig_client
    assert r is None, "empty history must no-op without touching the network"


# --- live transport (mocked httpx.AsyncClient, real dispatch) --------------

def _configured(**overrides):
    base = dict(skills_enabled=True, llm_api_key="test-key",
                model_default="deepseek/deepseek-v4-flash",
                llm_base_url="https://openrouter.ai/api/v1",
                app_public_url="http://localhost:8000", llm_app_title="IPEDS Query")
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _FakeAsyncClient:
    calls: list = []

    def __init__(self, item):
        self._item = item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None, timeout=None):
        _FakeAsyncClient.calls.append({"url": url, "json": json,
                                       "headers": headers, "timeout": timeout})
        if isinstance(self._item, BaseException):
            raise self._item
        return self._item


def _json_response(data, status=200):
    return httpx.Response(status, json=data,
                          request=httpx.Request("POST", "http://x/chat/completions"))


def _with_fake_transport(item, fn):
    _FakeAsyncClient.calls = []
    orig_settings, orig_client = feedback.get_settings, feedback.httpx.AsyncClient
    feedback.get_settings = _configured
    feedback.httpx.AsyncClient = lambda *a, **k: _FakeAsyncClient(item)
    try:
        return fn()
    finally:
        feedback.get_settings = orig_settings
        feedback.httpx.AsyncClient = orig_client


def test_distill_returns_none_on_ok_verdict():
    resp = _json_response({
        "choices": [{"message": {"content": "OK"}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 1},
    })
    r = _with_fake_transport(
        resp, lambda: asyncio.run(feedback.distill_feedback(
            _HISTORY, "thanks, that's exactly what I needed")))
    assert r is None, "an OK verdict (no generalizable feedback) must return None"


def test_distill_parses_revise_shaped_reply_into_headline_and_description():
    resp = _json_response({
        "choices": [{"message": {"content":
            "REVISE\n"
            "HEADLINE: Ask a clarifying question before assuming an award-level scope.\n"
            "DESCRIPTION: When a request like \"which major produces the most "
            "graduates\" doesn't specify an award level, ask before picking one "
            "instead of silently assuming bachelor's-only."}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 10},
    })
    r = _with_fake_transport(
        resp, lambda: asyncio.run(feedback.distill_feedback(
            _HISTORY,
            "you could have asked me a clarifying question instead of guessing "
            "bachelor's-only")))
    assert r is not None, "a REVISE-shaped reply must produce a (headline, description) pair"
    headline, description = r
    assert headline == "Ask a clarifying question before assuming an award-level scope.", headline
    # This phrase lives ONLY in the fixture's DESCRIPTION line (the HEADLINE line
    # above has no "bachelor's-only" text) -- pins that `description` really is the
    # parsed DESCRIPTION body, verbatim, not the headline or some other field.
    assert "silently assuming bachelor's-only" in description, description


def test_distill_transport_error_returns_none():
    r = _with_fake_transport(
        httpx.ConnectError("refused"),
        lambda: asyncio.run(feedback.distill_feedback(_HISTORY, "you got that wrong")))
    assert r is None, "a transport error must fail open (None), never raise"


def test_distill_non_json_200_response_returns_none():
    """A provider can return HTTP 200 with a non-JSON body (captive portal, proxy
    error page). r.json() raises json.JSONDecodeError (a ValueError subclass, NOT
    an httpx.HTTPError) -- `except httpx.HTTPError` alone would miss it and crash
    the caller mid-stream. Must fail open like every other error path here."""
    non_json_response = httpx.Response(
        200, text="<html><body>502 Bad Gateway</body></html>",
        request=httpx.Request("POST", "http://x/chat/completions"))
    r = _with_fake_transport(
        non_json_response,
        lambda: asyncio.run(feedback.distill_feedback(_HISTORY, "you got that wrong")))
    assert r is None, "a 200 response with a non-JSON body must fail open, not raise"


def test_distill_posts_url_headers_and_probe_timeout():
    """distill_feedback is a cheap probe call like guard.classify/critic.review —
    it must route through the shared llmhttp transport with PROBE_TIMEOUT (30s),
    not the full-turn DEFAULT_TIMEOUT (120s)."""
    from app import llmhttp
    resp = _json_response({
        "choices": [{"message": {"content": "OK"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    _with_fake_transport(
        resp, lambda: asyncio.run(feedback.distill_feedback(_HISTORY, "feedback text")))
    call = _FakeAsyncClient.calls[-1]
    assert call["url"] == "https://openrouter.ai/api/v1/chat/completions", call["url"]
    assert call["headers"]["Authorization"] == "Bearer test-key", call["headers"]
    assert call["timeout"] == llmhttp.PROBE_TIMEOUT, call["timeout"]


def test_distill_message_includes_history_and_latest_feedback():
    """The prior turn's Q&A and the user's corrective message must actually reach
    the model -- otherwise it has nothing to generalize a rule FROM."""
    resp = _json_response({
        "choices": [{"message": {"content": "OK"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    _with_fake_transport(
        resp, lambda: asyncio.run(feedback.distill_feedback(
            _HISTORY, "you should have kept the bachelor's scope from before")))
    call = _FakeAsyncClient.calls[-1]
    joined = " ".join(m.get("content") or "" for m in call["json"]["messages"])
    assert "147,345" in joined, joined
    assert "you should have kept the bachelor's scope from before" in joined, joined


def run():
    print("feedback distiller contract:")
    check("distill fails open without a key", test_distill_returns_none_without_key)
    check("distill fails open when skills are disabled",
          test_distill_returns_none_when_skills_disabled)
    check("distill no-ops (no network call) on empty history",
          test_distill_returns_none_with_empty_history)
    check("an OK verdict returns None (no generalizable feedback)",
          test_distill_returns_none_on_ok_verdict)
    check("a REVISE-shaped reply parses into (headline, description)",
          test_distill_parses_revise_shaped_reply_into_headline_and_description)
    check("a transport error fails open (None)", test_distill_transport_error_returns_none)
    check("a non-JSON 200 body fails open, does not raise",
          test_distill_non_json_200_response_returns_none)
    check("distill posts url/headers/PROBE_TIMEOUT via llmhttp",
          test_distill_posts_url_headers_and_probe_timeout)
    check("the prompt carries the prior turn + the latest corrective message",
          test_distill_message_includes_history_and_latest_feedback)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} feedback test(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL FEEDBACK TESTS PASSED")


if __name__ == "__main__":
    run()
