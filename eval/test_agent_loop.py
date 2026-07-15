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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Must be set before app.config is imported (settings are cached).
os.environ["OPENROUTER_API_KEY"] = "test-key"
os.environ["LLM_MAX_TOOL_ITERS"] = "3"

from app import llm  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.tools import registry  # noqa: E402

get_settings.cache_clear()
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


def run():
    print("agent tool-loop contract:")
    check("plain answer (no tool calls) is returned", test_plain_answer_path)
    check("budget exhaustion synthesizes from gathered data", test_synthesis_on_budget_exhaustion)
    check("empty synthesis falls back to a clear error", test_hard_error_when_synthesis_is_empty)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL AGENT-LOOP TESTS PASSED")


if __name__ == "__main__":
    run()
