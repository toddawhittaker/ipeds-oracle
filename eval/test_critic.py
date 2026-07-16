"""Post-answer critic contract (app/critic.py) + its wiring into the agent loop.

- parse_verdict reads the reviewer STRICTLY-toward-OK: revise only on an explicit
  REVISE, everything ambiguous/empty is OK (don't disturb a good draft).
- review() fails OPEN (ok=True) when disabled, unconfigured, or on a transport
  error, so the critic can never drop or block an answer.
- In stream_agent: a REVISE verdict drives exactly ONE revision round; an OK
  verdict returns the draft untouched; the critic runs at most once and never
  on a no-SQL (refusal) answer.
- The revision round is judged by whether the model ran a NEW run_sql after
  the critique: if so, its new answer is a genuine correction and streams
  through with critic_revised=True; if it just argues back with no new SQL,
  the loop discards that rebuttal and re-emits the ORIGINAL pre-critique draft
  verbatim with critic_revised=False (no leaked reviewer meta-commentary, no
  spurious lesson).
"""
import asyncio
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Set before app.config import (settings are cached).
os.environ["OPENROUTER_API_KEY"] = "test-key"
os.environ["LLM_MAX_TOOL_ITERS"] = "6"

import httpx  # noqa: E402

from app import critic, llm  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.critic import Critique, parse_verdict  # noqa: E402
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


# --- parse_verdict -------------------------------------------------------------

def test_parse_ok():
    ok, issue = parse_verdict("OK")
    assert ok is True and issue == "", (ok, issue)


def test_parse_ok_lowercase_and_noise():
    assert parse_verdict("ok, this looks correct")[0] is True
    assert parse_verdict("The answer is sound.")[0] is True


def test_parse_empty_is_ok():
    # a garbled/empty reply must not disturb the draft
    assert parse_verdict("")[0] is True
    assert parse_verdict("   ")[0] is True


def test_parse_revise_extracts_reason():
    ok, issue = parse_verdict(
        "REVISE: cipcode LIKE '51.%' double-counts; use an exact 6-digit code")
    assert ok is False, ok
    assert "double-counts" in issue and issue.lower().startswith("cipcode"), issue


def test_parse_revise_case_insensitive():
    ok, issue = parse_verdict("revise: magnitude looks 4x too high")
    assert ok is False and "magnitude" in issue, (ok, issue)


def test_parse_revise_without_colon_gets_default_issue():
    ok, issue = parse_verdict("REVISE")
    assert ok is False and issue, (ok, issue)


# --- build_review_messages / revision_instruction ------------------------------

def test_system_prompt_asks_for_a_readable_self_contained_revise_explanation():
    # The REVISE explanation is stored as a lesson AND shown to admins, so the
    # prompt must ask for plain-English prose, not cryptic shorthand. Kept
    # minimal — pinning stable substrings, not the exact wording.
    assert "self-contained" in critic._SYSTEM, critic._SYSTEM
    assert "not cryptic shorthand" in critic._SYSTEM, critic._SYSTEM
    assert "reusable rule" in critic._SYSTEM, critic._SYSTEM


def test_build_messages_includes_artifacts():
    msgs = critic.build_review_messages(
        "How many nursing degrees?",
        ["SELECT SUM(ctotalt) FROM c_a WHERE cipcode='51.3801'"],
        "Ohio awarded 12,345 nursing degrees.")
    assert msgs[0]["role"] == "system", msgs
    user = msgs[1]["content"]
    assert "How many nursing degrees?" in user
    assert "51.3801" in user
    assert "12,345" in user


def test_build_messages_truncates_long_answer():
    long_answer = "x" * 5000
    user = critic.build_review_messages("q", ["SELECT 1"], long_answer)[1]["content"]
    # answer is capped well under its raw length
    assert user.count("x") <= 2000, user.count("x")


def test_revision_instruction_carries_issue():
    msg = critic.revision_instruction("magnitude 4x too high")
    assert "magnitude 4x too high" in msg
    assert "run_sql" in msg  # tells the model it may re-query


# --- review(): fail-open + live transport --------------------------------------

