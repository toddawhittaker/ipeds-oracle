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


# --- S5: the critic reviews the tool-budget-exhausted answer too ---------------
# The exhaustion tail used to ship with ZERO critic review. Now it runs the
# critic and, on a REVISE, gets ONE bounded correction round with tools
# RE-ENABLED — same requeried-and-changed anti-leak gate as the normal path.

def _exhaust_then(*, correction, revise=True):
    """A fake_chat: burn the tool budget (main loop) → a draft synthesis answer →
    then `correction` drives the critic-triggered correction round. `correction`
    is called with (calls_dict, requeried_flag) and returns the next response.

    llm.critic.review is patched to REVISE (or OK) by the caller."""
    mi = get_settings().llm_max_tool_iters
    state = {"n": 0, "requeried": False}

    async def fake_chat(client, model, messages, tools=None):
        state["n"] += 1
        if tools is None:
            # synthesis draft (and the correction round's forced-answer fallback,
            # which these tests never reach)
            return _text_response("Draft best-effort answer: 500 degrees.")
        if state["n"] <= mi:
            return _tool_call_response()  # exhaust the main tool budget
        return correction(state)          # the critic-driven correction round
    return fake_chat


def _patch_review(ok):
    async def fake_review(question, sql_log, answer, *a, **k):
        return Critique(ok=ok, headline="H", description="D") if not ok \
            else Critique(ok=True)
    return fake_review


def _run_with_review(question, fake_chat, review_ok):
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    orig = llm.critic.review
    llm.critic.review = _patch_review(review_ok)
    try:
        return _run(question)
    finally:
        llm.critic.review = orig


def test_exhaustion_critic_correction_requeries_and_changes():
    """THE fix: a flagged exhaustion answer gets a real correction — the round
    re-queries (tools re-enabled) and lands on a different answer, so it ships
    and the turn is marked revised."""
    def correction(state):
        if not state["requeried"]:
            state["requeried"] = True
            return _tool_call_response()  # a re-query in the correction round
        return _text_response("Corrected after re-query: 1,000,000 degrees.")
    res = _run_with_review("q", _exhaust_then(correction=correction), review_ok=False)
    assert "1,000,000" in res.answer, res.answer
    assert res.critic_revised is True, "a genuine re-queried correction must ship"
    assert len(res.sql_log) > get_settings().llm_max_tool_iters, \
        "the correction round's re-query must land in sql_log"


def test_exhaustion_correction_confirming_same_answer_reverts_to_draft():
    """A re-query that merely confirms the same number is a critic false alarm —
    it must not disturb the draft, and records no revision."""
    def correction(state):
        if not state["requeried"]:
            state["requeried"] = True
            return _tool_call_response()
        return _text_response("Draft best-effort answer: 500 degrees.")  # unchanged
    res = _run_with_review("q", _exhaust_then(correction=correction), review_ok=False)
    assert "500" in res.answer, res.answer
    assert res.critic_revised is False, "an unchanged re-query is not a revision"


def test_exhaustion_correction_rebuttal_without_requery_reverts_to_draft():
    """The [[critic-revision-leak]] gate holds on this path too: a rebuttal that
    changes the prose but runs NO new run_sql is discarded — the clean draft
    ships, unrevised."""
    def correction(state):
        # Answers immediately, no re-query — a rebuttal.
        return _text_response("Actually the draft is right; 500 stands.")
    res = _run_with_review("q", _exhaust_then(correction=correction), review_ok=False)
    assert "500 degrees" in res.answer, res.answer  # the ORIGINAL draft
    assert "Actually" not in res.answer, "a no-requery rebuttal must not ship"
    assert res.critic_revised is False, res.critic_revised


def test_exhaustion_critic_ok_ships_draft_unchanged():
    """When the critic passes the exhaustion answer, no correction round runs and
    the draft ships as-is."""
    def correction(state):  # never reached — critic says OK
        return _text_response("should not be used")
    res = _run_with_review("q", _exhaust_then(correction=correction), review_ok=True)
    assert "500 degrees" in res.answer, res.answer
    assert res.critic_revised is False, res.critic_revised
    assert len(res.sql_log) == get_settings().llm_max_tool_iters, \
        "no correction round → sql_log unchanged from the main loop"


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


