"""Post-answer critic contract (backend/app/critic.py) + its wiring into the agent loop.

- parse_verdict reads the reviewer STRICTLY-toward-OK: revise only on an explicit
  REVISE, everything ambiguous/empty is OK (don't disturb a good draft). A
  REVISE verdict now carries a STRUCTURED headline + description (HEADLINE:/
  DESCRIPTION: labels, parsed case-insensitively); an unlabeled REVISE falls
  back to a truncated headline + the whole remainder as the description; a bare
  REVISE with nothing after it still fails toward a non-empty fallback. Never
  throws.
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
os.environ["LLM_API_KEY"] = "test-key"
os.environ["LLM_MAX_TOOL_ITERS"] = "6"

import httpx  # noqa: E402

from app import critic, llm, llmhttp  # noqa: E402
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
    ok, headline, description = parse_verdict("OK")
    assert ok is True and headline == "" and description == "", (ok, headline, description)


def test_parse_ok_lowercase_and_noise():
    assert parse_verdict("ok, this looks correct")[0] is True
    assert parse_verdict("The answer is sound.")[0] is True


def test_parse_empty_is_ok():
    # a garbled/empty reply must not disturb the draft
    assert parse_verdict("") == (True, "", "")
    assert parse_verdict("   ") == (True, "", "")


def test_parse_structured_revise_extracts_headline_and_description():
    reply = (
        "REVISE\n"
        "HEADLINE: Filter on an exact 6-digit CIP code, not a rollup.\n"
        "DESCRIPTION: cipcode LIKE '51.%' sums the 2-/4-/6-digit rollup rows "
        "together with the leaf code, overcounting; match the exact 6-digit "
        "code instead."
    )
    ok, headline, description = parse_verdict(reply)
    assert ok is False, ok
    assert headline == "Filter on an exact 6-digit CIP code, not a rollup.", headline
    assert description.startswith("cipcode LIKE '51.%'"), description


def test_parse_structured_revise_labels_are_case_insensitive():
    reply = ("revise\nheadline: Use majornum=1.\n"
             "description: Second majors double-count otherwise.")
    ok, headline, description = parse_verdict(reply)
    assert ok is False, ok
    assert headline == "Use majornum=1.", headline
    assert description == "Second majors double-count otherwise.", description


def test_parse_structured_revise_is_order_independent():
    # DESCRIPTION before HEADLINE must still parse cleanly: the DESCRIPTION
    # capture must stop at the next label instead of swallowing it.
    reply = (
        "REVISE\n"
        "DESCRIPTION: use cipcode='99' for national totals\n"
        "HEADLINE: prefer the grand-total row"
    )
    ok, headline, description = parse_verdict(reply)
    assert ok is False, ok
    assert headline == "prefer the grand-total row", headline
    assert "HEADLINE" not in description, \
        f"description must not swallow the following HEADLINE label: {description!r}"
    assert description == "use cipcode='99' for national totals", description


def test_parse_malformed_revise_falls_back_to_description_and_truncated_headline():
    blob = ("cipcode LIKE '51.%' double-counts because the rollup rows resum the "
            "same total as the leaf 6-digit code; match the exact code instead of "
            "a prefix.")
    ok, headline, description = parse_verdict(f"REVISE: {blob}")
    assert ok is False, ok
    assert description == blob, description
    assert headline, "an unlabeled REVISE must still produce a non-empty headline"
    assert len(headline) <= 90, f"headline should be truncated to ~80 chars: {headline!r}"
    assert len(headline) < len(description), \
        "the fallback headline must be a shorter truncation, not the whole description"
    assert blob.startswith(headline.rstrip()), \
        f"the fallback headline must be a prefix of the description, got {headline!r}"


def test_parse_bare_revise_gets_a_nonempty_fallback():
    ok, headline, description = parse_verdict("REVISE")
    assert ok is False, ok
    assert headline and description, (headline, description)


def test_parse_never_throws_on_garbage():
    for garbage in (None, 12345, "REVISE\nHEADLINE\nDESCRIPTION", "REVISE:", "revise:::::"):
        try:
            parse_verdict(garbage)  # noqa: F841 -- just must not raise
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"parse_verdict raised on {garbage!r}: {e}") from e


# --- _SYSTEM / build_review_messages / revision_instruction --------------------

def test_system_prompt_pins_key_substrings():
    # Only the labels the output parser consumes are load-bearing: a prompt
    # reworded to emit anything other than HEADLINE/DESCRIPTION would silently
    # break parse_verdict. (Prose wording of the rest of the prompt is free to
    # change without a behavior regression, so it isn't pinned here.)
    for s in ("HEADLINE", "DESCRIPTION"):
        assert s in critic._SYSTEM, (s, critic._SYSTEM)


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


def test_revision_instruction_carries_headline_and_description():
    msg = critic.revision_instruction(
        "Magnitude looks 4x too high.", "Because CIP rollups double-count, filter exactly.")
    assert "Magnitude looks 4x too high." in msg
    assert "Because CIP rollups double-count, filter exactly." in msg
    assert "run_sql" in msg  # tells the model it may re-query


def test_revision_instruction_keeps_anti_leak_hardening():
    msg = critic.revision_instruction("H", "D")
    assert "Output ONLY the final answer" in msg, msg
    assert "Never mention the reviewer" in msg, msg


# --- review(): fail-open + live transport --------------------------------------

def test_review_fails_open_without_key():
    orig = critic.get_settings
    critic.get_settings = lambda: types.SimpleNamespace(
        critic_enabled=True, llm_api_key="")
    try:
        c = asyncio.run(critic.review("q", ["SELECT 1"], "ans"))
    finally:
        critic.get_settings = orig
    assert c.ok is True, "no key must fail open"


def test_review_disabled_fails_open():
    orig = critic.get_settings
    critic.get_settings = lambda: types.SimpleNamespace(
        critic_enabled=False, llm_api_key="test-key")
    try:
        c = asyncio.run(critic.review("q", ["SELECT 1"], "ans"))
    finally:
        critic.get_settings = orig
    assert c.ok is True, "disabled critic must fail open"


def _configured(**overrides):
    base = dict(critic_enabled=True, llm_api_key="test-key",
                model_default="deepseek/deepseek-v4-flash",
                llm_base_url="https://openrouter.ai/api/v1",
                app_public_url="http://localhost:8000", llm_app_title="IPEDS Query")
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _FakeAsyncClient:
    """Records every call's url/json/headers/timeout on the class-level
    `calls` list (reset by `_with_fake_transport`), so PROBE_TIMEOUT and the
    request URL/headers are actually verified, not just ignored."""

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
    assert c.headline == "" and c.description == "", c
    assert c.prompt_tokens == 40 and c.completion_tokens == 1, c
    assert c.cost == 0.0002, c


def test_review_revise_verdict_live():
    resp = _json_response({
        "choices": [{"message": {"content":
            "REVISE\nHEADLINE: Add majornum=1.\n"
            "DESCRIPTION: no majornum=1 filter, double counts second majors."}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 8},
    })
    c = _with_fake_transport(
        resp, lambda: asyncio.run(critic.review("q", ["SELECT SUM(ctotalt) FROM c_a"], "ans")))
    assert c.ok is False, c
    assert c.headline == "Add majornum=1.", c.headline
    assert "majornum" in c.description, c.description


def test_review_transport_error_fails_open():
    c = _with_fake_transport(
        httpx.ConnectError("refused"),
        lambda: asyncio.run(critic.review("q", ["SELECT 1"], "ans")))
    assert c.ok is True, "transport error must fail open"


def test_review_non_json_200_response_fails_open():
    """A provider can return HTTP 200 with a non-JSON body — an HTML error
    page, captive portal, reverse-proxy index, or CDN interstitial. Most
    plausible now that LLM_BASE_URL is operator-configurable to any
    OpenAI-compatible endpoint: a misconfigured URL hitting the wrong host is
    the single most likely new failure mode.

    r.json() raises json.JSONDecodeError, a ValueError subclass that is NOT an
    httpx.HTTPError, so `except httpx.HTTPError` alone does not catch it. This
    must still fail OPEN (ok=True) — the critic is an enhancement and must
    never drop or block an answer, per this module's own docstring."""
    non_json_response = httpx.Response(
        200, text="<html><body>502 Bad Gateway</body></html>",
        request=httpx.Request("POST", "http://x/chat/completions"))
    c = _with_fake_transport(
        non_json_response,
        lambda: asyncio.run(critic.review("q", ["SELECT 1"], "ans")))
    assert c.ok is True, \
        "a 200 response with a non-JSON body must fail open, not raise"


