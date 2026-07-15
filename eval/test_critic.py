"""Post-answer critic contract (app/critic.py) + its wiring into the agent loop.

- parse_verdict reads the reviewer STRICTLY-toward-OK: revise only on an explicit
  REVISE, everything ambiguous/empty is OK (don't disturb a good draft).
- review() fails OPEN (ok=True) when disabled, unconfigured, or on a transport
  error, so the critic can never drop or block an answer.
- In stream_agent: a REVISE verdict drives exactly ONE revision round and the
  corrected answer is returned; an OK verdict returns the draft untouched; the
  critic runs at most once and never on a no-SQL (refusal) answer.
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
    # the revision message must have been fed back before the final answer
    assert calls["chat"] == 3, calls


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