def test_followup_turn_gets_a_tail_reminder_after_the_cached_prefix():
    """Measured regression: figure emission decayed with conversation DEPTH —
    turns 1-2 emitted, turns 3+ did not, and turn 6 failed on a question
    structurally identical to turn 1's. The system prompt must stay FIRST to
    remain the cacheable prefix, so its rules end up buried behind the
    conversation; this puts a pointer back to them next to the question.

    Placement is asserted, not just presence. A reminder ahead of the system
    prompt would silently collapse prompt-cache reuse and bill every schema
    token at full price — a costly regression with no functional symptom."""
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

    idx = [i for i, m in enumerate(msgs) if m.get("content") == llm._TURN_REMINDER]
    assert idx, f"no tail reminder on a follow-up turn: {msgs}"
    at = idx[0]
    assert at > 0, "the reminder must NEVER precede the cached system prefix"
    assert msgs[0]["role"] == "system" and "IPEDS" in msgs[0]["content"], msgs[0]
    # ...and it must sit after the history, immediately before the question, so
    # the rules are the last thing read before the task.
    assert msgs[at + 1]["content"] == "a follow-up", msgs[at + 1]
    assert any(m.get("content") == "prior answer" for m in msgs[:at]), msgs[:at]


def test_first_turn_gets_no_tail_reminder():
    """Follow-ups only. First turns already comply (measured 2/2) because the
    rules are still adjacent, so spending tokens there buys nothing."""
    captured = {}

    async def fake_chat(client, model, messages, tools=None):
        captured["messages"] = messages
        return _text_response("ok")
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    asyncio.run(llm.run_agent("a first question"))
    msgs = captured["messages"]
    assert not any(m.get("content") == llm._TURN_REMINDER for m in msgs), msgs


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


