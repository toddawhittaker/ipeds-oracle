"""Agent tool-loop contract — no network, no real LLM.

Guards two behaviors of stream_agent's loop:
  1. Normal path: a model reply with no tool calls becomes the answer.
  2. Budget exhaustion: if the model keeps calling tools until the iteration cap,
     we make a final tools-disabled pass and answer from the data gathered,
     rather than returning a bare "reached max tool iterations" error.
"""
import asyncio
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Must be set before app.config is imported (settings are cached).
os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_MAX_TOOL_ITERS"] = "3"

import httpx  # noqa: E402

from app import llm  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.tools import registry  # noqa: E402

get_settings.cache_clear()
# The real transport-level function, captured before any test below reassigns
# llm._chat to a fake — used by the "live transport" tests further down so the
# real _chat body (payload/headers/httpx call) actually executes, with only
# httpx.AsyncClient mocked underneath it.
_REAL_CHAT = llm._chat
FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _tool_call_response():
    return {
        "choices": [{"message": {"content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                "function": {"name": "run_sql", "arguments": '{"sql": "SELECT 1"}'}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _text_response(text):
    return {"choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def _run(question):
    return asyncio.run(llm.run_agent(question))


def test_plain_answer_path(monkeypatch=None):
    async def fake_chat(client, model, messages, tools=None):
        return _text_response("The answer is 42.")
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    res = _run("simple question")
    assert res.error is None, f"unexpected error: {res.error}"
    assert "42" in res.answer, res.answer


def test_synthesis_on_budget_exhaustion():
    calls = {"n": 0}

    async def fake_chat(client, model, messages, tools=None):
        # tools=None marks the final synthesis pass -> return prose.
        if tools is None:
            return _text_response("Best-effort summary from gathered data: 16,965.")
        calls["n"] += 1
        return _tool_call_response()  # never volunteer a final answer on its own

    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    res = _run("thorough question")
    assert res.error is None, f"should have synthesized, got error: {res.error}"
    assert "16,965" in res.answer, res.answer
    # It should have burned the whole tool budget before the synthesis pass.
    assert calls["n"] == get_settings().llm_max_tool_iters, calls["n"]


def test_hard_error_when_synthesis_is_empty():
    async def fake_chat(client, model, messages, tools=None):
        if tools is None:
            return _text_response("")  # model gives nothing to synthesize
        return _tool_call_response()
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    res = _run("hopeless question")
    assert res.error and "max tool iter" in res.error.lower(), res.error


async def _collect(agen):
    out = []
    async for e in agen:
        out.append(e)
    return out


def test_history_is_included_in_messages():
    captured = {}

    async def fake_chat(client, model, messages, tools=None):
        captured["messages"] = messages
        return _text_response("ok")
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    history = [{"role": "user", "content": "prior question"},
              {"role": "assistant", "content": "prior answer"}]
    asyncio.run(llm.run_agent("a follow-up", history=history))
    msgs = captured["messages"]
    assert any(m.get("content") == "prior question" for m in msgs), msgs
    assert any(m.get("content") == "prior answer" for m in msgs), msgs


def test_reasoning_field_yields_thinking_event():
    async def fake_chat(client, model, messages, tools=None):
        return {"choices": [{"message": {"content": "42",
                                         "reasoning": "chain of thought here"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    events = asyncio.run(_collect(llm.stream_agent("q")))
    thinking = [e for e in events if e["type"] == "thinking"]
    assert thinking and thinking[0]["text"] == "chain of thought here", events


def test_malformed_tool_args_json_is_swallowed():
    async def fake_chat(client, model, messages, tools=None):
        if tools is None:
            return _text_response("done despite bad args")
        return {"choices": [{"message": {"content": "",
                    "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "run_sql",
                                    "arguments": "{not-valid-json"}}]}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    calls = {"n": 0}

    def fake_dispatch(name, args, result_sink=None):
        calls["n"] += 1
        return "OK — 1 row(s)"
    llm._chat = fake_chat
    registry.dispatch = fake_dispatch
    events = asyncio.run(_collect(llm.stream_agent("q")))
    assert not any(e["type"] == "sql" for e in events), \
        "no 'sql' event should be yielded when the tool args fail to parse"
    assert calls["n"] >= 1, "dispatch should still be attempted with the raw args"
    assert any(e["type"] == "answer" for e in events), events


def test_non_run_sql_tool_call_yields_status_event():
    async def fake_chat(client, model, messages, tools=None):
        if tools is None:
            return _text_response("done")
        return {"choices": [{"message": {"content": "",
                    "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "list_families", "arguments": "{}"}}]}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "some families listed"
    events = asyncio.run(_collect(llm.stream_agent("q")))
    status = [e for e in events if e["type"] == "status"]
    assert any("Looking up list families" in e.get("text", "") for e in status), events


def test_escalation_after_two_consecutive_tool_failures():
    seen_models = []

    async def fake_chat(client, model, messages, tools=None):
        seen_models.append(model)
        if tools is None:
            return _text_response("final answer after escalation")
        return _tool_call_response()
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "SQL ERROR: no such table"
    events = asyncio.run(_collect(llm.stream_agent("q")))
    s = get_settings()
    assert any(e["type"] == "status" and "Escalating" in e.get("text", "")
              for e in events), events
    assert s.model_escalation in seen_models, seen_models
    assert seen_models[0] == s.model_default, \
        "should start on the default model before any failures"


def test_final_synthesis_transport_error_falls_back_to_max_iter_error():
    async def fake_chat(client, model, messages, tools=None):
        if tools is None:
            raise httpx.HTTPError("network blip during synthesis")
        return _tool_call_response()
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    res = _run("q")
    assert res.error and "max tool iter" in res.error.lower(), res.error


# ---------------------------------------------------------------------------
# Live-transport tests: llm._chat is restored to the REAL implementation, and
# only httpx.AsyncClient is mocked (returning a real httpx.Response so
# raise_for_status()/.json() behave exactly like the real thing) — this
# actually exercises _chat's own body (payload/headers/post/raise_for_status),
# not just the higher-level tool loop.
# ---------------------------------------------------------------------------

class _FakeAsyncClient:
    def __init__(self, item):
        self._item = item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None, timeout=None):
        if isinstance(self._item, BaseException):
            raise self._item
        return self._item


def _json_response(data, status=200):
    return httpx.Response(status, json=data,
                          request=httpx.Request("POST", "http://x/chat/completions"))


def _with_fake_transport(item, fn):
    orig_client_cls = llm.httpx.AsyncClient
    llm.httpx.AsyncClient = lambda *a, **k: _FakeAsyncClient(item)
    try:
        return fn()
    finally:
        llm.httpx.AsyncClient = orig_client_cls


def test_real_chat_transport_immediate_answer():
    llm._chat = _REAL_CHAT
    resp = _json_response({
        "choices": [{"message": {"content": "The answer is 99."}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    })
    res = _with_fake_transport(resp, lambda: _run("q"))
    assert res.error is None, res.error
    assert "99" in res.answer, res.answer
    assert res.prompt_tokens == 5 and res.completion_tokens == 3, res


def test_chat_transport_http_status_error_surfaces_as_agent_error():
    llm._chat = _REAL_CHAT
    resp = _json_response({"error": "server exploded"}, status=500)
    res = _with_fake_transport(resp, lambda: _run("q"))
    assert res.error and "LLM API error (500)" in res.error, res.error


def test_chat_transport_generic_http_error_surfaces_as_agent_error():
    llm._chat = _REAL_CHAT
    res = _with_fake_transport(httpx.ConnectError("connection refused"),
                               lambda: _run("q"))
    assert res.error and "LLM request failed" in res.error, res.error


def test_generate_title_no_key_returns_empty():
    orig_get_settings = llm.get_settings
    llm.get_settings = lambda: types.SimpleNamespace(llm_api_key="")
    try:
        title = asyncio.run(llm.generate_title("q", "a"))
    finally:
        llm.get_settings = orig_get_settings
    assert title == "", title


def test_generate_title_success_strips_quotes_and_period():
    llm._chat = _REAL_CHAT
    resp = _json_response({
        "choices": [{"message": {"content": '  "Nursing Degree Trends."  '}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 4},
    })
    title = _with_fake_transport(
        resp, lambda: asyncio.run(llm.generate_title(
            "How many nursing degrees were awarded?", "About 100,000.")))
    assert title == "Nursing Degree Trends", repr(title)


def test_generate_title_transport_error_returns_empty():
    llm._chat = _REAL_CHAT
    title = _with_fake_transport(
        httpx.ConnectError("boom"),
        lambda: asyncio.run(llm.generate_title("q", "a")))
    assert title == "", title


def run():
    print("agent tool-loop contract:")
    check("plain answer (no tool calls) is returned", test_plain_answer_path)
    check("budget exhaustion synthesizes from gathered data", test_synthesis_on_budget_exhaustion)
    check("empty synthesis falls back to a clear error", test_hard_error_when_synthesis_is_empty)
    check("conversation history is fed into the messages", test_history_is_included_in_messages)
    check("a reasoning field yields a 'thinking' event", test_reasoning_field_yields_thinking_event)
    check("malformed tool-call JSON args don't crash the loop",
          test_malformed_tool_args_json_is_swallowed)
    check("2 consecutive tool failures escalate to the stronger model",
          test_escalation_after_two_consecutive_tool_failures)
    check("a non-run_sql tool call yields a status event",
          test_non_run_sql_tool_call_yields_status_event)
    check("a transport error during the final synthesis pass still errors cleanly",
          test_final_synthesis_transport_error_falls_back_to_max_iter_error)
    check("real _chat: immediate answer over a mocked httpx transport",
          test_real_chat_transport_immediate_answer)
    check("real _chat: an HTTPStatusError surfaces as an agent error",
          test_chat_transport_http_status_error_surfaces_as_agent_error)
    check("real _chat: a generic transport error surfaces as an agent error",
          test_chat_transport_generic_http_error_surfaces_as_agent_error)
    check("generate_title returns '' with no API key configured",
          test_generate_title_no_key_returns_empty)
    check("generate_title strips quotes/period from a real response",
          test_generate_title_success_strips_quotes_and_period)
    check("generate_title returns '' on a transport error",
          test_generate_title_transport_error_returns_empty)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL AGENT-LOOP TESTS PASSED")


if __name__ == "__main__":
    run()
