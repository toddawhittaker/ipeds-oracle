"""Agent tool-loop contract — no network, no real LLM.

Guards two behaviors of stream_agent's loop:
  1. Normal path: a model reply with no tool calls becomes the answer.
  2. Budget exhaustion: if the model keeps calling tools until the iteration cap,
     we make a final tools-disabled pass and answer from the data gathered,
     rather than returning a bare "reached max tool iterations" error.
"""
import asyncio
import json
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Must be set before app.config is imported (settings are cached).
os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_MAX_TOOL_ITERS"] = "3"

import httpx  # noqa: E402

from app import critic, llm  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.critic import Critique  # noqa: E402
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
    # A usage payload with NO cache field leaves the metric at 0 (not None/NaN) —
    # the "provider reports nothing" baseline the dashboard degrades to gracefully.
    assert res.cached_prompt_tokens == 0, res.cached_prompt_tokens


def test_cached_prompt_tokens_accumulate_across_calls_openrouter_shape():
    # The regression: we sum prompt_tokens/cost from each call's usage but silently
    # DROP the provider's prompt-cache field, so the dashboard can never show a
    # prompt-cache-hit rate. Two LLM calls (a tool round + the final answer), each
    # reporting cached tokens the OpenRouter way, must roll up AND sum.
    calls = {"n": 0}

    async def fake_chat(client, model, messages, tools=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"choices": [{"message": {"content": "",
                "tool_calls": [{"id": "c1", "type": "function",
                    "function": {"name": "run_sql", "arguments": '{"sql": "SELECT 1"}'}}]}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5,
                          "prompt_tokens_details": {"cached_tokens": 60}}}
        return {"choices": [{"message": {"content": "Answer."}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5,
                          "prompt_tokens_details": {"cached_tokens": 80}}}

    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    res = _run("q")
    assert res.error is None, res.error
    assert res.cached_prompt_tokens == 140, res.cached_prompt_tokens
    # The schema-prefix split records ONLY the first call (60 cached / 100 prompt),
    # NOT the blended 140 — later tool rounds must not leak into the first-call
    # figures, or the "schema cache" rate would be inflated by in-turn caching.
    assert res.first_call_cached_prompt_tokens == 60, res.first_call_cached_prompt_tokens
    assert res.first_call_prompt_tokens == 100, res.first_call_prompt_tokens


def test_cached_prompt_tokens_accept_deepseek_native_shape():
    # DeepSeek-direct reports the same fact as `prompt_cache_hit_tokens` (not the
    # nested OpenRouter details) — the reader must accept both spellings.
    async def fake_chat(client, model, messages, tools=None):
        return {"choices": [{"message": {"content": "Answer."}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 2,
                          "prompt_cache_hit_tokens": 45}}
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    res = _run("q")
    assert res.cached_prompt_tokens == 45, res.cached_prompt_tokens


def test_effective_cost_prefers_provider_reported():
    # When the provider reports a real cost, that wins — the fallback prices are
    # never consulted (they could be stale/wrong; the bill is authoritative).
    s = types.SimpleNamespace(llm_input_cost_per_mtok=999.0, llm_output_cost_per_mtok=999.0)
    assert llm.effective_cost(0.0123, 1000, 500, s) == 0.0123


def test_effective_cost_estimates_from_prices_when_report_is_zero():
    # Provider silent (cost=0) but admin set list prices -> estimate from tokens.
    # 2,000,000 input @ $0.30/Mtok + 1,000,000 output @ $1.20/Mtok = 0.60 + 1.20.
    s = types.SimpleNamespace(llm_input_cost_per_mtok=0.30, llm_output_cost_per_mtok=1.20)
    assert abs(llm.effective_cost(0.0, 2_000_000, 1_000_000, s) - 1.80) < 1e-9


def test_effective_cost_zero_when_no_report_and_no_prices():
    # The default posture: provider reports cost (so prices stay 0). If a provider
    # is ALSO silent and no prices are set, spend legitimately stays 0 — never NaN.
    s = types.SimpleNamespace(llm_input_cost_per_mtok=0.0, llm_output_cost_per_mtok=0.0)
    assert llm.effective_cost(0.0, 5000, 2000, s) == 0.0


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
# Clarify (disambiguation) protocol: the model emits a ```clarify {"question":
# "...","options":[...]}``` fence when a request is materially ambiguous. Mirrors
# _extract_figure/_extract_suggestions (llm.py): always strip the fence, parse a
# dict only on valid JSON with a non-empty question and >=1 option. When present,
# the loop must skip the critic pass and leave figure/suggestions unset.
# ---------------------------------------------------------------------------

def test_extract_clarify_valid_json_strips_fence_and_parses():
    from app.llm import _extract_clarify
    ans = ("Do you mean bachelor's degrees only, or all award levels?\n\n"
           "```clarify\n"
           '{"question":"Which award level?",'
           '"options":["Bachelor\'s only","Include all levels"]}\n'
           "```\n")
    clean, clarify = _extract_clarify(ans)
    assert "```clarify" not in clean, clean
    assert clarify == {"question": "Which award level?",
                       "options": ["Bachelor's only", "Include all levels"]}, clarify


def test_extract_clarify_invalid_json_strips_fence_returns_none():
    from app.llm import _extract_clarify
    clean, clarify = _extract_clarify("x\n```clarify\nnot json\n```\ny")
    assert "```clarify" not in clean, \
        "the fence must be stripped even when it fails to parse (no raw JSON leak)"
    assert clarify is None, clarify


def test_extract_clarify_no_fence_is_unchanged():
    from app.llm import _extract_clarify
    assert _extract_clarify("a plain answer, no fence") == ("a plain answer, no fence", None)
    assert _extract_clarify("") == ("", None)


def test_extract_clarify_missing_question_or_options_is_none():
    from app.llm import _extract_clarify
    # No question at all.
    _, c1 = _extract_clarify('```clarify\n{"options":["a","b"]}\n```')
    assert c1 is None, c1
    # Blank question.
    _, c2 = _extract_clarify('```clarify\n{"question":"   ","options":["a"]}\n```')
    assert c2 is None, c2
    # No options.
    _, c3 = _extract_clarify('```clarify\n{"question":"Which?","options":[]}\n```')
    assert c3 is None, c3
    _, c4 = _extract_clarify('```clarify\n{"question":"Which?"}\n```')
    assert c4 is None, c4


def test_clarify_present_skips_critic_and_unsets_figure_and_suggestions():
    # A defensive scenario: the model ran SQL (sql_log non-empty, so the EXISTING
    # `res.sql_log` gate alone would let the critic through) but then decided the
    # request was materially ambiguous and asked a clarifying question instead of
    # answering. The clarify branch must override that gate explicitly -- this is
    # the regression a bare "no SQL -> no critic" check (already covered in
    # test_critic.py) can NOT catch, because sql_log is non-empty here.
    calls = {"chat": 0, "critic": 0}
    clarify_fence = (
        "Do you mean bachelor's degrees only, or all award levels?\n\n"
        "```clarify\n"
        '{"question":"Which award level?",'
        '"options":["Bachelor\'s only","Include all levels"]}\n'
        "```\n")

    async def fake_chat(client, model, messages, tools=None):
        calls["chat"] += 1
        if calls["chat"] == 1:
            return {"choices": [{"message": {"content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "run_sql", "arguments": '{"sql": "SELECT 1"}'}}]}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        return _text_response(clarify_fence)

    async def fake_review(question, sql_log, answer, *a, **kw):
        calls["critic"] += 1
        return Critique(ok=True)

    llm._chat = fake_chat
    llm.critic.review = fake_review
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    try:
        res = _run("which undergraduate major produces the most graduates?")
    finally:
        llm.critic.review = critic.review
    assert res.clarify == {"question": "Which award level?",
                           "options": ["Bachelor's only", "Include all levels"]}, res.clarify
    assert "```clarify" not in res.answer, res.answer
    assert calls["critic"] == 0, "the critic must never run on a clarify turn"
    assert res.figure is None, "a clarify turn must not carry a figure"
    assert res.suggestions is None, "a clarify turn must not carry followup suggestions"


# --- result retention + figure grounding ---------------------------------------
# Retention is the foundation: `last_result` alone was OVERWRITTEN on every
# run_sql, so a multi-query brief (the recent-years table plus the rank/share
# query prompt step 6(a) invites) discarded the very result its headline figure
# came from — leaving the server unable to check that figure against any data.

def _sql_call_response(sql, call_id):
    return {"choices": [{"message": {"content": "",
                "tool_calls": [{"id": call_id, "type": "function",
                    "function": {"name": "run_sql",
                                 "arguments": json.dumps({"sql": sql})}}]}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _sink_dispatch(results):
    """A dispatch stand-in that fills the sink the way registry._tool_run_sql
    does — one QueryResult per call, appended in call order."""
    seq = list(results)

    def dispatch(name, args, result_sink=None):
        if result_sink is not None and seq:
            r = seq.pop(0)
            result_sink["result"] = r
            result_sink.setdefault("results", []).append(r)
        return "OK — 1 row(s)"
    return dispatch


def test_every_query_result_of_the_turn_is_retained_in_call_order():
    from app.tools.sql import QueryResult
    r1 = QueryResult(columns=["n"], rows=[(1,)], row_count=1)
    r2 = QueryResult(columns=["n"], rows=[(2,)], row_count=1)
    r3 = QueryResult(columns=["n"], rows=[(3,)], row_count=1)
    calls = {"n": 0}

    async def fake_chat(client, model, messages, tools=None):
        calls["n"] += 1
        if calls["n"] <= 3:
            return _sql_call_response(f"SELECT {calls['n']} AS n", f"c{calls['n']}")
        return _text_response("done")
    llm._chat = fake_chat
    registry.dispatch = _sink_dispatch([r1, r2, r3])
    res = _run("q")
    assert len(res.results) == 3, f"expected 3 retained results, got {len(res.results)}"
    assert [r.rows[0][0] for r in res.results] == [1, 2, 3], "call order must survive"
    assert res.last_result is r3, "the last-result contract must still hold"


def test_a_figure_derived_from_an_earlier_query_is_grounded():
    """The multi-query case retention exists for: the headline comes from the
    FIRST query while a later one fills the recent-years table."""
    from app.tools.sql import QueryResult
    ranking = QueryResult(columns=["institution", "awards"],
                          rows=[("Ohio State", 400), ("Texas A&M", 300)], row_count=2)
    years = QueryResult(columns=["year", "awards"],
                        rows=[(2023, 90), (2024, 95)], row_count=2)
    calls = {"n": 0}

    async def fake_chat(client, model, messages, tools=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            return _sql_call_response("SELECT 1", f"c{calls['n']}")
        return _text_response(
            'Ohio State led.\n\n```figure\n{"value":"400","label":"Awards"}\n```')
    llm._chat = fake_chat
    registry.dispatch = _sink_dispatch([ranking, years])
    res = _run("q")
    assert res.figure_grounding == "exact", res.figure_grounding
    assert res.figure_derivation == "value(q1.awards)", res.figure_derivation


def test_an_invented_figure_is_recorded_as_ungrounded_but_still_ships():
    """OBSERVE-ONLY, and that is the point of this test: a number absent from
    the data is FLAGGED, but the answer and the figure are delivered untouched.
    If this ever starts suppressing, it is a behavior change that needs its own
    decision — not something to discover in production."""
    from app.tools.sql import QueryResult
    r = QueryResult(columns=["awards"], rows=[(400,)], row_count=1)
    calls = {"n": 0}

    async def fake_chat(client, model, messages, tools=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _sql_call_response("SELECT 1", "c1")
        return _text_response(
            'Huge.\n\n```figure\n{"value":"87,654","label":"Awards"}\n```')
    llm._chat = fake_chat
    registry.dispatch = _sink_dispatch([r])
    res = _run("q")
    assert res.figure_grounding == "ungrounded", res.figure_grounding
    assert res.figure == {"value": "87,654", "label": "Awards"}, \
        "observe-only: the figure must reach the user unchanged"
    assert "Huge." in res.answer, res.answer


def test_an_answer_with_no_figure_is_not_measured():
    # no_figure must not count toward the grounded rate in either direction.
    async def fake_chat(client, model, messages, tools=None):
        return _text_response("A plain lookup answer with no hero number.")
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    assert _run("q").figure_grounding == "no_figure"


def test_clarify_absent_on_a_normal_answer():
    async def fake_chat(client, model, messages, tools=None):
        return _text_response("California awarded 7,679 CS bachelor's degrees.")
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    res = _run("q")
    assert res.clarify is None, res.clarify


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
    # The raw upstream body must NOT be reflected to the client (it can carry
    # provider/proxy detail); only the status code is surfaced.
    assert "server exploded" not in res.error, \
        f"upstream response body leaked into the client error: {res.error!r}"


def test_chat_transport_generic_http_error_surfaces_as_agent_error():
    llm._chat = _REAL_CHAT
    res = _with_fake_transport(httpx.ConnectError("connection refused"),
                               lambda: _run("q"))
    assert res.error and "LLM request failed" in res.error, res.error
    # Security (CodeQL py/stack-trace-exposure): the transport exception's text
    # can carry connection/host detail and is surfaced to the user via the
    # persisted message — it must NOT leak into the client-facing error string.
    assert "connection refused" not in res.error, res.error


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
    check("_extract_clarify parses valid JSON and strips the fence",
          test_extract_clarify_valid_json_strips_fence_and_parses)
    check("_extract_clarify strips the fence even on invalid JSON",
          test_extract_clarify_invalid_json_strips_fence_returns_none)
    check("_extract_clarify is a no-op with no fence present",
          test_extract_clarify_no_fence_is_unchanged)
    check("_extract_clarify returns None for a missing question/options",
          test_extract_clarify_missing_question_or_options_is_none)
    check("a clarify turn skips the critic and unsets figure/suggestions",
          test_clarify_present_skips_critic_and_unsets_figure_and_suggestions)
    check("a normal answer carries no clarify", test_clarify_absent_on_a_normal_answer)
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
    check("every query result of the turn is retained, in call order",
          test_every_query_result_of_the_turn_is_retained_in_call_order)
    check("a figure derived from an EARLIER query is grounded",
          test_a_figure_derived_from_an_earlier_query_is_grounded)
    check("an invented figure is flagged ungrounded but still ships (observe-only)",
          test_an_invented_figure_is_recorded_as_ungrounded_but_still_ships)
    check("an answer with no figure is not measured",
          test_an_answer_with_no_figure_is_not_measured)
    check("effective_cost prefers the provider-reported cost over fallback prices",
          test_effective_cost_prefers_provider_reported)
    check("effective_cost estimates from list prices when the report is 0",
          test_effective_cost_estimates_from_prices_when_report_is_zero)
    check("effective_cost stays 0 with no report and no configured prices",
          test_effective_cost_zero_when_no_report_and_no_prices)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL AGENT-LOOP TESTS PASSED")


if __name__ == "__main__":
    run()