def test_an_unparseable_figure_fence_is_malformed_not_no_figure():
    """These two look identical downstream — _extract_figure returns None for
    both — but they call for OPPOSITE fixes: 'no_figure' is a prompt-compliance
    problem (the model didn't emit one), 'malformed' is a format problem (it did,
    but the JSON was junk). Collapsing them hides which one you have, which is
    exactly the ambiguity that made the 0/9-follow-ups finding hard to diagnose."""
    from app.tools.sql import QueryResult
    r = QueryResult(columns=["awards"], rows=[(400,)], row_count=1)
    calls = {"n": 0}

    async def fake_chat(client, model, messages, tools=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _sql_call_response("SELECT 1", "c1")
        return _text_response('Answer.\n\n```figure\n{not valid json\n```')
    llm._chat = fake_chat
    registry.dispatch = _sink_dispatch([r])
    res = _run("q")
    assert res.figure_grounding == "malformed", res.figure_grounding
    assert res.figure is None, res.figure
    # The fence is still stripped, so raw JSON never reaches the user.
    assert "```figure" not in res.answer, res.answer


def test_an_answer_with_no_figure_is_not_measured():
    # no_figure must not count toward the grounded rate in either direction.
    async def fake_chat(client, model, messages, tools=None):
        return _text_response("A plain lookup answer with no hero number.")
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    assert _run("q").figure_grounding == "no_figure"


# --- conversation-scoped grounding (prior_results) ------------------------------
# A follow-up that recites a number WITHOUT re-querying used to be 'unchecked'
# (turn-scoped grounding saw no current results). Prior turns' results now ground
# it, and the retry can reach it. `ctx:` marks a match against an EARLIER turn.

def _run_with_prior(question, prior_results):
    return asyncio.run(llm.run_agent(question, history=[{"role": "user", "content": "x"}],
                                     prior_results=prior_results))


def test_a_recited_figure_grounds_against_a_prior_turn():
    """The `unchecked`→grounded fix: a no-SQL follow-up whose figure value lives
    in an EARLIER turn's result grounds, tagged `ctx:`."""
    from app.tools.sql import QueryResult
    prior = [QueryResult(columns=["awards"], rows=[(400,)], row_count=1)]

    async def fake_chat(client, model, messages, tools=None):
        # No tool call — answers straight from context (sql_log stays empty).
        return _text_response('Still 400.\n\n```figure\n{"value":"400","label":"Awards"}\n```')
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    res = _run_with_prior("and that top school again?", prior)
    assert res.figure == {"value": "400", "label": "Awards"}, res.figure
    assert res.figure_grounding == "exact", res.figure_grounding
    assert res.figure_derivation == "ctx:value(q1.awards)", res.figure_derivation


def test_retry_fires_on_a_no_sql_turn_using_prior_results():
    """The residual no_figure gap: a no-SQL follow-up that emits no figure but
    recites a number present in a prior turn now gets one, grounded `retry:ctx:`."""
    from app.tools.sql import QueryResult
    prior = [QueryResult(columns=["awards"], rows=[(400,)], row_count=1)]

    async def fake_chat(client, model, messages, tools=None):
        return _text_response("It was 400, as I said.")  # digit, no figure, no SQL
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"

    async def fake_retry(question, answer, *a, **k):
        return llm._FigureRetry(figure={"value": "400", "label": "Awards"})
    orig = llm.retry_missing_figure
    llm.retry_missing_figure = fake_retry
    try:
        res = _run_with_prior("the top school's count again?", prior)
    finally:
        llm.retry_missing_figure = orig
    assert res.figure == {"value": "400", "label": "Awards"}, res.figure
    assert res.figure_derivation == "retry:ctx:value(q1.awards)", res.figure_derivation


def test_figure_required_relaxes_only_when_prior_results_exist():
    """The retry-gate relaxation, pinned directly: a no-SQL numeric answer is
    figure-required ONLY when there are prior results to ground against."""
    res = llm.AgentResult(sql_log=[])
    assert llm._figure_required(res, "It was 400.", has_prior=True) is True
    assert llm._figure_required(res, "It was 400.", has_prior=False) is False
    # No digit → never required, prior or not (the enumerable skip).
    assert llm._figure_required(res, "The address is Main St.", has_prior=True) is False


def test_current_turn_results_take_precedence_over_prior():
    """A figure from THIS turn's query must ground against THIS turn's data, not a
    prior turn's — no spurious `ctx:` when the number is in the current result."""
    from app.tools.sql import QueryResult
    prior = [QueryResult(columns=["awards"], rows=[(400,)], row_count=1)]
    current = QueryResult(columns=["awards"], rows=[(400,)], row_count=1)
    calls = {"n": 0}

    async def fake_chat(client, model, messages, tools=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _sql_call_response("SELECT 1", "c1")
        return _text_response('Now 400.\n\n```figure\n{"value":"400","label":"Awards"}\n```')
    llm._chat = fake_chat
    registry.dispatch = _sink_dispatch([current])
    res = _run_with_prior("re-run it", prior)
    assert res.figure_grounding == "exact", res.figure_grounding
    assert res.figure_derivation == "value(q1.awards)", \
        "current-turn match must not be tagged ctx:"


# --- parser leniency: recover a MIS-WRAPPED figure (_extract_figure) -----------
# The 0/10 compression run showed the model emitting a CORRECT figure object but
# not inside a ```figure fence — as a bare object at the answer's head, sometimes
# behind a stray `[Figure: 767](767)` markdown-link artifact — so _extract_figure
# caught nothing. Recovering these costs no LLM call.

def test_extract_figure_recovers_the_observed_broken_wrapper():
    ans = '[Figure: 767](767)\n\n{"value":"767","label":"CS bachelor\'s, Ohio 2025"}\n\nrest'
    clean, fig = llm._extract_figure(ans)
    assert fig == {"value": "767", "label": "CS bachelor's, Ohio 2025"}, fig
    assert clean == "rest", repr(clean)


def test_extract_figure_recovers_a_leading_bare_object():
    clean, fig = llm._extract_figure('{"value":"5","label":"Y"}\n\nthe prose')
    assert fig == {"value": "5", "label": "Y"}, fig
    assert clean == "the prose", repr(clean)


# --- mis-fenced ![figure]/![chart] normalization (a live regression) -----------
# DeepSeek was observed emitting figures/charts as MARKDOWN IMAGE syntax
# (`![figure]\n{json}`, `![chart]\n{json}`) instead of ```figure/```chart fences.
# Nothing recognized that, so the raw JSON leaked into chat (and, where the retry
# separately recovered the figure, DUPLICATED it). Normalization rewrites the
# mis-wrap into a real fence before extraction.

def test_normalize_repairs_a_misfenced_figure_image():
    # The exact observed form: an image label on its own line, then bare JSON.
    ans = '![figure]\n{"value":"43.8%","label":"Public share"}\n\nProse follows.'
    fixed = llm._normalize_misfenced_blocks(ans)
    assert "```figure" in fixed, fixed
    # ...and it now extracts+strips cleanly, so nothing leaks.
    clean, fig = llm._extract_figure(fixed)
    assert fig == {"value": "43.8%", "label": "Public share"}, fig
    assert "![figure]" not in clean and '"value"' not in clean, repr(clean)


def test_normalize_repairs_a_misfenced_chart_with_nested_data():
    # A chart's JSON has nested arrays/objects — the balanced scan must span them.
    ans = ('![chart]\n{"type":"line","x":"year","data":[{"year":2024,"n":5},'
           '{"year":2025,"n":6}]}\n\ndone')
    fixed = llm._normalize_misfenced_blocks(ans)
    assert "```chart" in fixed and '"data"' in fixed, fixed
    assert "![chart]" not in fixed, fixed
    # The whole object was captured (nested braces balanced), prose preserved.
    assert fixed.rstrip().endswith("done"), fixed


def test_normalize_handles_the_image_link_variant():
    ans = '![Figure: 767](767)\n\n{"value":"767","label":"CS"}\n\ntail'
    fixed = llm._normalize_misfenced_blocks(ans)
    _, fig = llm._extract_figure(fixed)
    assert fig == {"value": "767", "label": "CS"}, fig


def test_normalize_leaves_a_real_image_and_real_fences_alone():
    # A genuine image (not followed by JSON) must be untouched...
    img = "See ![a nursing chart](https://x/y.png) above."
    assert llm._normalize_misfenced_blocks(img) == img
    # ...and a proper fence is already correct — no double-fencing.
    good = '```figure\n{"value":"5","label":"Y"}\n```'
    assert llm._normalize_misfenced_blocks(good) == good


def test_normalize_is_a_noop_without_a_figure_or_chart_label():
    txt = "Just prose with a number 42 and a {json-ish} brace."
    assert llm._normalize_misfenced_blocks(txt) == txt


def test_extract_figure_does_not_mistake_a_chart_fence_for_a_figure():
    ans = '```chart\n{"type":"line","x":"year","data":[{"year":2024,"n":5}]}\n```'
    clean, fig = llm._extract_figure(ans)
    assert fig is None, fig
    assert clean == ans, "a non-figure answer must be returned untouched"


def test_extract_figure_ignores_a_json_object_buried_in_prose():
    # Head-only: a bare object mid-sentence is NOT a mis-wrapped figure.
    ans = 'The answer is {"value":"5","label":"Y"} somewhere.'
    clean, fig = llm._extract_figure(ans)
    assert fig is None, fig
    assert clean == ans


def test_extract_figure_leaves_a_leading_non_figure_object_in_place():
    # A leading object lacking value/label is not a figure; don't strip it.
    ans = '{"note":"hi"}\n\nbody'
    clean, fig = llm._extract_figure(ans)
    assert fig is None, fig
    assert clean == ans


# --- missing-figure retry (_maybe_retry_figure / retry_missing_figure) ----------
# Structural recovery when a data answer that should lead with a figure emits none.
# Tests monkeypatch llm.retry_missing_figure (like the critic tests patch
# llm.critic.review), so they don't depend on FIGURE_RETRY_ENABLED or a live call.

def _numeric_answer_no_figure_chat(answer):
    """A fake_chat: one SQL round, then a numeric answer carrying NO figure."""
    calls = {"n": 0}

    async def fake_chat(client, model, messages, tools=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _sql_call_response("SELECT 1", "c1")
        return _text_response(answer)
    return fake_chat


def test_retry_recovers_a_grounded_figure():
    """The point of the whole change: a data answer with a number but no figure
    gets one back, and a retry figure the data supports is KEPT — tagged `retry:`
    so recovered figures are distinguishable from first-pass ones."""
    from app.tools.sql import QueryResult
    r = QueryResult(columns=["awards"], rows=[(400,)], row_count=1)
    llm._chat = _numeric_answer_no_figure_chat("Ohio awarded 400 degrees.")
    registry.dispatch = _sink_dispatch([r])

    async def fake_retry(question, answer, *a, **k):
        return llm._FigureRetry(figure={"value": "400", "label": "Awards"})
    orig = llm.retry_missing_figure
    llm.retry_missing_figure = fake_retry
    try:
        res = _run("how many awards?")
    finally:
        llm.retry_missing_figure = orig
    assert res.figure == {"value": "400", "label": "Awards"}, res.figure
    assert res.figure_grounding == "exact", res.figure_grounding
    assert res.figure_derivation.startswith("retry:"), res.figure_derivation


def test_retry_figure_that_cannot_be_grounded_is_suppressed():
    """The 'suppress' decision, pinned: a figure we FORCED that isn't in the data
    is a hallucination we induced — worse than the honest absence — so the user
    sees none, but the attempt is recorded so a flood is visible."""
    from app.tools.sql import QueryResult
    r = QueryResult(columns=["awards"], rows=[(400,)], row_count=1)
    llm._chat = _numeric_answer_no_figure_chat("Ohio awarded 400 degrees.")
    registry.dispatch = _sink_dispatch([r])

    async def fake_retry(question, answer, *a, **k):
        return llm._FigureRetry(figure={"value": "87,654", "label": "Awards"})
    orig = llm.retry_missing_figure
    llm.retry_missing_figure = fake_retry
    try:
        res = _run("how many awards?")
    finally:
        llm.retry_missing_figure = orig
    assert res.figure is None, "a forced, ungrounded figure must be suppressed"
    assert res.figure_grounding == "ungrounded", res.figure_grounding
    assert res.figure_derivation == "retry:suppressed", res.figure_derivation


def test_retry_does_not_fire_when_the_answer_has_no_number():
    """A plain lookup (no digit) legitimately needs no figure — the retry must not
    fire and spend a call on it. _figure_required IS the enumerable skip."""
    from app.tools.sql import QueryResult
    called = {"n": 0}

    async def fake_retry(question, answer, *a, **k):
        called["n"] += 1
        return llm._FigureRetry()
    llm._chat = _numeric_answer_no_figure_chat("The address is Main Street.")
    registry.dispatch = _sink_dispatch(
        [QueryResult(columns=["x"], rows=[("Main St",)], row_count=1)])
    orig = llm.retry_missing_figure
    llm.retry_missing_figure = fake_retry
    try:
        res = _run("what's the address?")
    finally:
        llm.retry_missing_figure = orig
    assert called["n"] == 0, "retry fired on a numberless answer"
    assert res.figure_grounding == "no_figure", res.figure_grounding


def test_first_pass_ungrounded_figure_still_ships_retry_not_invoked():
    """The suppress rule is scoped to RETRY figures only — it must not become a
    behavior change to #163, where a first-pass ungrounded figure ships
    (observe-only). And the retry must not run when a figure already exists."""
    from app.tools.sql import QueryResult
    r = QueryResult(columns=["awards"], rows=[(400,)], row_count=1)
    called = {"n": 0}

    async def fake_retry(question, answer, *a, **k):
        called["n"] += 1
        return llm._FigureRetry()

    async def fake_chat(client, model, messages, tools=None):
        if messages[-1]["role"] == "tool" or any(m.get("role") == "tool" for m in messages):
            return _text_response('Huge.\n\n```figure\n{"value":"87,654","label":"Awards"}\n```')
        return _sql_call_response("SELECT 1", "c1")
    llm._chat = fake_chat
    registry.dispatch = _sink_dispatch([r])
    orig = llm.retry_missing_figure
    llm.retry_missing_figure = fake_retry
    try:
        res = _run("q")
    finally:
        llm.retry_missing_figure = orig
    assert res.figure == {"value": "87,654", "label": "Awards"}, res.figure
    assert res.figure_grounding == "ungrounded", res.figure_grounding
    assert called["n"] == 0, "retry must not run when a first-pass figure exists"


def test_retry_missing_figure_fails_open_on_transport_error():
    """Like critic.review, a retry outage must never break a finished answer."""
    import app.llm as _llm

    async def boom(*a, **k):
        raise httpx.HTTPError("provider down")
    orig = _llm.chat_completion
    _llm.chat_completion = boom
    # Force the setting on for this one call so we exercise the network path
    # (ci_env pins FIGURE_RETRY_ENABLED=false, which would otherwise short-circuit
    # before the transport call we're testing).
    s = get_settings()
    prev = s.figure_retry_enabled
    s.figure_retry_enabled = True
    try:
        out = asyncio.run(_llm.retry_missing_figure("q", "Ohio awarded 400 degrees."))
    finally:
        _llm.chat_completion = orig
        s.figure_retry_enabled = prev
    assert out.figure is None, out
    assert out.prompt_tokens == 0 and out.cost == 0, out


def test_clarify_absent_on_a_normal_answer():
    async def fake_chat(client, model, messages, tools=None):
        return _text_response("California awarded 7,679 CS bachelor's degrees.")
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    res = _run("q")
    assert res.clarify is None, res.clarify


# --- structured emission (config.structured_emission_enabled) ------------------
# The model FINISHES via emit_answer/ask_clarification tools instead of fences;
# the server reconstructs WELL-FORMED fences from the validated args, so the whole
# extraction/critic/grounding tail runs unchanged and nothing is manglea​ble.

def _emit_call(name, args):
    return {"choices": [{"message": {"content": "",
                "tool_calls": [{"id": "e1", "type": "function",
                    "function": {"name": name,
                                 "arguments": json.dumps(args)}}]}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _run_structured(question, fake_chat):
    s = get_settings()
    prev = s.structured_emission_enabled
    s.structured_emission_enabled = True
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    try:
        return _run(question)
    finally:
        s.structured_emission_enabled = prev


def test_reconstruct_answer_builds_well_formed_fences():
    call = {"function": {"name": "emit_answer", "arguments": json.dumps({
        "markdown": "Ohio led with 400.",
        "figure": {"value": "400", "label": "Awards"},
        "chart": {"type": "line", "x": "year", "data": [{"year": 2024, "n": 5}]},
        "followups": ["Compare to Texas?"]})}}
    out = llm._reconstruct_answer(call)
    # Server-written fences → the existing extractors parse them cleanly.
    _, fig = llm._extract_figure(out)
    assert fig == {"value": "400", "label": "Awards"}, fig
    assert "```chart" in out and '"data"' in out, out
    _, sug = llm._extract_suggestions(out)
    assert sug == ["Compare to Texas?"], sug
    assert "Ohio led with 400." in out


def test_reconstruct_ask_clarification():
    call = {"function": {"name": "ask_clarification", "arguments": json.dumps({
        "question": "Which award level?", "options": ["Bachelor's", "All levels"]})}}
    out = llm._reconstruct_answer(call)
    _, clar = llm._extract_clarify(out)
    assert clar == {"question": "Which award level?",
                    "options": ["Bachelor's", "All levels"]}, clar


def test_leak_flag_detects_residual_debris():
    assert llm._leak_flag('lead\n{"value":"5","label":"x"} trail') is True
    assert llm._leak_flag("```figure\n{}\n```") is True   # should've been extracted
    assert llm._leak_flag("![figure] {}") is True          # a mangled image form
    assert llm._leak_flag("A clean prose answer with a number 511.") is False
    # A plain ```chart fence is the INTENDED chart delivery — never a leak...
    assert llm._leak_flag('```chart\n{"type":"line","data":[{"y":1}]}\n```') is False, \
        "a chart fence is delivered to the frontend by design, not debris"
    # ...but a mangled ![chart] image form (the observed #167 leak) IS flagged.
    assert llm._leak_flag('![chart] {"type":"line"}') is True


def test_structured_emit_answer_maps_figure_and_followups():
    calls = {"n": 0}

    async def fake_chat(client, model, messages, tools=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _sql_call_response("SELECT 1", "c1")
        return _emit_call("emit_answer", {
            "markdown": "Ohio led with **400** degrees.",
            "figure": {"value": "400", "label": "Awards"},
            "followups": ["Compare to Texas?", "Which programs?"]})
    res = _run_structured("q", fake_chat)
    assert res.emit_mode == "structured", res.emit_mode
    assert res.figure == {"value": "400", "label": "Awards"}, res.figure
    assert res.suggestions == ["Compare to Texas?", "Which programs?"], res.suggestions
    assert "Ohio led with **400** degrees." in res.answer, res.answer
    # No raw JSON / fence leaked into the shipped prose.
    assert '"value"' not in res.answer and "```figure" not in res.answer, res.answer
    assert res.leaked is False, "a clean structured answer must not flag a leak"


def test_structured_emit_answer_chart_becomes_a_server_written_fence():
    async def fake_chat(client, model, messages, tools=None):
        return _emit_call("emit_answer", {
            "markdown": "Trend below.",
            "chart": {"type": "line", "x": "year", "y": "n",
                      "data": [{"year": 2024, "n": 5}, {"year": 2025, "n": 6}]}})
    res = _run_structured("q", fake_chat)
    # Chart passes to the frontend as a normal ```chart fence — server-written,
    # so always well-formed. Frontend contract unchanged.
    assert "```chart" in res.answer, res.answer
    assert '"data"' in res.answer, res.answer


def test_structured_ask_clarification_yields_a_clarify_turn():
    async def fake_chat(client, model, messages, tools=None):
        return _emit_call("ask_clarification", {
            "question": "Which award level?", "options": ["Bachelor's", "All"]})
    res = _run_structured("q", fake_chat)
    assert res.clarify == {"question": "Which award level?",
                           "options": ["Bachelor's", "All"]}, res.clarify
    assert res.figure is None and res.suggestions is None, "clarify carries neither"


def test_fence_path_records_emit_mode_fence():
    # With structured emission OFF (the default), a normal answer stays on the
    # fence path and is recorded as such — the telemetry's baseline.
    async def fake_chat(client, model, messages, tools=None):
        return _text_response("Plain answer, 42.")
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    assert _run("q").emit_mode == "fence"


# --- adoption nudge (0.1): reject-and-reprompt a free-typed answer -------------

def test_structured_reprompt_converts_a_free_typer():
    """THE adoption lever: under structured mode, a plain-text answer is rejected
    and the model is nudged to call emit_answer — after which the turn is
    structured. Also checks the _EMIT_REPROMPT message actually reached it."""
    calls = {"n": 0, "saw_reprompt": False}

    async def fake_chat(client, model, messages, tools=None):
        calls["n"] += 1
        if any(m.get("content") == llm._EMIT_REPROMPT for m in messages):
            calls["saw_reprompt"] = True
        if calls["n"] == 1:
            return _text_response("Ohio led with 400 degrees.")  # free-typed
        return _emit_call("emit_answer", {"markdown": "Ohio led with **400**.",
                                          "figure": {"value": "400", "label": "Awards"}})
    res = _run_structured("q", fake_chat)
    assert calls["saw_reprompt"], "the nudge message must be sent to the model"
    assert res.emit_mode == "structured", res.emit_mode
    assert res.figure == {"value": "400", "label": "Awards"}, res.figure
    assert "Ohio led with **400**." in res.answer, res.answer


def test_structured_reprompt_fires_only_once_then_falls_back():
    """Bounded: a model that ignores the nudge and free-types AGAIN is accepted on
    the fence path — the reprompt must not loop."""
    calls = {"n": 0}

    async def fake_chat(client, model, messages, tools=None):
        calls["n"] += 1
        return _text_response("Stubborn plain-text answer, 42.")  # always free-types
    res = _run_structured("q", fake_chat)
    assert res.emit_mode == "fence", "a second free-type falls back to the fence path"
    assert "Stubborn plain-text answer" in res.answer, res.answer
    assert calls["n"] == 2, f"exactly one reprompt round (2 calls), got {calls['n']}"


def test_structured_reprompt_nudges_a_free_typed_clarify():
    """The nudge precedes the clarify check, so a free-typed clarify is pushed
    onto ask_clarification."""
    calls = {"n": 0}

    async def fake_chat(client, model, messages, tools=None):
        calls["n"] += 1
        if calls["n"] == 1:  # free-typed clarify fence
            return _text_response('Which level?\n\n```clarify\n'
                                  '{"question":"Which level?","options":["BA","All"]}\n```')
        return _emit_call("ask_clarification",
                          {"question": "Which level?", "options": ["BA", "All"]})
    res = _run_structured("q", fake_chat)
    assert res.clarify == {"question": "Which level?", "options": ["BA", "All"]}, res.clarify


def test_no_reprompt_when_structured_off():
    # The default: a free-typed answer is accepted as-is, no nudge.
    calls = {"n": 0}

    async def fake_chat(client, model, messages, tools=None):
        calls["n"] += 1
        return _text_response("Plain answer, 42.")
    llm._chat = fake_chat
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    res = _run("q")
    assert calls["n"] == 1, "structured OFF → no reprompt round"
    assert res.emit_mode == "fence"


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
    check("S5: exhaustion critic correction re-queries and changes → ships revised",
          test_exhaustion_critic_correction_requeries_and_changes)
    check("S5: exhaustion correction confirming same answer reverts to draft",
          test_exhaustion_correction_confirming_same_answer_reverts_to_draft)
    check("S5: exhaustion rebuttal without re-query reverts to draft (leak gate)",
          test_exhaustion_correction_rebuttal_without_requery_reverts_to_draft)
    check("S5: exhaustion critic OK ships the draft unchanged",
          test_exhaustion_critic_ok_ships_draft_unchanged)
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
    check("_reconstruct_answer builds well-formed fences from emit_answer args",
          test_reconstruct_answer_builds_well_formed_fences)
    check("_reconstruct_answer builds a clarify fence from ask_clarification",
          test_reconstruct_ask_clarification)
    check("_leak_flag detects residual fence/JSON debris",
          test_leak_flag_detects_residual_debris)
    check("structured emit_answer maps figure + followups (no leak)",
          test_structured_emit_answer_maps_figure_and_followups)
    check("structured emit_answer chart → a server-written ```chart fence",
          test_structured_emit_answer_chart_becomes_a_server_written_fence)
    check("structured ask_clarification yields a clarify turn",
          test_structured_ask_clarification_yields_a_clarify_turn)
    check("the fence path records emit_mode='fence' (telemetry baseline)",
          test_fence_path_records_emit_mode_fence)
    check("structured reprompt converts a free-typer to emit_answer",
          test_structured_reprompt_converts_a_free_typer)
    check("structured reprompt fires only once, then falls back",
          test_structured_reprompt_fires_only_once_then_falls_back)
    check("structured reprompt nudges a free-typed clarify",
          test_structured_reprompt_nudges_a_free_typed_clarify)
    check("no reprompt when structured emission is off",
          test_no_reprompt_when_structured_off)
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
    check("a follow-up gets a tail reminder, after the cached prefix",
          test_followup_turn_gets_a_tail_reminder_after_the_cached_prefix)
    check("a first turn gets no tail reminder",
          test_first_turn_gets_no_tail_reminder)
    check("an unparseable figure fence is 'malformed', not 'no_figure'",
          test_an_unparseable_figure_fence_is_malformed_not_no_figure)
    check("an answer with no figure is not measured",
          test_an_answer_with_no_figure_is_not_measured)
    check("a recited figure grounds against a prior turn (ctx:)",
          test_a_recited_figure_grounds_against_a_prior_turn)
    check("the retry fires on a no-SQL turn using prior results",
          test_retry_fires_on_a_no_sql_turn_using_prior_results)
    check("_figure_required relaxes only when prior results exist",
          test_figure_required_relaxes_only_when_prior_results_exist)
    check("current-turn results take precedence over prior",
          test_current_turn_results_take_precedence_over_prior)
    check("normalize repairs a mis-fenced ![figure] image",
          test_normalize_repairs_a_misfenced_figure_image)
    check("normalize repairs a mis-fenced ![chart] with nested data",
          test_normalize_repairs_a_misfenced_chart_with_nested_data)
    check("normalize handles the ![Figure](url) link variant",
          test_normalize_handles_the_image_link_variant)
    check("normalize leaves a real image and real fences alone",
          test_normalize_leaves_a_real_image_and_real_fences_alone)
    check("normalize is a no-op without a figure/chart label",
          test_normalize_is_a_noop_without_a_figure_or_chart_label)
    check("_extract_figure recovers the observed broken wrapper",
          test_extract_figure_recovers_the_observed_broken_wrapper)
    check("_extract_figure recovers a leading bare object",
          test_extract_figure_recovers_a_leading_bare_object)
    check("_extract_figure does not mistake a chart fence for a figure",
          test_extract_figure_does_not_mistake_a_chart_fence_for_a_figure)
    check("_extract_figure ignores a JSON object buried in prose",
          test_extract_figure_ignores_a_json_object_buried_in_prose)
    check("_extract_figure leaves a leading non-figure object in place",
          test_extract_figure_leaves_a_leading_non_figure_object_in_place)
    check("retry recovers a grounded figure (tagged retry:)",
          test_retry_recovers_a_grounded_figure)
    check("a retry figure that can't be grounded is suppressed",
          test_retry_figure_that_cannot_be_grounded_is_suppressed)
    check("retry does not fire when the answer has no number",
          test_retry_does_not_fire_when_the_answer_has_no_number)
    check("a first-pass ungrounded figure still ships; retry not invoked",
          test_first_pass_ungrounded_figure_still_ships_retry_not_invoked)
    check("retry_missing_figure fails open on a transport error",
          test_retry_missing_figure_fails_open_on_transport_error)
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