def test_review_fails_open_without_key():
    orig = critic.get_settings
    critic.get_settings = lambda: types.SimpleNamespace(
        critic_enabled=True, openrouter_api_key="")
    try:
        c = asyncio.run(critic.review("q", ["SELECT 1"], "ans"))
    finally:
        critic.get_settings = orig
    assert c.ok is True, "no key must fail open"


def test_review_disabled_fails_open():
    orig = critic.get_settings
    critic.get_settings = lambda: types.SimpleNamespace(
        critic_enabled=False, openrouter_api_key="test-key")
    try:
        c = asyncio.run(critic.review("q", ["SELECT 1"], "ans"))
    finally:
        critic.get_settings = orig
    assert c.ok is True, "disabled critic must fail open"


def _configured(**overrides):
    base = dict(critic_enabled=True, openrouter_api_key="test-key",
                model_default="deepseek/deepseek-v4-flash",
                openrouter_base_url="https://openrouter.ai/api/v1",
                app_public_url="http://localhost:8000", app_title="IPEDS Query")
    base.update(overrides)
    return types.SimpleNamespace(**base)


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
    orig_settings, orig_client = critic.get_settings, critic.httpx.AsyncClient
    critic.get_settings = _configured
    critic.httpx.AsyncClient = lambda *a, **k: _FakeAsyncClient(item)
    try:
        return fn()
    finally:
        critic.get_settings = orig_settings
        critic.httpx.AsyncClient = orig_client