def test_review_posts_url_headers_and_probe_timeout():
    """review() must route through the shared llmhttp transport helper with
    PROBE_TIMEOUT (30s), matching the guard's probe call — not
    DEFAULT_TIMEOUT (120s), which is reserved for full agent turns."""
    resp = _json_response({
        "choices": [{"message": {"content": "OK"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    _with_fake_transport(resp, lambda: asyncio.run(critic.review("q", ["SELECT 1"], "ans")))
    call = _FakeAsyncClient.calls[-1]
    assert call["url"] == "https://openrouter.ai/api/v1/chat/completions", call["url"]
    assert call["headers"]["Authorization"] == "Bearer test-key", call["headers"]
    assert call["headers"]["HTTP-Referer"] == "http://localhost:8000", call["headers"]
    assert call["headers"]["X-Title"] == "IPEDS Query", call["headers"]
    assert call["timeout"] == llmhttp.PROBE_TIMEOUT, call["timeout"]


# --- agent-loop integration ----------------------------------------------------

def _run(question):
    return asyncio.run(llm.run_agent(question))


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
    assert res.critic_headline == "" and res.critic_description == "", res
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
        return Critique(ok=False, headline="Add majornum=1.",
                        description="missing majornum=1; ~4x overcount")

    llm._chat = fake_chat
    llm.critic.review = fake_review
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    try:
        res = _run("q")
    finally:
        llm.critic.review = critic.review
    assert res.answer == "Corrected: 1,000,000.", res.answer
    assert res.critic_revised is True, res.critic_revised
    assert res.critic_headline == "Add majornum=1.", res.critic_headline
    assert "majornum" in res.critic_description, res.critic_description
    assert calls["critic"] == 1, "critic must run at most once per turn"
    # run_sql, draft, revision-round run_sql, corrected answer
    assert calls["chat"] == 4, calls


def test_rebuttal_without_new_sql_reemits_clean_draft():
    # Core regression test for the critic-revision leak: the model argues back
    # instead of re-querying after a REVISE verdict. The loop must discard the
    # rebuttal and re-emit the ORIGINAL clean draft, with critic_revised False
    # so no spurious lesson gets recorded downstream.
    calls = {"chat": 0, "critic": 0}
    clean_draft = "Example awarded 2,183 in 2024."
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
        return Critique(ok=False, headline="Verify cstotlt.", description="cstotlt may overcount")

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
        return Critique(ok=False, headline="Check magnitude.", description="magnitude looks high")

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


def test_requery_rebuttal_with_review_meta_reemits_clean_draft():
    # THE observed regression (memory critic-revision-leak): after a REVISE
    # verdict the model DOES re-run SQL (sql_log grows) but only to CONFIRM the
    # original number, then writes a rebuttal that addresses the reviewer and
    # lands on different prose — same answer, new text. `requeried and changed`
    # alone treats that as a genuine correction and ships the leaked
    # "The reviewer's concern…" meta. The review-meta backstop must catch it:
    # re-emit the clean draft, critic_revised False, no reviewer wording.
    calls = {"chat": 0, "critic": 0}
    draft = "Institutions awarded 144,671 master's degrees in Education in 2023."
    rebuttal = (
        "The reviewer's concern is understandable, but the 2-digit rollup row "
        "(cipcode='13') does exist and correctly represents the sum. I verified "
        "by also summing the 6-digit detail rows — both give exactly 144,671 "
        "master's degrees, so the original results are sound.")

    async def fake_chat(client, model, messages, tools=None):
        calls["chat"] += 1
        if calls["chat"] == 1:
            return {"choices": [{"message": {"content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "run_sql",
                    "arguments": '{"sql": "SELECT SUM(ctotalt) FROM c_a WHERE '
                                 'cipcode=\'13\' AND awlevel=7"}'}}]}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        if calls["chat"] == 2:
            return {"choices": [{"message": {"content": draft}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        if calls["chat"] == 3:  # revision round: model re-queries to double-check
            return {"choices": [{"message": {"content": "",
                "tool_calls": [{"id": "c2", "type": "function", "function": {
                    "name": "run_sql",
                    "arguments": '{"sql": "SELECT SUM(ctotalt) FROM c_a WHERE '
                                 'SUBSTR(cipcode,1,3)=\'13.\' AND length(cipcode)=7 '
                                 'AND awlevel=7"}'}}]}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        # ...then argues back with different prose but the SAME number
        return {"choices": [{"message": {"content": rebuttal}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    async def fake_review(question, sql_log, answer):
        calls["critic"] += 1
        return Critique(ok=False, headline="Avoid CIP rollup double count.",
                        description="cipcode='13' may double count across levels")

    llm._chat = fake_chat
    llm.critic.review = fake_review
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    try:
        res = _run("q")
    finally:
        llm.critic.review = critic.review
    assert res.answer == draft, res.answer
    assert res.critic_revised is False, res.critic_revised
    lowered = res.answer.lower()
    for leak in ("reviewer", "i verified", "the original results"):
        assert leak not in lowered, (leak, res.answer)


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
        return Critique(ok=False, headline="Use cipcode='99'.",
                        description="use cipcode='99' for the national total")

    llm._chat = fake_chat
    llm.critic.review = fake_review
    registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
    try:
        _run("q")
    finally:
        llm.critic.review = critic.review
    joined = " ".join(m.get("content") or "" for m in captured["msgs"])
    assert "reviewer flagged" in joined, joined
    assert "Use cipcode='99'." in joined and "cipcode='99' for the national total" in joined, joined


def test_no_sql_answer_skips_critic():
    calls = {"critic": 0}

    async def fake_chat(client, model, messages, tools=None):
        return {"choices": [{"message": {"content": "I can only help with IPEDS data."}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    async def fake_review(question, sql_log, answer):
        calls["critic"] += 1
        return Critique(ok=False, headline="should not run", description="should not be called")

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
    check("parse empty/garbled -> OK, empty headline/description", test_parse_empty_is_ok)
    check("parse structured REVISE extracts HEADLINE/DESCRIPTION",
          test_parse_structured_revise_extracts_headline_and_description)
    check("parse structured REVISE labels are case-insensitive",
          test_parse_structured_revise_labels_are_case_insensitive)
    check("parse structured REVISE is order-independent (DESCRIPTION before HEADLINE)",
          test_parse_structured_revise_is_order_independent)
    check("parse malformed REVISE falls back to description + truncated headline",
          test_parse_malformed_revise_falls_back_to_description_and_truncated_headline)
    check("parse bare REVISE gets a non-empty fallback",
          test_parse_bare_revise_gets_a_nonempty_fallback)
    check("parse_verdict never throws on garbage input", test_parse_never_throws_on_garbage)
    check("_SYSTEM pins the parser's HEADLINE/DESCRIPTION labels",
          test_system_prompt_pins_key_substrings)
    check("build_review_messages includes question/SQL/answer",
          test_build_messages_includes_artifacts)
    check("build_review_messages truncates a long answer",
          test_build_messages_truncates_long_answer)
    check("revision_instruction carries the headline and description",
          test_revision_instruction_carries_headline_and_description)
    check("revision_instruction keeps the anti-leak hardening",
          test_revision_instruction_keeps_anti_leak_hardening)
    check("review fails open without a key", test_review_fails_open_without_key)
    check("review fails open when disabled", test_review_disabled_fails_open)
    check("review OK verdict (live transport)", test_review_ok_verdict_live)
    check("review REVISE verdict (live transport)", test_review_revise_verdict_live)
    check("review transport error fails open", test_review_transport_error_fails_open)
    check("review non-JSON 200 body fails open, does not raise",
          test_review_non_json_200_response_fails_open)
    check("review posts url/headers/PROBE_TIMEOUT via llmhttp",
          test_review_posts_url_headers_and_probe_timeout)
    check("OK verdict returns the draft unchanged",
          test_ok_verdict_returns_draft_unchanged)
    check("REVISE verdict triggers exactly one revision",
          test_revise_verdict_triggers_one_revision)
    check("a rebuttal with no new SQL re-emits the clean draft (leak regression)",
          test_rebuttal_without_new_sql_reemits_clean_draft)
    check("a requery confirming the same answer is not a revision",
          test_requery_confirming_same_answer_is_not_a_revision)
    check("a requery rebuttal that leaks reviewer meta re-emits the clean draft",
          test_requery_rebuttal_with_review_meta_reemits_clean_draft)
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