def test_review_ok_verdict_live():
    resp = _json_response({
        "choices": [{"message": {"content": "OK"}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 1, "cost": 0.0002},
    })
    c = _with_fake_transport(
        resp, lambda: asyncio.run(critic.review("q", ["SELECT 1"], "ans")))
    assert c.ok is True, c
    assert c.prompt_tokens == 40 and c.completion_tokens == 1, c
    assert c.cost == 0.0002, c


def test_review_revise_verdict_live():
    resp = _json_response({
        "choices": [{"message": {"content": "REVISE: no majornum=1, double count"}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 8},
    })
    c = _with_fake_transport(
        resp, lambda: asyncio.run(critic.review("q", ["SELECT SUM(ctotalt) FROM c_a"], "ans")))
    assert c.ok is False, c
    assert "majornum" in c.issue, c.issue


def test_review_transport_error_fails_open():
    c = _with_fake_transport(
        httpx.ConnectError("refused"),
        lambda: asyncio.run(critic.review("q", ["SELECT 1"], "ans")))
    assert c.ok is True, "transport error must fail open"


# --- agent-loop integration ----------------------------------------------------

def _run(question):
    return asyncio.run(llm.run_agent(question))


def _restore():
    # tests reassign these module globals; reset between cases
    pass


def test_ok_verdict_returns_draft_unchanged():
    calls = {"chat": 0, "critic": 0}

    async def fake_chat(client, model, messages, tools=None):
        calls["chat"] += 1
        if calls["chat"] == 1:  # first: run a query so sql_log is populated
            return {"choices": [{"message": {"content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "run_sql", "arguments": '{"sql": "SELECT 1"}'}}]}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        return {"choices": [{"message": {"content": "Final: 1,234."}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    async def fake_review(question, sql_log, answer):
        calls["critic"] += 1
        return Critique(ok=True)

    llm._chat = fake_chat
    llm.critic.review = fake_review
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    try:
        res = _run("q")
    finally:
        llm.critic.review = critic.review
    assert res.answer == "Final: 1,234.", res.answer
    assert res.critic_revised is False, res.critic_revised
    assert calls["critic"] == 1, "critic should run once on a SQL-backed answer"


def test_revise_verdict_triggers_one_revision():
    # Genuine correction: critic flags the draft, the model re-runs SQL, THEN
    # answers. Sequence: (1) run_sql -> (2) draft -> critic REVISE ->
    # (3) run_sql again -> (4) "Corrected: 1,000,000."
    calls = {"chat": 0, "critic": 0}

    async def fake_chat(client, model, messages, tools=None):
        calls["chat"] += 1
        if calls["chat"] == 1:
            return {"choices": [{"message": {"content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "run_sql", "arguments": '{"sql": "SELECT SUM(ctotalt) FROM c_a"}'}}]}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        if calls["chat"] == 2:
            return {"choices": [{"message": {"content": "Draft: 4,000,000 (wrong)."}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        if calls["chat"] == 3:  # revision round: the model re-queries, no answer yet
            return {"choices": [{"message": {"content": "",
                "tool_calls": [{"id": "c2", "type": "function", "function": {
                    "name": "run_sql",
                    "arguments": '{"sql": "SELECT SUM(ctotalt) FROM c_a WHERE majornum=1"}'
                }}]}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        return {"choices": [{"message": {"content": "Corrected: 1,000,000."}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    async def fake_review(question, sql_log, answer):
        calls["critic"] += 1
        return Critique(ok=False, issue="missing majornum=1; ~4x overcount")

    llm._chat = fake_chat
    llm.critic.review = fake_review
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    try:
        res = _run("q")
    finally:
        llm.critic.review = critic.review
    assert res.answer == "Corrected: 1,000,000.", res.answer
    assert res.critic_revised is True, res.critic_revised
    assert calls["critic"] == 1, "critic must run at most once per turn"
    # run_sql, draft, revision-round run_sql, corrected answer
    assert calls["chat"] == 4, calls


def test_rebuttal_without_new_sql_reemits_clean_draft():
    # Core regression test for the critic-revision leak: the model argues back
    # instead of re-querying after a REVISE verdict. The loop must discard the
    # rebuttal and re-emit the ORIGINAL clean draft, with critic_revised False
    # so no spurious lesson gets recorded downstream.
    calls = {"chat": 0, "critic": 0}
    clean_draft = "Franklin awarded 2,183 in 2024."
    rebuttal = ("The reviewer's concern is understandable but does not apply here. "
                "I verified from the survey dictionary that cstotlt is correct.")

    async def fake_chat(client, model, messages, tools=None):
        calls["chat"] += 1
        if calls["chat"] == 1:
            return {"choices": [{"message": {"content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "run_sql",
                    "arguments": '{"sql": "SELECT SUM(cstotlt) FROM ic_a"}'}}]}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        if calls["chat"] == 2:
            return {"choices": [{"message": {"content": clean_draft}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        # revision round: no tool call, just a rebuttal
        return {"choices": [{"message": {"content": rebuttal}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    async def fake_review(question, sql_log, answer):
        calls["critic"] += 1
        return Critique(ok=False, issue="cstotlt may overcount")

    llm._chat = fake_chat
    llm.critic.review = fake_review
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    try:
        res = _run("q")
    finally:
        llm.critic.review = critic.review
    assert res.answer == clean_draft, res.answer
    assert res.critic_revised is False, res.critic_revised
    lowered = res.answer.lower()
    for leak in ("reviewer", "i verified", "does not apply"):
        assert leak not in lowered, (leak, res.answer)


def test_requery_confirming_same_answer_is_not_a_revision():
    # Finding 2 (verify-by-requery false alarm): the critic flags a false alarm,
    # the model DOES re-run SQL (sql_log grows) but the re-query only confirms
    # the original number. That's "re-queried and confirmed," not a correction,
    # so critic_revised must stay False and the answer must be the same draft
    # text (no spurious lesson recorded downstream).
    calls = {"chat": 0, "critic": 0}
    draft = "Ohio awarded 12,345 nursing degrees."

    async def fake_chat(client, model, messages, tools=None):
        calls["chat"] += 1
        if calls["chat"] == 1:
            return {"choices": [{"message": {"content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "run_sql",
                    "arguments": '{"sql": "SELECT SUM(ctotalt) FROM c_a WHERE cipcode='
                                 '\'51.3801\' AND stabbr=\'OH\'"}'}}]}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        if calls["chat"] == 2:
            return {"choices": [{"message": {"content": draft}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        if calls["chat"] == 3:  # revision round: model re-queries to double-check
            return {"choices": [{"message": {"content": "",
                "tool_calls": [{"id": "c2", "type": "function", "function": {
                    "name": "run_sql",
                    "arguments": '{"sql": "SELECT SUM(ctotalt) FROM c_a WHERE cipcode='
                                 '\'51.3801\' AND stabbr=\'OH\' AND majornum=1"}'}}]}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        # confirms the same number, verbatim
        return {"choices": [{"message": {"content": draft}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    async def fake_review(question, sql_log, answer):
        calls["critic"] += 1
        return Critique(ok=False, issue="magnitude looks high")

    llm._chat = fake_chat
    llm.critic.review = fake_review
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    try:
        res = _run("q")
    finally:
        llm.critic.review = critic.review
    assert res.answer == draft, res.answer
    assert res.critic_revised is False, res.critic_revised
    assert calls["critic"] == 1, "critic must run at most once per turn"
    # run_sql, draft, revision-round run_sql, confirmed (same) answer
    assert calls["chat"] == 4, calls


def test_revision_message_reaches_the_model():
    captured = {"msgs": None}
    calls = {"chat": 0}

    async def fake_chat(client, model, messages, tools=None):
        calls["chat"] += 1
        if calls["chat"] == 1:
            return {"choices": [{"message": {"content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "run_sql", "arguments": '{"sql": "SELECT 1"}'}}]}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        if calls["chat"] == 2:
            return {"choices": [{"message": {"content": "draft"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        captured["msgs"] = list(messages)
        return {"choices": [{"message": {"content": "revised"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    async def fake_review(question, sql_log, answer):
        return Critique(ok=False, issue="use cipcode='99' for the national total")

    llm._chat = fake_chat
    llm.critic.review = fake_review
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    try:
        _run("q")
    finally:
        llm.critic.review = critic.review
    joined = " ".join(m.get("content") or "" for m in captured["msgs"])
    assert "reviewer flagged" in joined and "cipcode='99'" in joined, joined


def test_no_sql_answer_skips_critic():
    calls = {"critic": 0}

    async def fake_chat(client, model, messages, tools=None):
        return {"choices": [{"message": {"content": "I can only help with IPEDS data."}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    async def fake_review(question, sql_log, answer):
        calls["critic"] += 1
        return Critique(ok=False, issue="should not be called")

    llm._chat = fake_chat
    llm.critic.review = fake_review
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    try:
        res = _run("hi")
    finally:
        llm.critic.review = critic.review
    assert res.answer == "I can only help with IPEDS data.", res.answer
    assert calls["critic"] == 0, "critic must not run when no SQL was executed"


def run():
    print("post-answer critic:")
    check("parse OK", test_parse_ok)
    check("parse OK with lowercase/noise", test_parse_ok_lowercase_and_noise)
    check("parse empty/garbled -> OK", test_parse_empty_is_ok)
    check("parse REVISE extracts the reason", test_parse_revise_extracts_reason)
    check("parse REVISE is case-insensitive", test_parse_revise_case_insensitive)
    check("parse bare REVISE gets a default issue",
          test_parse_revise_without_colon_gets_default_issue)
    check("_SYSTEM asks for a readable, self-contained REVISE explanation",
          test_system_prompt_asks_for_a_readable_self_contained_revise_explanation)
    check("build_review_messages includes question/SQL/answer",
          test_build_messages_includes_artifacts)
    check("build_review_messages truncates a long answer",
          test_build_messages_truncates_long_answer)
    check("revision_instruction carries the issue",
          test_revision_instruction_carries_issue)
    check("review fails open without a key", test_review_fails_open_without_key)
    check("review fails open when disabled", test_review_disabled_fails_open)
    check("review OK verdict (live transport)", test_review_ok_verdict_live)
    check("review REVISE verdict (live transport)", test_review_revise_verdict_live)
    check("review transport error fails open", test_review_transport_error_fails_open)
    check("OK verdict returns the draft unchanged",
          test_ok_verdict_returns_draft_unchanged)
    check("REVISE verdict triggers exactly one revision",
          test_revise_verdict_triggers_one_revision)
    check("a rebuttal with no new SQL re-emits the clean draft (leak regression)",
          test_rebuttal_without_new_sql_reemits_clean_draft)
    check("a requery confirming the same answer is not a revision",
          test_requery_confirming_same_answer_is_not_a_revision)
    check("the revision message reaches the model",
          test_revision_message_reaches_the_model)
    check("a no-SQL (refusal) answer skips the critic",
          test_no_sql_answer_skips_critic)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} critic test(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL CRITIC TESTS PASSED")


if __name__ == "__main__":
    run()
