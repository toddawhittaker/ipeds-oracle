"""Chat router contract (backend/app/routers/chat.py): streaming turns, the semantic
cache-hit shortcut, edit/rerun (replacing an old exchange in place),
conversation list/get/delete, critic-driven lesson recording, the
fresh-deploy no-data guard (admin/non-admin wording, no agent run, no
conversation created), and the CSV export's error branches.

The 👍/👎 feedback feature (and its `promote_from_message` lesson path) was
removed. Lessons now come from the critic (the model's own mistakes) AND the
user-feedback distiller (corrective feedback on a follow-up turn) — see
`_record_feedback_lesson`/`_fire_and_forget` below. `POST /messages/{id}/feedback`
must 404 (route gone), and `get_conversation` no longer selects a `feedback` column.

No LLM/API key needed: guard.classify is patched to always allow, and
chat_router.stream_agent is replaced per-test with a canned async generator
(same pattern as backend/tests/test_guard.py) so every branch runs deterministically.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"
os.environ["COOKIE_SECURE"] = "false"
os.environ["LLM_API_KEY"] = ""
os.environ["RESEND_API_KEY"] = ""
# This suite logs in as more than one user; keep the auth rate limiter out of
# the way so it never masks a real assertion.
os.environ["AUTH_RATE_MAX_PER_EMAIL"] = "1000"
os.environ["AUTH_RATE_MAX_PER_IP"] = "1000"
# Disable the per-user chat throttle (SEC-3) here — this module fires far more
# than the default per-window budget of stream turns for one user; its own
# 429 is exercised in test_rate_limit.py.
os.environ["CHAT_RATE_MAX_PER_USER"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app import mailer  # noqa: E402

captured = {}
mailer.send_magic_link = lambda to, link: captured.__setitem__("link", link) or True
mailer.send_access_request = lambda *a, **k: True
mailer.send_access_approved = lambda to: captured.__setitem__("approved", to) or True

from app import guard, llmhttp, skills  # noqa: E402
from app.db import connect  # noqa: E402
from app.llm import AgentResult  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import chat as chat_router  # noqa: E402
from app.tools.sql import (  # noqa: E402
    QueryResult,
    SQLTimeoutError,
    SQLValidationError,
    run_sql,
)

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


async def _always_allow(question, history=None):
    # Carries real usage on purpose: the guard's spend is folded into every
    # turn's usage_log row, so a zeroed stub here would make the fold untestable.
    return guard.Verdict(allowed=True,
                         usage=llmhttp.Usage(prompt_tokens=11, completion_tokens=2,
                                             cached_prompt_tokens=8))


guard.classify = _always_allow


def _login(c, email="admin@example.edu"):
    c.post("/api/auth/request", json={"email": email})
    token = captured["link"].split("token=")[1]
    assert c.post("/api/auth/verify", json={"token": token}).status_code == 200


def _parse_sse(text):
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if block.startswith("data: "):
            events.append(json.loads(block[len("data: "):]))
    return events


def _make_agent(answer_text, *, sql_log=None, error=None, model="test-model"):
    """A canned chat_router.stream_agent replacement yielding one answer + done."""
    async def _agent(question, *, history=None, skills_block="", prior_results=None):
        if answer_text is not None:
            yield {"type": "answer", "text": answer_text}
        yield {"type": "done", "result": AgentResult(
            answer=answer_text or "", model_used=model, error=error,
            sql_log=sql_log or [], prompt_tokens=3, completion_tokens=2)}
    return _agent


def _explode_agent(question, *, history=None, skills_block="", prior_results=None):
    """A stream_agent that must never be reached — proves a cache hit really did
    short-circuit the agent, so its usage row can only be the guard's."""
    raise AssertionError("stream_agent must NOT run on an answer-cache hit")
    yield  # pragma: no cover — makes this an async generator


def _clear_usage_log():
    """Own the usage_log window: other tests in this module post turns too, and
    these assertions are positional (first row / second row)."""
    con = connect()
    try:
        con.execute("DELETE FROM usage_log")
        con.commit()
    finally:
        con.close()


def _make_agent_no_result():
    """A stream_agent that emits some progress but NEVER a terminal `done`
    result, so chat.py's `result is None` branch fires and `_persist` never
    runs. This is the deterministic server-side proxy for a mid-turn client
    disconnect (closed tab / dropped network / Stop-generating abandon): the
    turn produces nothing to persist."""
    async def _agent(question, *, history=None, skills_block="", prior_results=None):
        yield {"type": "status", "text": "working…"}
    return _agent


def _post_turn_no_result(c, question, *, conversation_id=None, edit_message_id=None):
    """Post a turn whose agent never yields a result (interrupted-turn proxy)."""
    orig_agent = chat_router.stream_agent
    orig_cache_lookup = skills.cache_lookup
    orig_skills_block = skills.retrieve_skills_block
    chat_router.stream_agent = _make_agent_no_result()
    skills.cache_lookup = lambda q, _uid=None: None
    skills.retrieve_skills_block = lambda q: ("", [])
    try:
        body = {"question": question}
        if conversation_id is not None:
            body["conversation_id"] = conversation_id
        if edit_message_id is not None:
            body["edit_message_id"] = edit_message_id
        return c.post("/api/chat/stream", json=body)
    finally:
        chat_router.stream_agent = orig_agent
        skills.cache_lookup = orig_cache_lookup
        skills.retrieve_skills_block = orig_skills_block


def _post_turn(c, question, *, conversation_id=None, edit_message_id=None,
              answer_text="42", sql_log=None, error=None):
    orig_agent = chat_router.stream_agent
    orig_skills_block = skills.retrieve_skills_block
    orig_bump = skills.bump_hits
    orig_cache_lookup = skills.cache_lookup
    orig_cache_store = skills.cache_store
    chat_router.stream_agent = _make_agent(answer_text, sql_log=sql_log, error=error)
    skills.retrieve_skills_block = lambda q: ("", [])
    skills.bump_hits = lambda ids: None
    skills.cache_lookup = lambda q, _uid=None: None
    skills.cache_store = lambda *a, **k: None
    try:
        body = {"question": question}
        if conversation_id is not None:
            body["conversation_id"] = conversation_id
        if edit_message_id is not None:
            body["edit_message_id"] = edit_message_id
        r = c.post("/api/chat/stream", json=body)
    finally:
        chat_router.stream_agent = orig_agent
        skills.retrieve_skills_block = orig_skills_block
        skills.bump_hits = orig_bump
        skills.cache_lookup = orig_cache_lookup
        skills.cache_store = orig_cache_store
    return r


# ---------------------------------------------------------------------------
# chat_stream: validation, ownership, edit/rerun
# ---------------------------------------------------------------------------

def test_empty_question_rejected():
    with TestClient(app) as c:
        _login(c)
        r = c.post("/api/chat/stream", json={"question": "   "})
        assert r.status_code == 400, r.text


def test_oversized_question_rejected_before_anything_is_written():
    """A question is a question, not a payload.

    BodyLimitMiddleware caps the whole request at 10 MB, but under that ceiling
    an unbounded question is still written to app.db TWICE (the user message and
    usage_log.question) and sent to the provider as billed tokens. The rejection
    has to land BEFORE any of that: the regression worth catching is not the
    status code but a row surviving it, so this asserts the conversation count is
    unchanged rather than just reading the 400.
    """
    from app.routers.chat import MAX_QUESTION_LEN

    with TestClient(app) as c:
        _login(c)
        with connect() as con:
            before = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]

        r = c.post("/api/chat/stream", json={"question": "x" * (MAX_QUESTION_LEN + 1)})
        assert r.status_code == 400, r.text
        # A readable sentence, not pydantic's {"detail": [{...}]} array — which
        # is why this is a hand-raised 400 and not Field(max_length=...).
        assert isinstance(r.json()["detail"], str), r.text
        assert "too long" in r.json()["detail"].lower(), r.text

        with connect() as con:
            after = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            msgs = con.execute(
                "SELECT COUNT(*) FROM messages WHERE length(content) > ?",
                (MAX_QUESTION_LEN,)).fetchone()[0]
        assert after == before, "an over-long question must not create a conversation"
        assert msgs == 0, "an over-long question must never reach messages"

    # The boundary itself is allowed through (it is a cap, not a target) — this
    # gets past the length check and fails later for want of a dataset/model,
    # which is enough to prove the guard did not fire.
    with TestClient(app) as c:
        _login(c)
        r = c.post("/api/chat/stream", json={"question": "y" * MAX_QUESTION_LEN})
        assert r.status_code != 400 or "too long" not in r.text.lower(), r.text


def test_stream_unknown_conversation_id_404():
    with TestClient(app) as c:
        _login(c)
        r = c.post("/api/chat/stream",
                   json={"question": "hi", "conversation_id": 999999})
        assert r.status_code == 404, r.text


def test_stream_conversation_not_owned_by_caller_404():
    with TestClient(app) as c:
        _login(c, "admin@example.edu")
        r = _post_turn(c, "first question in admin's conversation")
        events = _parse_sse(r.text)
        conv_id = next(e["id"] for e in events if e["type"] == "conversation")

        # A second user must not be able to post into admin's conversation.
        c.post("/api/admin/allowlist", json={"email": "other@example.edu"})
        c2 = TestClient(app)
        _login(c2, "other@example.edu")  # approval sends no link; they request their own
        r2 = c2.post("/api/chat/stream",
                    json={"question": "hi", "conversation_id": conv_id})
        assert r2.status_code == 404, r2.text


def test_edit_message_id_replaces_old_exchange():
    with TestClient(app) as c:
        _login(c)
        r1 = _post_turn(c, "original question", answer_text="original answer")
        events1 = _parse_sse(r1.text)
        conv_id = next(e["id"] for e in events1 if e["type"] == "conversation")
        done1 = next(e for e in events1 if e["type"] == "done")
        first_user_msg_id = done1["user_message_id"]

        before = c.get(f"/api/chat/conversations/{conv_id}").json()
        assert len(before) == 2, before  # user + assistant

        r2 = _post_turn(c, "edited question", conversation_id=conv_id,
                        edit_message_id=first_user_msg_id,
                        answer_text="edited answer")
        assert r2.status_code == 200, r2.text

        after = c.get(f"/api/chat/conversations/{conv_id}").json()
        assert len(after) == 2, after  # old pair replaced, not appended
        assert after[0]["content"] == "edited question", after
        assert after[1]["content"] == "edited answer", after


def test_interrupted_new_turn_leaves_no_phantom_conversation():
    """Regression (data-loss bug a): a brand-new conversation whose very first
    turn never persists (client disconnect / no agent result) must leave NO
    conversation behind. Before the fix, the `conversations` row was INSERTed +
    committed BEFORE gen(), so an interrupted first turn stranded a titled,
    0-message phantom in the sidebar."""
    with TestClient(app) as c:
        _login(c)
        before = c.get("/api/chat/conversations").json()

        r = _post_turn_no_result(c, "a doomed first question")
        assert r.status_code == 200, r.text

        after = c.get("/api/chat/conversations").json()
        # No new conversation may survive an interrupted first turn.
        assert len(after) == len(before), after
        # And specifically no 0-message phantom for any conversation.
        for conv in after:
            msgs = c.get(f"/api/chat/conversations/{conv['id']}").json()
            assert len(msgs) > 0, (conv, msgs)


def test_interrupted_edit_turn_keeps_the_old_exchange_intact():
    """Regression (data-loss bug b, the urgent one): an edit/rerun turn that
    never persists (client disconnect / no agent result) must NOT destroy the
    exchange it was replacing. Before the fix, `DELETE FROM messages WHERE
    id>=?` was committed BEFORE gen(), so an interrupted edit wiped the old
    user+assistant pair with nothing written back."""
    with TestClient(app) as c:
        _login(c)
        r1 = _post_turn(c, "original question", answer_text="original answer")
        events1 = _parse_sse(r1.text)
        conv_id = next(e["id"] for e in events1 if e["type"] == "conversation")
        first_user_msg_id = next(e for e in events1
                                 if e["type"] == "done")["user_message_id"]
        assert len(c.get(f"/api/chat/conversations/{conv_id}").json()) == 2

        # Edit the first message, but the turn is interrupted before persistence.
        r2 = _post_turn_no_result(c, "edited but doomed question",
                                  conversation_id=conv_id,
                                  edit_message_id=first_user_msg_id)
        assert r2.status_code == 200, r2.text

        after = c.get(f"/api/chat/conversations/{conv_id}").json()
        # The ORIGINAL exchange must be intact — not deleted, not replaced.
        assert len(after) == 2, after
        assert after[0]["content"] == "original question", after
        assert after[1]["content"] == "original answer", after


# ---------------------------------------------------------------------------
# Semantic cache hit branch
# ---------------------------------------------------------------------------

def test_cache_hit_serves_cached_answer_and_titles_new_conversation():
    with TestClient(app) as c:
        _login(c)
        orig_agent = chat_router.stream_agent
        orig_cache_lookup = skills.cache_lookup
        orig_gen_title = chat_router.generate_title
        orig_skills_block = skills.retrieve_skills_block

        def _explode(question, *, history=None, skills_block=""):
            raise AssertionError("stream_agent must not run on a cache hit")

        async def _fake_title(question, answer):
            return "A Cached Title", llmhttp.Usage(prompt_tokens=5,
                                                   completion_tokens=3)

        chat_router.stream_agent = _explode
        skills.cache_lookup = lambda q, _uid=None: {
            "answer_md": "cached answer 12,345", "final_sql": "SELECT 1"}
        chat_router.generate_title = _fake_title
        skills.retrieve_skills_block = lambda q: ("", [])
        try:
            r = c.post("/api/chat/stream", json={"question": "a cacheable question"})
        finally:
            chat_router.stream_agent = orig_agent
            skills.cache_lookup = orig_cache_lookup
            chat_router.generate_title = orig_gen_title
            skills.retrieve_skills_block = orig_skills_block

        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        answer = next(e for e in events if e["type"] == "answer")
        assert answer["text"] == "cached answer 12,345", events
        done = next(e for e in events if e["type"] == "done")
        assert done.get("cached") is True, done
        assert done.get("title") == "A Cached Title", done


def test_turn_duration_is_measured_persisted_and_in_the_done_event():
    """The "Thought for N seconds" value: the turn's wall-clock ms is in the done
    event (live display) AND persisted on the assistant message (survives reload,
    returned by get_conversation). Can't come from timestamps — the user + the
    assistant rows share one created_at."""
    with TestClient(app) as c:
        _login(c)
        r = _post_turn(c, "how many institutions?", answer_text="42")
        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        done = next(e for e in events if e["type"] == "done")
        assert isinstance(done.get("duration_ms"), int) and done["duration_ms"] >= 0, done
        conv_id = next(e for e in events if e["type"] == "conversation")["id"]
        msgs = c.get(f"/api/chat/conversations/{conv_id}").json()
        assistant = [m for m in msgs if m["role"] == "assistant"][-1]
        assert assistant["duration_ms"] is not None and assistant["duration_ms"] >= 0, assistant
        # get_conversation also surfaces the user turn's created_at (the stamp).
        user = [m for m in msgs if m["role"] == "user"][-1]
        assert user["created_at"] and user["created_at"] > 0, user


def _usage_rows(cols="prompt_tokens, completion_tokens, cached_prompt_tokens, cost"):
    con = connect()
    try:
        return con.execute(f"SELECT {cols} FROM usage_log ORDER BY id").fetchall()
    finally:
        con.close()


def test_guard_spend_is_billed_on_every_turn_shape():
    """THE REGRESSION: the topical guard makes a real LLM call on EVERY question,
    before the answer cache and before the agent -- and none of it reached
    usage_log. On the refusal path Verdict.tokens landed only on messages.tokens;
    on the allowed path the verdict was never referenced again. So the app's only
    record of spend under-counted by one guard call per question, forever.

    All three shapes are checked here because each writes usage_log from a
    DIFFERENT branch, and two of them have no AgentResult to fold into:
      1. a normal agent turn  -> guard usage ADDED to the agent's own
      2. an off-topic refusal -> the guard is the only call, so the row IS it
      3. an answer-cache hit  -> not free; the guard ran before the lookup

    The stub guard reports 11/2 prompt/completion (see _always_allow), the stub
    agent 3/2, so a correct fold is 14/4 -- asserting the SUM is what catches a
    fold that silently overwrites instead of accumulating."""
    async def _deny(question, history=None):
        return guard.Verdict(allowed=False,
                             usage=llmhttp.Usage(prompt_tokens=9, completion_tokens=3,
                                                 cached_prompt_tokens=7))

    cached_answer = {"answer_md": "a stored answer", "final_sql": "SELECT 1"}
    with TestClient(app) as c:
        _login(c)
        orig_agent, orig_guard = chat_router.stream_agent, guard.classify
        orig_block, orig_lookup = skills.retrieve_skills_block, skills.cache_lookup
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q, _uid=None: None
        try:
            _clear_usage_log()
            # 1) agent turn — guard usage adds to the agent's
            chat_router.stream_agent = _make_agent("an answer", sql_log=["SELECT 1"])
            c.post("/api/chat/stream", json={"question": "a normal question"})
            # 2) refusal — the guard is the whole turn
            guard.classify = _deny
            c.post("/api/chat/stream", json={"question": "give me a recipe"})
            guard.classify = _always_allow
            # 3) cache hit — skips the agent, not the guard
            skills.cache_lookup = lambda q, _uid=None: cached_answer
            chat_router.stream_agent = _explode_agent
            c.post("/api/chat/stream", json={"question": "a repeat question"})
        finally:
            chat_router.stream_agent, guard.classify = orig_agent, orig_guard
            skills.retrieve_skills_block, skills.cache_lookup = orig_block, orig_lookup

    rows = _usage_rows()
    assert len(rows) == 3, rows
    agent_row, refusal_row, cache_row = rows
    # 1) 3+11 prompt, 2+2 completion — added, not replaced.
    assert agent_row["prompt_tokens"] == 14, dict(agent_row)
    assert agent_row["completion_tokens"] == 4, dict(agent_row)
    assert agent_row["cached_prompt_tokens"] == 8, dict(agent_row)
    # 2) the refusal row is exactly the guard's own call, no longer 0/0.
    assert refusal_row["prompt_tokens"] == 9, dict(refusal_row)
    assert refusal_row["completion_tokens"] == 3, dict(refusal_row)
    assert refusal_row["cached_prompt_tokens"] == 7, dict(refusal_row)
    # 3) a cache hit is NOT free — it still paid for the guard.
    assert cache_row["prompt_tokens"] == 11, dict(cache_row)
    assert cache_row["completion_tokens"] == 2, dict(cache_row)


def test_guard_tokens_never_pollute_the_schema_cache_columns():
    """first_call_* isolates the AGENT's schema-prefix reuse -- its first call
    carries the big static SCHEMA.md block. The guard's prompt is a completely
    different prefix, so folding its tokens in there would silently corrupt the
    Schema-cache stat on Admin -> Usage, which exists to answer 'is keeping the
    whole schema in the prompt paying off?'. The blended cached_prompt_tokens DOES
    take them (asserted above); these two columns must not."""
    with TestClient(app) as c:
        _login(c)
        orig_agent = chat_router.stream_agent
        orig_block, orig_lookup = skills.retrieve_skills_block, skills.cache_lookup
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q, _uid=None: None
        try:
            _clear_usage_log()
            chat_router.stream_agent = _make_agent("an answer", sql_log=["SELECT 1"])
            c.post("/api/chat/stream", json={"question": "a normal question"})
        finally:
            chat_router.stream_agent = orig_agent
            skills.retrieve_skills_block, skills.cache_lookup = orig_block, orig_lookup

    rows = _usage_rows("first_call_prompt_tokens, first_call_cached_prompt_tokens, "
                       "cached_prompt_tokens")
    assert len(rows) == 1, rows
    # The stub agent sets no first_call_*; the guard's 11/8 must not appear here.
    assert rows[0]["first_call_prompt_tokens"] == 0, dict(rows[0])
    assert rows[0]["first_call_cached_prompt_tokens"] == 0, dict(rows[0])
    # ...while the blended column DID take the guard's cached tokens.
    assert rows[0]["cached_prompt_tokens"] == 8, dict(rows[0])


def test_add_usage_accumulates_onto_a_committed_row():
    """The title call and the feedback distiller finish AFTER _persist commits, so
    their spend is added to the existing row rather than folded in at insert time
    -- deliberately, because moving either call ahead of the write would put a
    network probe in front of the only statement that saves the user's answer.

    Pins the two things that make the UPDATE correct: it ACCUMULATES (a second
    probe must not overwrite the first), and cost_estimated is a MAX, so a later
    estimated call taints an otherwise provider-billed row and a later billed call
    cannot clear the flag."""
    con = connect()
    try:
        con.execute("DELETE FROM usage_log")
        con.execute(
            "INSERT INTO usage_log(user_id, question, model_used, prompt_tokens, "
            "completion_tokens, cached_prompt_tokens, ok, cached, cost, "
            "cost_estimated, created_at) VALUES (1,'q','m',10,5,4,1,0,0.001,0,?)",
            (time.time(),))
        con.commit()
        row_id = con.execute("SELECT id FROM usage_log").fetchone()[0]
    finally:
        con.close()

    chat_router._add_usage(row_id, llmhttp.Usage(prompt_tokens=7, completion_tokens=3,
                                                 cached_prompt_tokens=2, cost=0.002))
    r = _usage_rows("prompt_tokens, completion_tokens, cached_prompt_tokens, cost, "
                    "cost_estimated")[0]
    assert (r["prompt_tokens"], r["completion_tokens"]) == (17, 8), dict(r)
    assert r["cached_prompt_tokens"] == 6, dict(r)
    assert abs(r["cost"] - 0.003) < 1e-9, dict(r)
    # A provider-reported cost leaves the row's provenance alone.
    assert r["cost_estimated"] == 0, dict(r)

    # An UNPRICED call (no reported cost) is an estimate, and taints the row.
    chat_router._add_usage(row_id, llmhttp.Usage(prompt_tokens=1, completion_tokens=1))
    r = _usage_rows("prompt_tokens, cost_estimated")[0]
    assert r["prompt_tokens"] == 18, dict(r)
    assert r["cost_estimated"] == 1, dict(r)

    # ...and a later billed call must NOT clear it (MAX, not assignment).
    chat_router._add_usage(row_id, llmhttp.Usage(prompt_tokens=1, cost=0.005))
    assert _usage_rows("cost_estimated")[0]["cost_estimated"] == 1

    # A no-op probe (nothing spent) writes nothing at all.
    before = _usage_rows("prompt_tokens")[0]["prompt_tokens"]
    chat_router._add_usage(row_id, llmhttp.Usage())
    assert _usage_rows("prompt_tokens")[0]["prompt_tokens"] == before


def test_exhaustion_status_is_persisted_to_usage_log():
    """A degraded tool-budget-exhausted turn (S5) records exhaustion='degraded' on
    usage_log, so Admin -> Usage can count it. Threaded from AgentResult's
    exhausted/exhaustion_degraded flags through _persist (chat.py)."""
    def _exhausted_agent(answer_text, *, exhausted, degraded):
        async def _agent(question, *, history=None, skills_block="", prior_results=None):
            yield {"type": "answer", "text": answer_text}
            yield {"type": "done", "result": AgentResult(
                answer=answer_text, model_used="test-model", sql_log=["SELECT 1"],
                exhausted=exhausted, exhaustion_degraded=degraded,
                prompt_tokens=3, completion_tokens=2)}
        return _agent

    with TestClient(app) as c:
        _login(c)
        orig_agent = chat_router.stream_agent
        orig_block, orig_lookup = skills.retrieve_skills_block, skills.cache_lookup
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q, _uid=None: None
        try:
            chat_router.stream_agent = _exhausted_agent(
                "couldn't finish", exhausted=True, degraded=True)
            c.post("/api/chat/stream", json={"question": "a hard question"})
            chat_router.stream_agent = _make_agent("a normal answer", sql_log=["SELECT 1"])
            c.post("/api/chat/stream", json={"question": "an easy question"})
        finally:
            chat_router.stream_agent = orig_agent
            skills.retrieve_skills_block, skills.cache_lookup = orig_block, orig_lookup
        con = connect()
        try:
            rows = [r[0] for r in con.execute(
                "SELECT exhaustion FROM usage_log ORDER BY id").fetchall()]
        finally:
            con.close()
        # The degraded turn records 'degraded'; the normal turn stays NULL.
        assert "degraded" in rows, rows
        assert None in rows, rows


def test_normal_flow_titles_a_new_conversation():
    with TestClient(app) as c:
        _login(c)
        orig_agent = chat_router.stream_agent
        orig_gen_title = chat_router.generate_title
        orig_skills_block = skills.retrieve_skills_block
        orig_cache_lookup = skills.cache_lookup
        orig_cache_store = skills.cache_store

        async def _fake_title(question, answer):
            return "A Real-Flow Title", llmhttp.Usage(prompt_tokens=5,
                                                      completion_tokens=3)

        chat_router.stream_agent = _make_agent("a real answer", sql_log=["SELECT 1"])
        chat_router.generate_title = _fake_title
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q, _uid=None: None
        skills.cache_store = lambda *a, **k: None
        try:
            r = c.post("/api/chat/stream", json={"question": "a fresh question"})
        finally:
            chat_router.stream_agent = orig_agent
            chat_router.generate_title = orig_gen_title
            skills.retrieve_skills_block = orig_skills_block
            skills.cache_lookup = orig_cache_lookup
            skills.cache_store = orig_cache_store

        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        done = next(e for e in events if e["type"] == "done")
        assert done.get("title") == "A Real-Flow Title", done
        conv_id = next(e["id"] for e in events if e["type"] == "conversation")
        convs = c.get("/api/chat/conversations").json()
        assert any(x["id"] == conv_id and x["title"] == "A Real-Flow Title"
                  for x in convs), convs


def test_thinking_trace_is_persisted_and_returned_on_reload():
    """The assistant's progress trace (status/reasoning/SQL/tool) must be stored
    and returned by GET /conversations/{id}, so the 'Thinking' disclosure
    survives a reload — not just the live in-session turn. Regression guard for
    the reopened-chat case where SQL persisted but Thinking vanished."""
    async def _traced_agent(question, *, history=None, skills_block="", prior_results=None):
        yield {"type": "thinking", "text": "reasoning about the question"}
        yield {"type": "status", "text": "Running query…"}
        yield {"type": "sql", "sql": "SELECT 1"}
        yield {"type": "tool", "name": "run_sql", "ok": True}
        yield {"type": "answer", "text": "the answer"}
        yield {"type": "done", "result": AgentResult(
            answer="the answer", model_used="test", sql_log=["SELECT 1"],
            prompt_tokens=1, completion_tokens=1)}

    with TestClient(app) as c:
        _login(c)
        orig_agent = chat_router.stream_agent
        orig_skills_block = skills.retrieve_skills_block
        orig_cache_lookup = skills.cache_lookup
        orig_cache_store = skills.cache_store
        chat_router.stream_agent = _traced_agent
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q, _uid=None: None
        skills.cache_store = lambda *a, **k: None
        try:
            r = c.post("/api/chat/stream", json={"question": "a traced question"})
        finally:
            chat_router.stream_agent = orig_agent
            skills.retrieve_skills_block = orig_skills_block
            skills.cache_lookup = orig_cache_lookup
            skills.cache_store = orig_cache_store

        assert r.status_code == 200, r.text
        conv_id = next(e["id"] for e in _parse_sse(r.text) if e["type"] == "conversation")
        # Reload the conversation (what the client does on reopen/refresh).
        msgs = c.get(f"/api/chat/conversations/{conv_id}").json()
        assistant = next(m for m in msgs if m["role"] == "assistant")
        trace = json.loads(assistant["thinking"])
        # Mirrors the frontend's live addThought() mapping exactly.
        assert trace == [
            {"kind": "reason", "text": "reasoning about the question"},
            {"kind": "status", "text": "Running query…"},
            {"kind": "sql", "text": "SELECT 1"},
            {"kind": "tool", "text": "run_sql ✓"},
        ], trace
        # The user message carries no trace: NULL, like its NULL sql_log. The
        # client maps null -> [] on load (Chat.jsx), so no Thinking toggle shows.
        user_msg = next(m for m in msgs if m["role"] == "user")
        assert user_msg["thinking"] is None, user_msg["thinking"]


def test_extract_figure_parses_and_strips_the_fence():
    """The server pulls the ```figure fence out of the answer into structured data
    and ALWAYS strips it from the prose (so raw JSON never reaches the user)."""
    from app.llm import _extract_figure
    ans = ("CA publics awarded **7,679** CS degrees.\n\n"
           "```figure\n"
           '{"value":"7,679","unit":"degrees","label":"CS bachelor\'s","source":"IPEDS"}\n'
           "```\n")
    clean, fig = _extract_figure(ans)
    assert "```figure" not in clean, clean
    assert fig == {"value": "7,679", "unit": "degrees",
                   "label": "CS bachelor's", "source": "IPEDS"}, fig
    # No fence -> unchanged, None.
    assert _extract_figure("plain answer") == ("plain answer", None)
    # Malformed JSON -> fence stripped anyway (no raw JSON leak), None.
    clean2, fig2 = _extract_figure("x\n```figure\nnot json\n```\ny")
    assert "```figure" not in clean2 and fig2 is None, (clean2, fig2)
    # Missing the required label -> stripped, None (no lopsided half-figure).
    _, fig3 = _extract_figure('```figure\n{"value":"5"}\n```')
    assert fig3 is None, fig3
    # Some models emit an HTML <figure> tag instead of the fence — accept + strip it.
    tag = 'text\n<figure>\n{"value":"3,395","label":"Nursing BSN"}\n</figure>\nmore'
    clean4, fig4 = _extract_figure(tag)
    assert "<figure>" not in clean4 and "3,395" not in clean4, clean4
    assert fig4 == {"value": "3,395", "label": "Nursing BSN"}, fig4


def test_figure_is_persisted_and_returned_on_reload():
    """A structured figure emitted during a turn reaches the client AND is stored +
    returned by GET /conversations/{id}, so the hero statistic survives a reload
    like sql_log/thinking."""
    fig = {"value": "7,679", "unit": "degrees", "label": "CS bachelor's", "source": "IPEDS"}

    async def _figure_agent(question, *, history=None, skills_block="", prior_results=None):
        yield {"type": "figure", "figure": fig}
        yield {"type": "answer", "text": "the answer"}
        yield {"type": "done", "result": AgentResult(
            answer="the answer", model_used="test", sql_log=["SELECT 1"],
            figure=fig, prompt_tokens=1, completion_tokens=1)}

    with TestClient(app) as c:
        _login(c)
        orig_agent = chat_router.stream_agent
        orig_skills_block = skills.retrieve_skills_block
        orig_cache_lookup = skills.cache_lookup
        orig_cache_store = skills.cache_store
        chat_router.stream_agent = _figure_agent
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q, _uid=None: None
        skills.cache_store = lambda *a, **k: None
        try:
            r = c.post("/api/chat/stream", json={"question": "a figured question"})
        finally:
            chat_router.stream_agent = orig_agent
            skills.retrieve_skills_block = orig_skills_block
            skills.cache_lookup = orig_cache_lookup
            skills.cache_store = orig_cache_store

        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        # The figure event reaches the client (the frontend accumulates it live).
        assert any(e["type"] == "figure" and e["figure"] == fig for e in events), events
        conv_id = next(e["id"] for e in events if e["type"] == "conversation")
        # Reload the conversation (what the client does on reopen/refresh).
        msgs = c.get(f"/api/chat/conversations/{conv_id}").json()
        assistant = next(m for m in msgs if m["role"] == "assistant")
        assert json.loads(assistant["figure"]) == fig, assistant["figure"]
        # A figureless message stores NULL; the client maps null -> no figure.
        user_msg = next(m for m in msgs if m["role"] == "user")
        assert user_msg["figure"] is None, user_msg["figure"]


def test_table_grounding_is_persisted_and_in_the_done_event():
    """The table's verdict has to reach the READER, live and after a reload.

    check_table graded every measure cell from the day it shipped, but the result
    landed only on usage_log — an admin stat, invisible to the person about to
    copy those numbers into a report. Without BOTH halves of this the mark can't
    render: no done-event field means the live turn shows nothing until refresh,
    and no persisted column means it vanishes on reopen. Exactly the gap #215
    closed for the figure.

    Also pins that the two COUNTS travel with the status — the wording states
    "N values", so a status without its counts renders an empty claim.
    """
    async def _table_agent(question, *, history=None, skills_block="", prior_results=None):
        yield {"type": "answer", "text": "| a |\n| --- |\n| 1 |"}
        yield {"type": "done", "result": AgentResult(
            answer="| a |\n| --- |\n| 1 |", model_used="test", sql_log=["SELECT 1"],
            table_grounding="matched", table_cells_checked=12, table_cells_matched=12,
            prompt_tokens=1, completion_tokens=1)}

    with TestClient(app) as c:
        _login(c)
        orig_agent = chat_router.stream_agent
        orig_skills_block = skills.retrieve_skills_block
        orig_cache_lookup = skills.cache_lookup
        orig_cache_store = skills.cache_store
        chat_router.stream_agent = _table_agent
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q, _uid=None: None
        skills.cache_store = lambda *a, **k: None
        try:
            r = c.post("/api/chat/stream", json={"question": "a table question"})
        finally:
            chat_router.stream_agent = orig_agent
            skills.retrieve_skills_block = orig_skills_block
            skills.cache_lookup = orig_cache_lookup
            skills.cache_store = orig_cache_store

        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        done = next(e for e in events if e["type"] == "done")
        # Live: the mark renders without waiting for a reload.
        assert done.get("table_grounding") == "matched", done
        assert done.get("table_cells_checked") == 12, done
        assert done.get("table_cells_matched") == 12, done

        conv_id = next(e["id"] for e in events if e["type"] == "conversation")
        msgs = c.get(f"/api/chat/conversations/{conv_id}").json()
        assistant = next(m for m in msgs if m["role"] == "assistant")
        # Reloaded: same verdict, same counts, straight off the message row.
        # .get() on purpose — if get_conversation stops SELECTing these the
        # failure must be a readable assertion, not a KeyError that aborts the
        # whole suite and hides every test after it.
        assert assistant.get("table_grounding") == "matched", assistant
        assert assistant.get("table_cells_checked") == 12, assistant
        assert assistant.get("table_cells_matched") == 12, assistant
        # An ungraded row stores NULL, not 0 — "0 values reproduced" would be a
        # claim, where NULL correctly renders no mark at all.
        user_msg = next(m for m in msgs if m["role"] == "user")
        assert user_msg.get("table_grounding") is None, user_msg
        assert user_msg.get("table_cells_checked") is None, user_msg


def test_extract_suggestions_parses_and_strips_the_fence():
    """The server pulls the ```followups fence (a JSON array of drill-down
    questions) into structured data and ALWAYS strips it from the prose."""
    from app.llm import _extract_suggestions
    ans = ('The answer.\n\n```followups\n'
           '["How does this compare to Texas?", "Which programs drove it?"]\n```\n')
    clean, sugg = _extract_suggestions(ans)
    assert "```followups" not in clean, clean
    assert sugg == ["How does this compare to Texas?", "Which programs drove it?"], sugg
    assert _extract_suggestions("plain answer") == ("plain answer", None)
    # Malformed JSON -> stripped, None. Caps at 3.
    clean2, sugg2 = _extract_suggestions("x\n```followups\nnot json\n```\ny")
    assert "```followups" not in clean2 and sugg2 is None, (clean2, sugg2)
    _, sugg3 = _extract_suggestions('```followups\n["a","b","c","d"]\n```')
    assert sugg3 == ["a", "b", "c"], sugg3


def test_suggestions_are_persisted_and_returned_on_reload():
    """Drill-down suggestions reach the client AND are stored + returned by
    GET /conversations/{id}, so the chips survive a reload like figure/sql_log."""
    sugg = ["How does this compare to Texas?", "Which programs drove it?"]

    async def _suggest_agent(question, *, history=None, skills_block="", prior_results=None):
        yield {"type": "suggestions", "suggestions": sugg}
        yield {"type": "answer", "text": "the answer"}
        yield {"type": "done", "result": AgentResult(
            answer="the answer", model_used="test", sql_log=["SELECT 1"],
            suggestions=sugg, prompt_tokens=1, completion_tokens=1)}

    with TestClient(app) as c:
        _login(c)
        orig_agent = chat_router.stream_agent
        orig_skills_block = skills.retrieve_skills_block
        orig_cache_lookup = skills.cache_lookup
        orig_cache_store = skills.cache_store
        chat_router.stream_agent = _suggest_agent
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q, _uid=None: None
        skills.cache_store = lambda *a, **k: None
        try:
            r = c.post("/api/chat/stream", json={"question": "a question with follow-ups"})
        finally:
            chat_router.stream_agent = orig_agent
            skills.retrieve_skills_block = orig_skills_block
            skills.cache_lookup = orig_cache_lookup
            skills.cache_store = orig_cache_store

        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        assert any(e["type"] == "suggestions" and e["suggestions"] == sugg for e in events), events
        conv_id = next(e["id"] for e in events if e["type"] == "conversation")
        msgs = c.get(f"/api/chat/conversations/{conv_id}").json()
        assistant = next(m for m in msgs if m["role"] == "assistant")
        assert json.loads(assistant["suggestions"]) == sugg, assistant["suggestions"]
        user_msg = next(m for m in msgs if m["role"] == "user")
        assert user_msg["suggestions"] is None, user_msg["suggestions"]


def test_clarify_turn_persists_is_never_cached_and_records_no_lesson():
    """The disambiguation "clarify" turn: the `clarify` SSE event reaches the
    client, is persisted on the assistant message (GET /conversations/{id} must
    select it, mirroring figure/suggestions), and — the two behavioral contracts
    that ONLY this test catches — is never written to the answer cache and never
    triggers a critic-lesson recording. `sql_log` is deliberately non-empty here
    (a defensive "the model ran SQL anyway" scenario): the pre-existing cache
    gate (`... and result.sql_log`) and critic-lesson gate would otherwise let
    this turn through on their OWN, un-related conditions, masking a missing
    `clarify is None` guard."""
    clarify = {"question": "Do you mean bachelor's degrees only, or all award levels?",
              "options": ["Bachelor's only", "Include all levels"]}

    async def _clarify_agent(question, *, history=None, skills_block="", prior_results=None):
        yield {"type": "clarify", "clarify": clarify}
        yield {"type": "answer", "text": clarify["question"]}
        yield {"type": "done", "result": AgentResult(
            answer=clarify["question"], model_used="test-model", error=None,
            sql_log=["SELECT 1"], clarify=clarify,
            prompt_tokens=1, completion_tokens=1)}

    with TestClient(app) as c:
        _login(c)
        orig_agent = chat_router.stream_agent
        orig_skills_block = skills.retrieve_skills_block
        orig_cache_lookup = skills.cache_lookup
        orig_cache_store = skills.cache_store
        orig_record_critic = skills.record_lesson_from_critic
        cache_calls = {"n": 0}
        lesson_calls = {"n": 0}
        chat_router.stream_agent = _clarify_agent
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q, _uid=None: None
        skills.cache_store = lambda *a, **k: cache_calls.__setitem__("n", cache_calls["n"] + 1)
        skills.record_lesson_from_critic = \
            lambda *a, **k: lesson_calls.__setitem__("n", lesson_calls["n"] + 1)
        try:
            r = c.post("/api/chat/stream",
                       json={"question": "which undergraduate major produces the most graduates?"})
        finally:
            chat_router.stream_agent = orig_agent
            skills.retrieve_skills_block = orig_skills_block
            skills.cache_lookup = orig_cache_lookup
            skills.cache_store = orig_cache_store
            skills.record_lesson_from_critic = orig_record_critic

        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        assert any(e["type"] == "clarify" and e["clarify"] == clarify for e in events), events
        conv_id = next(e["id"] for e in events if e["type"] == "conversation")

        msgs = c.get(f"/api/chat/conversations/{conv_id}").json()
        assistant = next(m for m in msgs if m["role"] == "assistant")
        assert json.loads(assistant["clarify"]) == clarify, assistant["clarify"]
        user_msg = next(m for m in msgs if m["role"] == "user")
        assert user_msg["clarify"] is None, user_msg["clarify"]

        assert cache_calls["n"] == 0, "a clarify turn must never be written to the answer cache"
        assert lesson_calls["n"] == 0, "a clarify turn must never record a critic lesson"


def test_retrieved_skills_bump_their_hit_count():
    with TestClient(app) as c:
        _login(c)
        skill_row = c.get("/api/admin/skills").json()[0]
        skill_id = skill_row["id"]
        before_hits = skill_row["hits"]

        orig_agent = chat_router.stream_agent
        orig_skills_block = skills.retrieve_skills_block
        orig_cache_lookup = skills.cache_lookup
        orig_cache_store = skills.cache_store

        chat_router.stream_agent = _make_agent("answer", sql_log=["SELECT 1"])
        skills.retrieve_skills_block = lambda q: ("some few-shot block", [skill_id])
        skills.cache_lookup = lambda q, _uid=None: None
        skills.cache_store = lambda *a, **k: None
        try:
            r = c.post("/api/chat/stream", json={"question": "a question using a skill"})
        finally:
            chat_router.stream_agent = orig_agent
            skills.retrieve_skills_block = orig_skills_block
            skills.cache_lookup = orig_cache_lookup
            skills.cache_store = orig_cache_store

        assert r.status_code == 200, r.text
        after = next(x for x in c.get("/api/admin/skills").json() if x["id"] == skill_id)
        assert after["hits"] == before_hits + 1, after


def test_critic_revision_records_a_lesson():
    """The anti-vacuous half of the category gate: a LEARNABLE category
    (CIP_ROLLUP) must still let a critic-forced revision reach
    record_lesson_from_critic. Without this test, an implementation that
    suppresses every lesson (e.g. always failing lessoncats.is_learnable)
    would pass the rest of this suite's "must NOT record" cases trivially."""
    with TestClient(app) as c:
        _login(c)
        captured = {}

        async def _critic_agent(question, *, history=None, skills_block="", prior_results=None):
            yield {"type": "answer", "text": "corrected answer"}
            yield {"type": "done", "result": AgentResult(
                answer="corrected answer", model_used="test-model", error=None,
                sql_log=["SELECT SUM(x) FROM c_a"], critic_revised=True,
                critic_headline="Add majornum=1.",
                critic_description="no majornum=1 filter; double count",
                critic_category="CIP_ROLLUP")}

        orig_agent = chat_router.stream_agent
        orig_block = skills.retrieve_skills_block
        orig_cache_lookup = skills.cache_lookup
        orig_cache_store = skills.cache_store
        orig_record = skills.record_lesson_from_critic
        chat_router.stream_agent = _critic_agent
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q, _uid=None: None
        skills.cache_store = lambda *a, **k: None
        skills.record_lesson_from_critic = \
            lambda q, sql, headline, description: captured.update(
                q=q, sql=sql, headline=headline, description=description)
        try:
            r = c.post("/api/chat/stream", json={"question": "national bachelor total"})
        finally:
            chat_router.stream_agent = orig_agent
            skills.retrieve_skills_block = orig_block
            skills.cache_lookup = orig_cache_lookup
            skills.cache_store = orig_cache_store
            skills.record_lesson_from_critic = orig_record

        assert r.status_code == 200, r.text
        assert captured.get("q") == "national bachelor total", captured
        assert captured.get("sql") == "SELECT SUM(x) FROM c_a", captured
        assert captured.get("headline") == "Add majornum=1.", captured
        assert "majornum" in captured.get("description", ""), captured


def test_critic_lesson_not_recorded_on_followup_turn():
    with TestClient(app) as c:
        _login(c)
        calls = {"n": 0}

        async def _critic_agent(question, *, history=None, skills_block="", prior_results=None):
            yield {"type": "answer", "text": "ans"}
            yield {"type": "done", "result": AgentResult(
                answer="ans", model_used="test-model", error=None,
                sql_log=["SELECT 1"], critic_revised=True,
                critic_headline="A headline.", critic_description="a rule",
                # A learnable category, so this test isolates the history gate
                # (turn 1 records, turn 2 doesn't) from the category gate
                # exercised separately below.
                critic_category="CIP_ROLLUP")}

        orig_agent = chat_router.stream_agent
        orig_block = skills.retrieve_skills_block
        orig_cache_lookup = skills.cache_lookup
        orig_cache_store = skills.cache_store
        orig_record = skills.record_lesson_from_critic
        chat_router.stream_agent = _critic_agent
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q, _uid=None: None
        skills.cache_store = lambda *a, **k: None
        skills.record_lesson_from_critic = lambda *a, **k: calls.__setitem__("n", calls["n"] + 1)
        try:
            # Turn 1 (new conversation) records; find its conversation id.
            r1 = c.post("/api/chat/stream", json={"question": "first turn q"})
            conv_id = next(e["id"] for e in _parse_sse(r1.text) if e["type"] == "conversation")
            assert calls["n"] == 1, "first turn should record a lesson"
            # Turn 2 in the SAME conversation now has history → must NOT record.
            c.post("/api/chat/stream",
                   json={"question": "and for Ohio?", "conversation_id": conv_id})
        finally:
            chat_router.stream_agent = orig_agent
            skills.retrieve_skills_block = orig_block
            skills.cache_lookup = orig_cache_lookup
            skills.cache_store = orig_cache_store
            skills.record_lesson_from_critic = orig_record
        assert calls["n"] == 1, "a follow-up turn must not record a context-less lesson"


def test_ungrounded_number_category_revises_but_records_no_lesson():
    """UNGROUNDED_NUMBER is the rejected class this PR exists to stop from
    becoming a lesson: app/grounding.py already enforces it deterministically
    per-turn, and a retrieved lesson can't fix that. The revision round still
    has to fire and ship (pinned separately, at the agent-loop level, in
    test_critic.py's test_revise_still_fires_when_category_is_not_learnable) --
    what this test pins is that chat.py's recording gate refuses to store it as
    a lesson even though critic_revised is True and a headline/description are
    present, exactly the shape a genuinely learnable finding has. Without the
    category check this reaches skills.record_lesson_from_critic."""
    with TestClient(app) as c:
        _login(c)
        calls = {"n": 0}

        async def _critic_agent(question, *, history=None, skills_block="", prior_results=None):
            yield {"type": "answer", "text": "corrected answer"}
            yield {"type": "done", "result": AgentResult(
                answer="corrected answer", model_used="test-model", error=None,
                sql_log=["SELECT SUM(x) FROM c_a"], critic_revised=True,
                critic_headline="Verify the figure against the query result.",
                critic_description="the headline number was not present in the retained rows",
                critic_category="UNGROUNDED_NUMBER")}

        orig_agent = chat_router.stream_agent
        orig_block = skills.retrieve_skills_block
        orig_cache_lookup = skills.cache_lookup
        orig_cache_store = skills.cache_store
        orig_record = skills.record_lesson_from_critic
        chat_router.stream_agent = _critic_agent
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q, _uid=None: None
        skills.cache_store = lambda *a, **k: None
        skills.record_lesson_from_critic = lambda *a, **k: calls.__setitem__("n", calls["n"] + 1)
        try:
            r = c.post("/api/chat/stream", json={"question": "national bachelor total"})
        finally:
            chat_router.stream_agent = orig_agent
            skills.retrieve_skills_block = orig_block
            skills.cache_lookup = orig_cache_lookup
            skills.cache_store = orig_cache_store
            skills.record_lesson_from_critic = orig_record

        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        assert any(e.get("type") == "answer" and e.get("text") == "corrected answer"
                   for e in events), \
            "the (already-revised) answer must still reach the client unchanged"
        assert calls["n"] == 0, \
            "an UNGROUNDED_NUMBER finding must never be recorded as a lesson"


def test_critic_category_reaches_the_lesson_gate_through_the_real_agent_loop():
    """Guards the app.llm -> app.routers.chat SEAM for critic_category, as
    opposed to the two tests above (and test_critic_revision_records_a_lesson),
    which inject a canned AgentResult directly and so cannot see a category
    that llm.py forgot to copy from Critique onto AgentResult, or that chat.py
    read under the wrong name. This runs the REAL app.llm.stream_agent (not a
    stub), with only the network-touching leaves faked, so a broken hand-off
    anywhere along that path surfaces here. Without the wiring this fails
    CLOSED -- no exception, no error event, the lesson is just silently never
    recorded, exactly the "lessons silently stopped" symptom the PR names."""
    from app import critic as critic_mod
    from app import llm as llm_mod
    from app.config import get_settings as _get_settings
    from app.tools import registry as tool_registry

    with TestClient(app) as c:
        _login(c)
        captured = {}
        calls = {"chat": 0}

        async def fake_chat(client, model, messages, tools=None):
            if tools is None:  # generate_title's own call via the same _chat name
                return {"choices": [{"message": {"content": "a title"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
            calls["chat"] += 1
            if calls["chat"] == 1:
                return {"choices": [{"message": {"content": "",
                    "tool_calls": [{"id": "c1", "type": "function", "function": {
                        "name": "run_sql",
                        "arguments": '{"sql": "SELECT SUM(x) FROM c_a"}'}}]}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
            if calls["chat"] == 2:
                return {"choices": [{"message": {"content": "Draft: 4,000,000 (wrong)."}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
            if calls["chat"] == 3:
                return {"choices": [{"message": {"content": "",
                    "tool_calls": [{"id": "c2", "type": "function", "function": {
                        "name": "run_sql",
                        "arguments": '{"sql": "SELECT SUM(x) FROM c_a WHERE majornum=1"}'
                    }}]}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
            return {"choices": [{"message": {"content": "Corrected: 1,000,000."}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        async def fake_review(question, sql_log, answer, *a, **kw):
            return critic_mod.Critique(ok=False, headline="Add majornum=1.",
                                       description="missing majornum=1; ~4x overcount",
                                       category="CIP_ROLLUP")

        async def fake_retry_missing_figure(question, answer):
            return llm_mod._FigureRetry()  # fails open — no network, no figure

        real_settings = _get_settings()
        patched_settings = real_settings.model_copy(update={
            "llm_api_key": "test-key",
            # Keep the plain-text tool-call path (matches test_critic.py's
            # agent-loop fakes below) rather than the emit_answer tool-call
            # shape, which these fakes don't speak.
            "structured_emission_enabled": False,
            "critic_enabled": True,
        })

        orig_agent = chat_router.stream_agent
        orig_get_settings = llm_mod.get_settings
        orig_chat = llm_mod._chat
        orig_review = llm_mod.critic.review
        orig_retry = llm_mod.retry_missing_figure
        orig_dispatch = tool_registry.dispatch
        orig_block = skills.retrieve_skills_block
        orig_cache_lookup = skills.cache_lookup
        orig_cache_store = skills.cache_store
        orig_record = skills.record_lesson_from_critic

        chat_router.stream_agent = llm_mod.stream_agent
        llm_mod.get_settings = lambda: patched_settings
        llm_mod._chat = fake_chat
        llm_mod.critic.review = fake_review
        llm_mod.retry_missing_figure = fake_retry_missing_figure
        tool_registry.dispatch = lambda *a, **k: "OK — 1 row(s)"
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q, _uid=None: None
        skills.cache_store = lambda *a, **k: None
        skills.record_lesson_from_critic = \
            lambda q, sql, headline, description: captured.update(
                q=q, sql=sql, headline=headline, description=description)
        try:
            r = c.post("/api/chat/stream", json={"question": "national bachelor total, seam"})
        finally:
            chat_router.stream_agent = orig_agent
            llm_mod.get_settings = orig_get_settings
            llm_mod._chat = orig_chat
            llm_mod.critic.review = orig_review
            llm_mod.retry_missing_figure = orig_retry
            tool_registry.dispatch = orig_dispatch
            skills.retrieve_skills_block = orig_block
            skills.cache_lookup = orig_cache_lookup
            skills.cache_store = orig_cache_store
            skills.record_lesson_from_critic = orig_record

        assert r.status_code == 200, r.text
        assert captured.get("headline") == "Add majornum=1.", \
            f"a CIP_ROLLUP finding produced through the real agent loop must reach " \
            f"record_lesson_from_critic: {captured}"
        assert "majornum" in captured.get("description", ""), captured


# ---------------------------------------------------------------------------
# feedback-distilled lessons (the background helpers)
#
# The production call site fires _record_feedback_lesson FIRE-AND-FORGET (a
# detached task, so the SSE body can close and re-enable the composer without
# waiting for the distiller's LLM round-trip). A detached task can't be awaited
# by a test event loop, so these exercise the helpers DIRECTLY — awaited and
# drained — which both covers them deterministically AND documents the
# contract. The call site itself is gated on a configured key, so key-free CI
# never spawns the detached task (a task still pending at loop teardown would
# non-deterministically drop the streaming generator's coverage).
# ---------------------------------------------------------------------------

def test_record_feedback_lesson_records_when_distiller_finds_a_rule():
    captured = {}

    async def _distill(history, question):
        return (("Keep the established scope.",
                 "inherit the award-level scope on follow-ups"),
                llmhttp.Usage(prompt_tokens=6, completion_tokens=2))

    orig_distill = chat_router.feedback.distill_feedback
    orig_record = skills.record_lesson_from_feedback
    chat_router.feedback.distill_feedback = _distill
    skills.record_lesson_from_feedback = (
        lambda q, headline, description: captured.update(
            q=q, headline=headline, description=description))
    try:
        asyncio.run(chat_router._record_feedback_lesson(
            [{"role": "user", "content": "prior turn"}], "you should have kept the scope"))
    finally:
        chat_router.feedback.distill_feedback = orig_distill
        skills.record_lesson_from_feedback = orig_record

    assert captured.get("q") == "you should have kept the scope", captured
    assert captured.get("headline") == "Keep the established scope.", captured
    assert "scope" in captured.get("description", ""), captured


def test_record_feedback_lesson_noops_when_distiller_finds_nothing():
    calls = {"n": 0}

    async def _distill(history, question):
        # No lesson found — but the call still happened and still cost money,
        # which is the common case and must still be billed.
        return None, llmhttp.Usage(prompt_tokens=6, completion_tokens=2)

    orig_distill = chat_router.feedback.distill_feedback
    orig_record = skills.record_lesson_from_feedback
    chat_router.feedback.distill_feedback = _distill
    skills.record_lesson_from_feedback = lambda *a, **k: calls.__setitem__("n", calls["n"] + 1)
    try:
        asyncio.run(chat_router._record_feedback_lesson(
            [{"role": "user", "content": "x"}], "thanks, that's great!"))
    finally:
        chat_router.feedback.distill_feedback = orig_distill
        skills.record_lesson_from_feedback = orig_record
    assert calls["n"] == 0, "no finding must record no lesson"


def test_record_feedback_lesson_swallows_distiller_errors():
    """The answer is already persisted when this runs, so a distiller failure
    only costs a missed lesson — it must never raise out of the task."""
    async def _boom(history, question):
        raise RuntimeError("distiller exploded")

    orig_distill = chat_router.feedback.distill_feedback
    chat_router.feedback.distill_feedback = _boom
    try:
        # Must NOT raise.
        asyncio.run(chat_router._record_feedback_lesson(
            [{"role": "user", "content": "x"}], "you got that wrong"))
    finally:
        chat_router.feedback.distill_feedback = orig_distill


def test_fire_and_forget_runs_then_self_discards():
    ran = {"ok": False}

    async def _job():
        ran["ok"] = True

    async def _drive():
        chat_router._fire_and_forget(_job())
        await asyncio.gather(*list(chat_router._background_tasks))
        await asyncio.sleep(0)  # let the done-callback (discard) run

    asyncio.run(_drive())
    assert ran["ok"] is True, "the scheduled coroutine must run"
    assert len(chat_router._background_tasks) == 0, "the task must self-discard when done"


# ---------------------------------------------------------------------------
# conversation list / get / delete
# ---------------------------------------------------------------------------

def test_conversation_crud():
    with TestClient(app) as c:
        _login(c)
        r = _post_turn(c, "a crud test question")
        conv_id = next(e["id"] for e in _parse_sse(r.text) if e["type"] == "conversation")

        lst = c.get("/api/chat/conversations").json()
        assert any(x["id"] == conv_id for x in lst), lst

        got = c.get(f"/api/chat/conversations/{conv_id}")
        assert got.status_code == 200 and len(got.json()) == 2, got.text

        missing = c.get("/api/chat/conversations/999999")
        assert missing.status_code == 404, missing.text

        deleted = c.delete(f"/api/chat/conversations/{conv_id}")
        assert deleted.status_code == 200 and deleted.json()["ok"] is True

        gone = c.get(f"/api/chat/conversations/{conv_id}")
        assert gone.status_code == 404, gone.text

        missing_delete = c.delete("/api/chat/conversations/999999")
        assert missing_delete.status_code == 404, missing_delete.text


def test_conversation_rename():
    """PATCH /conversations/{id} renames without reordering the sidebar.

    Regressions caught: (a) losing the ownership check (a user renaming
    someone else's chat); (b) a rename bumping updated_at and silently
    reshuffling the recency-ordered sidebar; (c) blank/whitespace titles
    wiping a chat's name."""
    with TestClient(app) as c:
        _login(c)
        r = _post_turn(c, "a rename test question")
        conv_id = next(e["id"] for e in _parse_sse(r.text) if e["type"] == "conversation")
        before = next(x for x in c.get("/api/chat/conversations").json()
                      if x["id"] == conv_id)

        renamed = c.patch(f"/api/chat/conversations/{conv_id}",
                          json={"title": "  My renamed chat  "})
        assert renamed.status_code == 200, renamed.text
        assert renamed.json() == {"ok": True, "title": "My renamed chat"}, renamed.text

        after = next(x for x in c.get("/api/chat/conversations").json()
                     if x["id"] == conv_id)
        assert after["title"] == "My renamed chat", after
        # A rename is metadata-only: updated_at (the sidebar's recency order)
        # must NOT move, or renaming an old chat would jump it to the top.
        assert after["updated_at"] == before["updated_at"], (before, after)

        # Blank or whitespace-only titles are rejected, not stored.
        blank = c.patch(f"/api/chat/conversations/{conv_id}", json={"title": "   "})
        assert blank.status_code == 400, blank.text
        # Absurdly long titles are rejected (the UI caps at the same bound).
        long = c.patch(f"/api/chat/conversations/{conv_id}", json={"title": "x" * 201})
        assert long.status_code == 400, long.text

        missing = c.patch("/api/chat/conversations/999999", json={"title": "t"})
        assert missing.status_code == 404, missing.text


def test_conversation_rename_not_owned_404():
    """Another signed-in user renaming my conversation must 404 (never 200,
    and indistinguishable from not-found so ids can't be probed)."""
    with TestClient(app) as c:
        _login(c, "admin@example.edu")
        r = _post_turn(c, "admin's own conversation")
        conv_id = next(e["id"] for e in _parse_sse(r.text) if e["type"] == "conversation")

        c.post("/api/admin/allowlist", json={"email": "renamer@example.edu"})
        c2 = TestClient(app)
        _login(c2, "renamer@example.edu")
        r2 = c2.patch(f"/api/chat/conversations/{conv_id}", json={"title": "mine now"})
        assert r2.status_code == 404, r2.text
        # And the title is untouched.
        mine = c.get("/api/chat/conversations").json()
        assert next(x for x in mine if x["id"] == conv_id)["title"] != "mine now"


# ---------------------------------------------------------------------------
# CSV download error branches (the success path is covered in test_backend.py)
# ---------------------------------------------------------------------------

def test_download_csv_unknown_message_404():
    with TestClient(app) as c:
        _login(c)
        r = c.get("/api/chat/messages/999999/download.csv")
        assert r.status_code == 404, r.text


def test_download_csv_no_sql_log_400():
    with TestClient(app) as c:
        _login(c)
        r = _post_turn(c, "a question with no sql", answer_text="just prose",
                       sql_log=[])
        done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
        msg_id = done["message_id"]
        csv_r = c.get(f"/api/chat/messages/{msg_id}/download.csv")
        assert csv_r.status_code == 400, csv_r.text


# ---------------------------------------------------------------------------
# no-data guard: a fresh deploy with no ipeds.db dataset gets a friendly
# notice instead of a raw SQL error, creates NO conversation, and never runs
# the agent. chat_router.ipeds_years is patched to [] the same way the rest of
# this suite patches chat_router.stream_agent -- and BOTH are restored in a
# finally so the rest of the suite (which relies on the real ipeds.db existing
# in this test environment) is unaffected.
# ---------------------------------------------------------------------------

def _exploding_agent(question, *, history=None, skills_block=""):
    raise AssertionError("stream_agent must not run when there is no ipeds.db dataset")
    yield  # pragma: no cover - unreachable; keeps this an async generator


def test_no_data_guard_admin_wording_and_skips_agent():
    with TestClient(app) as c:
        _login(c, "admin@example.edu")
        orig_years = chat_router.ipeds_years
        orig_agent = chat_router.stream_agent
        chat_router.ipeds_years = lambda: []
        chat_router.stream_agent = _exploding_agent
        try:
            before = c.get("/api/chat/conversations").json()
            r = c.post("/api/chat/stream", json={"question": "how many CS bachelors last year"})
            after = c.get("/api/chat/conversations").json()
        finally:
            chat_router.ipeds_years = orig_years
            chat_router.stream_agent = orig_agent

        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        answer = next(e for e in events if e["type"] == "answer")
        assert "No IPEDS dataset is loaded yet" in answer["text"], answer
        assert "Admin" in answer["text"] and "Imports" in answer["text"], \
            f"admin wording must route to Admin -> Imports: {answer}"
        done = next(e for e in events if e["type"] == "done")
        assert done.get("no_data") is True, done
        assert after == before, "a no-data reply must not create/alter a conversation"


def test_no_data_guard_non_admin_wording_and_skips_agent():
    with TestClient(app) as c:
        _login(c, "admin@example.edu")
        c.post("/api/admin/allowlist", json={"email": "nodata-user@example.edu"})
        c2 = TestClient(app)
        _login(c2, "nodata-user@example.edu")

        orig_years = chat_router.ipeds_years
        orig_agent = chat_router.stream_agent
        chat_router.ipeds_years = lambda: []
        chat_router.stream_agent = _exploding_agent
        try:
            before = c2.get("/api/chat/conversations").json()
            r = c2.post("/api/chat/stream", json={"question": "how many CS bachelors last year"})
            after = c2.get("/api/chat/conversations").json()
        finally:
            chat_router.ipeds_years = orig_years
            chat_router.stream_agent = orig_agent

        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        answer = next(e for e in events if e["type"] == "answer")
        assert "No IPEDS dataset is loaded yet" in answer["text"], answer
        assert "administrator" in answer["text"].lower(), \
            f"non-admin wording must tell them to wait for an administrator: {answer}"
        assert "Imports" not in answer["text"], \
            f"non-admin wording must not route to the admin-only Imports UI: {answer}"
        done = next(e for e in events if e["type"] == "done")
        assert done.get("no_data") is True, done
        assert after == before, "a no-data reply must not create/alter a conversation"


def test_download_csv_rejected_sql_400():
    with TestClient(app) as c:
        _login(c)
        r = _post_turn(c, "a question with bad sql", answer_text="answer",
                       sql_log=["DROP TABLE c_a"])
        done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
        msg_id = done["message_id"]
        csv_r = c.get(f"/api/chat/messages/{msg_id}/download.csv")
        assert csv_r.status_code == 400, csv_r.text


def test_download_csv_picks_table_query_not_a_trailing_count():
    # The listing rule makes the answer's LAST SQL a scalar COUNT(*) (for the
    # "full size" note) — so re-running the last SQL hands back "total: N", not
    # the ranking the user saw. The CSV must be the TABLE query (many rows).
    listing = ("SELECT year, SUM(ctotalt) a FROM c_a "
               "WHERE awlevel=3 AND majornum=1 AND cipcode='99' GROUP BY year")
    count = "SELECT COUNT(*) total FROM c_a"
    with TestClient(app) as c:
        _login(c)
        r = _post_turn(c, "associate's by year, ranked", answer_text="see table",
                       sql_log=[listing, count])  # count is LAST
        msg_id = next(e for e in _parse_sse(r.text) if e["type"] == "done")["message_id"]
        # The client passes the shown table's column count (2) → the listing wins.
        csv_r = c.get(f"/api/chat/messages/{msg_id}/download.csv?cols=2")
        assert csv_r.status_code == 200, csv_r.text
        header, *data = csv_r.text.strip().splitlines()
        assert header == "year,a", f"got the count, not the listing: {header}"
        assert len(data) >= 1, "expected the listing's data rows, not an empty page"
        # Even WITHOUT the hint, a 1-column count must not beat the 2-column listing.
        no_hint = c.get(f"/api/chat/messages/{msg_id}/download.csv")
        assert no_hint.text.strip().splitlines()[0] == "year,a", no_hint.text[:80]


def test_download_csv_prefers_the_last_matching_query():
    # When two queries share the shown table's column count, the LAST one wins
    # (closest to the answer) — not the one with the most rows.
    first = ("SELECT year, SUM(ctotalt) a FROM c_a "
             "WHERE awlevel=3 AND majornum=1 AND cipcode='99' GROUP BY year")
    last = "SELECT awlevel, COUNT(*) b FROM c_a GROUP BY awlevel"
    with TestClient(app) as c:
        _login(c)
        r = _post_turn(c, "two 2-column queries", answer_text="see table",
                       sql_log=[first, last])
        msg_id = next(e for e in _parse_sse(r.text) if e["type"] == "done")["message_id"]
        csv_r = c.get(f"/api/chat/messages/{msg_id}/download.csv?cols=2")
        assert csv_r.status_code == 200, csv_r.text
        assert csv_r.text.strip().splitlines()[0] == "awlevel,b", \
            f"expected the LAST 2-col query, got: {csv_r.text[:80]}"


def test_version_endpoint_authed_and_shape():
    # GET /api/version is signed-in only and returns the three-key payload the
    # About dialog / Admin banner consume. version_info is stubbed so the test
    # never touches GitHub.
    from app import version as version_mod
    orig = version_mod.version_info
    version_mod.version_info = lambda: {
        "current": "0.1.0", "latest": "0.2.0", "update_available": True}
    try:
        with TestClient(app) as c:
            assert c.get("/api/version").status_code == 401  # signed-out
            _login(c)
            r = c.get("/api/version")
            assert r.status_code == 200, r.text
            assert r.json() == {"current": "0.1.0", "latest": "0.2.0",
                                "update_available": True}
    finally:
        version_mod.version_info = orig


def test_results_for_storage_caps_and_drops_largest_over_budget():
    """Persisted result rows are capped so a wide brief can't bloat app.db. Over
    the byte ceiling, the LARGEST result is dropped first (a headline usually
    derives from a compact table, so the small recent-years/ranking result is
    the one worth keeping)."""
    assert chat_router._results_for_storage([]) is None
    assert chat_router._results_for_storage(None) is None
    small = QueryResult(columns=["n"], rows=[(1,), (2,)], row_count=2)
    # Big enough to exceed the byte ceiling even AFTER the 200-row cap.
    huge = QueryResult(columns=["a", "b"],
                       rows=[(i, "x" * 500) for i in range(400)], row_count=400)
    out = chat_router._results_for_storage([small, huge])
    # The small result survives; the oversized one is dropped under the ceiling.
    assert out is not None and len(out) == 1, out
    assert out[0]["columns"] == ["n"], out


def test_results_for_storage_caps_a_lone_oversized_result():
    """THE REGRESSION: the drop-largest loop was guarded by `len(blobs) > 1`, so
    it was a no-op for a single result and stopped as soon as dropping left one —
    the survivor was NEVER measured. RESULT_STORE_MAX_BYTES therefore meant "at
    most one result may exceed it, unbounded". `to_storage` caps rows (200) but
    not WIDTH, and one value may reach SQL_MAX_VALUE_BYTES (1 MiB), so 200 rows
    of a wide SELECT * is comfortably megabytes — written twice, into
    messages.results AND query_cache.results.

    The lone survivor is now shrunk to fit instead of trusted.
    """
    # ~2 MB before any capping: well past the 64 KB ceiling on its own.
    huge = QueryResult(columns=["a", "b"],
                       rows=[(i, "x" * 10_000) for i in range(200)], row_count=200)
    out = chat_router._results_for_storage([huge])
    assert out is not None, "a lone oversized result should shrink, not vanish"
    assert len(json.dumps(out)) <= chat_router.RESULT_STORE_MAX_BYTES, (
        f"stored {len(json.dumps(out))} bytes, ceiling is "
        f"{chat_router.RESULT_STORE_MAX_BYTES}")
    # Shrinking drops ROWS, never the shape — grounding indexes columns.
    assert out[0]["columns"] == ["a", "b"], out[0]["columns"]

    # A single row too wide to ever fit stores nothing rather than an unbounded
    # blob — the terminating case of the halving loop.
    monster = QueryResult(columns=["a"], rows=[("x" * 200_000,)], row_count=1)
    assert chat_router._results_for_storage([monster]) is None

    # Anti-vacuous: an ordinary result is untouched, so "shrink everything to
    # None" would not pass this test.
    ordinary = QueryResult(columns=["n"], rows=[(1,), (2,)], row_count=2)
    assert chat_router._results_for_storage([ordinary]) == [
        {"columns": ["n"], "rows": [[1], [2]]}]


def test_results_for_storage_flags_a_blob_it_halved():
    """THE REGRESSION for the halving loop specifically: `truncated` must be
    set on the surviving blob BEFORE it is halved, so the key's own bytes are
    inside the RESULT_STORE_MAX_BYTES ceiling the loop measures. Setting the
    flag AFTER halving would let a borderline blob slip back over the ceiling
    -- the loop would have measured a smaller (unflagged) blob than the one it
    actually stores, which is exactly the gap app/grounding.py's truncation
    gate depends on being closed."""
    huge = QueryResult(columns=["a", "b"],
                       rows=[(i, "x" * 10_000) for i in range(200)], row_count=200)
    out = chat_router._results_for_storage([huge])
    assert out is not None
    # `.get()`, not `[...]` -- a not-yet-fixed to_storage omits the key
    # entirely, and this must fail as a clean AssertionError, not a KeyError
    # the hand-rolled `check()` runner (catches AssertionError only) can't
    # report.
    assert out[0].get("truncated") is True, out[0]
    assert len(json.dumps(out)) <= chat_router.RESULT_STORE_MAX_BYTES, (
        f"stored {len(json.dumps(out))} bytes, ceiling is "
        f"{chat_router.RESULT_STORE_MAX_BYTES}")


def test_csv_probes_are_time_bounded_but_the_winning_rerun_is_not():
    """THE REGRESSION: _select_table_sql probes EVERY query in the answer's
    sql_log at `LIMIT 1`, and LIMIT 1 bounds the ROWS returned, never the WORK
    done — so each probe could burn the full sql_timeout_seconds (25s). sql_log
    records every attempt the agent made, failures included, so 5-8 candidates
    is routine and one CSV request could hold a threadpool worker for two to
    three minutes.

    The probes are now bounded. The WINNING re-run deliberately is NOT — that is
    the query the user actually asked for, and it keeps the full budget.
    """
    calls = []
    real = chat_router.run_sql

    def recording_run_sql(sql, **kw):
        calls.append({"sql": sql, "limit": kw.get("limit"), "timeout": kw.get("timeout")})
        return QueryResult(columns=["year", "a"], rows=[(2025, 1)], row_count=1)

    chat_router.run_sql = recording_run_sql
    try:
        chat_router._select_table_sql(
            ["SELECT 1 a, 2 b", "SELECT 3 c, 4 d"], cols=2, cap=100)
    finally:
        chat_router.run_sql = real

    probes = [c for c in calls if c["limit"] == 1]
    assert len(probes) == 2, f"expected one probe per candidate: {calls}"
    assert all(p["timeout"] == chat_router.CSV_PROBE_TIMEOUT_SECONDS for p in probes), (
        f"an unbounded probe can burn the full 25s SQL budget: {probes}")
    winner = calls[-1]
    assert winner["limit"] == 100, winner
    assert winner["timeout"] is None, (
        f"the winning re-run must keep the full default budget: {winner}")


def test_cache_hit_keeps_the_conversation_grounding_chain_intact():
    """THE REGRESSION: a cache hit persisted messages.results=NULL, so the NEXT
    turn in that conversation had nothing to ground a recited number against and
    silently graded `unchecked`.

    The assertion that matters is the second one — that _load_prior_results (the
    thing a later turn actually calls) can see the cached turn's rows. Checking
    only that the column is non-NULL would pass on a blob the grounding path
    can't read.
    """
    cached_results = [QueryResult(columns=["year", "awards"],
                                  rows=[(2024, 61234)], row_count=1).to_storage()]
    orig_lookup = skills.cache_lookup
    orig_agent = chat_router.stream_agent
    orig_block = skills.retrieve_skills_block
    skills.cache_lookup = lambda q, uid: {
        "final_sql": "SELECT year, awards FROM c_a",
        "answer_md": "61,234 awards in 2024.",
        "figure": None, "suggestions": None,
        "results": cached_results, "results_truncated": False,
        "matched_question": q, "similarity": 0.99,
    }
    skills.retrieve_skills_block = lambda q: ("", [])
    try:
        with TestClient(app) as c:
            _login(c)
            r = c.post("/api/chat/stream", json={"question": "nursing awards in 2024"})
            assert r.status_code == 200, r.text
            events = _parse_sse(r.text)
            done = next(e for e in events if e.get("type") == "done")
            assert done.get("cached") is True, f"expected a cache hit: {events}"
            msg_id = done["message_id"]
    finally:
        skills.cache_lookup = orig_lookup
        chat_router.stream_agent = orig_agent
        skills.retrieve_skills_block = orig_block

    con = connect()
    try:
        row = con.execute(
            "SELECT id, conversation_id, results FROM messages WHERE id=?",
            (msg_id,)).fetchone()
        assert row is not None, "the cached turn was not persisted"
        assert row["results"] is not None, \
            "a cache hit must persist the rows backing its answer, not NULL"

        # The chain itself: what a LATER turn in this conversation would borrow.
        prior = chat_router._load_prior_results(con, row["conversation_id"])
        assert prior and prior[0].rows[0][1] == 61234, \
            f"a later turn could not borrow the cached turn's rows: {prior!r}"
    finally:
        con.close()


def test_load_prior_results_respects_before_id_window():
    """An edit/rerun grounds only against results that will survive the pending
    delete — _load_prior_results must exclude messages at/after before_id, exactly
    like _load_history. Otherwise a re-asked turn could ground against data from
    the very messages it's about to replace."""
    con = connect()
    try:
        uid = con.execute(
            "INSERT INTO users(email, created_at) VALUES "
            "('prior-results@x.edu', 0)").lastrowid
        cur = con.execute("INSERT INTO conversations(user_id, title, created_at, "
                          "updated_at) VALUES (?,'t',0,0)", (uid,))
        conv = cur.lastrowid
        ids = []
        for n in (10, 20, 30):  # three assistant turns, each with one result
            blob = json.dumps([QueryResult(columns=["v"], rows=[(n,)],
                                           row_count=1).to_storage()])
            c2 = con.execute(
                "INSERT INTO messages(conversation_id, role, content, results, "
                "created_at) VALUES (?,?,?,?,?)", (conv, "assistant", "a", blob, n))
            ids.append(c2.lastrowid)
        con.commit()

        # No bound: all three turns' results load, chronologically.
        vals = [r.rows[0][0] for r in chat_router._load_prior_results(con, conv)]
        assert vals == [10, 20, 30], vals
        # before_id = the last message: it and anything after are excluded.
        vals = [r.rows[0][0]
                for r in chat_router._load_prior_results(con, conv, before_id=ids[2])]
        assert vals == [10, 20], vals
    finally:
        con.execute("DELETE FROM messages WHERE conversation_id=?", (conv,))
        con.execute("DELETE FROM conversations WHERE id=?", (conv,))
        con.execute("DELETE FROM users WHERE email='prior-results@x.edu'")
        con.commit()


def test_csv_export_survives_a_failed_query_in_the_answers_log():
    """FOUND LIVE (conversation 23): an answer's CSV export failed permanently.

    sql_log records EVERY query attempt, INCLUDING ones SQLite rejected — the
    agent runs a query, gets an error, fixes it, re-runs, and all of that is
    persisted. So a perfectly good answer routinely carries a query that cannot
    run. The real one was `ambiguous column name: year`, from a c_a/hd JOIN the
    model corrected on its very next attempt.

    _select_table_sql probes every candidate but caught only
    SQLValidationError/SQLTimeoutError, so a plain sqlite3.OperationalError
    escaped both the loop AND download_csv's try/except. The export 500'd with
    no `detail`, reaching the user as the generic "Couldn't build that CSV. Try
    again in a moment." — and retrying could never help, because the failing
    query is stored and failed identically every time.
    """
    good = "SELECT year, COUNT(*) AS n FROM _years GROUP BY year"
    # The same shape that broke it: passes validation, fails at execution.
    ambiguous = "SELECT year FROM _years a JOIN _years b ON a.year = b.year"

    # Guard the fixture: if this ever stops raising, or starts raising something
    # the OLD code already handled, the test below proves nothing.
    raised = None
    try:
        run_sql(ambiguous, limit=1)
    except Exception as e:  # noqa: BLE001
        raised = e
    assert raised is not None, "the fixture query no longer fails — pick another"
    assert not isinstance(raised, (SQLValidationError, SQLTimeoutError)), (
        f"fixture must raise what the OLD handler missed, got {type(raised).__name__}")

    # Failing candidate FIRST, so the probe loop meets it before anything usable.
    result = chat_router._select_table_sql([ambiguous, good], 2, 100)
    assert result.columns == ["year", "n"], result.columns
    assert result.rows, "the runnable candidate should have produced rows"

    # With nothing runnable it still degrades to the clean 400 path rather than
    # leaking a 500.
    try:
        chat_router._select_table_sql([ambiguous], 2, 100)
        raise AssertionError("expected SQLValidationError when nothing runs")
    except SQLValidationError:
        pass


def test_every_persisted_turn_field_reaches_the_reader_and_the_done_event():
    """THE regression this repo keeps paying for, mechanized.

    A displayable field on an assistant message had to be added by hand to ~10
    places — a migration, _persist's signature, the INSERT columns, its values
    tuple, the `?` count, two call sites, the `done` dict, get_conversation's
    SELECT, and three spots in Chat.jsx. Nothing checked any pair against any
    other, and it shipped TWO defects: `results_truncated` (missed in the
    SELECT) and `table_cells_matched` (missed on the live path).

    The failure is ASYMMETRIC, which is why review keeps missing it: the reload
    path inherits new fields for free (Chat.jsx spreads `...m`) while the live
    `done` path enumerates them by hand — so a miss renders CORRECTLY after a
    refresh and wrongly only on the turn that produced it.

    SCHEMA PARITY (the load-bearing half) — the real messages table, minus
    structural and explicitly backend-only columns, must equal
    MESSAGE_READ_COLUMNS. Adding a migration column now fails HERE until it is
    wired up or deliberately excluded. This is the half that catches the NEXT
    field, not just the last two. Then a ROUND TRIP: a turn populating every
    field comes back intact from get_conversation.

    What this test does NOT check any more: whether DONE_EVENT_FIELDS actually
    reaches a real `done` SSE frame. That used to be `set(DONE_EVENT_FIELDS) -
    set(MESSAGE_READ_COLUMNS)` — two static tuples compared to each other,
    never touching chat.py's `done = {...}` dict literal. Deleting a key from
    that literal left it green, because there was nothing there for the
    literal to disagree WITH. See
    test_done_event_carries_every_field_on_all_three_message_bearing_paths (a
    real stream, a real parsed `done` frame) and the ungrounded-turn
    live-vs-reload agreement test below for the real check.
    """
    # Everything runs inside the TestClient context: the app's lifespan is what
    # creates/migrates app.db, so PRAGMA table_info(messages) is EMPTY before it
    # — and an empty schema would make the parity check below vacuously pass,
    # which is the one failure mode a schema gate must not have.
    with TestClient(app) as c:
        con = connect()
        conv = None
        try:
            # --- 1. schema parity (the load-bearing half) --------------------
            actual = {r["name"] for r in con.execute("PRAGMA table_info(messages)")}
            assert actual, "no messages schema to check against — the gate would be vacuous"
            classified = set(chat_router.MESSAGE_TURN_COLUMNS) | chat_router._STRUCTURAL
            unclassified = actual - classified
            assert not unclassified, (
                f"messages has column(s) {sorted(unclassified)} that no field list "
                "knows about — add them to MESSAGE_TURN_COLUMNS (so they persist AND "
                "reach the reader), or to _STRUCTURAL/_BACKEND_ONLY with a reason")
            stale = classified - actual
            assert not stale, f"field list names column(s) not in the schema: {sorted(stale)}"
            assert set(chat_router.MESSAGE_READ_COLUMNS) == (
                set(chat_router.MESSAGE_TURN_COLUMNS) - chat_router._BACKEND_ONLY)

            # --- 2. round trip ----------------------------------------------
            # Own the conversation with the address _login already signs in as:
            # a fresh user would not be allowlisted, so no magic link is minted
            # and the sign-in silently has nothing to consume.
            con.execute("INSERT OR IGNORE INTO users(email, created_at) "
                        "VALUES ('admin@example.edu', 0)")
            uid = con.execute("SELECT id FROM users WHERE email='admin@example.edu'"
                              ).fetchone()["id"]
            cur = con.execute("INSERT INTO conversations(user_id, title, created_at, "
                              "updated_at) VALUES (?,'parity',0,0)", (uid,))
            conv = cur.lastrowid
            con.commit()
        finally:
            con.close()

        # Attribute access, not a positional unpack: _persist returns a
        # _PersistResult NamedTuple whose fourth field (turn_values) is what
        # lets the `done` event project its values out of the SAME mapping the
        # INSERT used — unpacking by position here would keep that field
        # pinned to a fixed arity for no reason this test needs, and is
        # exactly the kind of positional fragility the shared-mapping fix is
        # removing everywhere else.
        persisted = chat_router._persist(
            uid, conv, "q", "a",
            sql_log=["SELECT 1"], model="m", tokens=7, cached=False, ok=True,
            thinking=[{"kind": "sql", "text": "SELECT 1"}],
            figure={"value": 1, "label": "x"},
            # Every field populated at once. A figure AND a clarify never occur
            # together in a real turn — this exercises the PLUMBING, not the
            # semantics, and any field left None would silently satisfy the
            # "was it persisted?" check below.
            suggestions=["more?"],
            clarify={"question": "which?", "options": ["a", "b"]},
            results=[{"columns": ["v"], "rows": [[1]]}],
            duration_ms=1234, results_truncated=True, figure_grounding="exact",
            table_grounding="matched", table_cells_checked=4, table_cells_matched=4)
        msg_id = persisted.message_id

        con = connect()
        try:
            row = con.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
            for col in chat_router.MESSAGE_TURN_COLUMNS:
                assert row[col] is not None, f"{col} was not persisted"
        finally:
            con.close()

        # Read it back the way the browser does.
        _login(c)
        r = c.get(f"/api/chat/conversations/{conv}")
        assert r.status_code == 200, r.text
        assistant = [m for m in r.json() if m["role"] == "assistant"][-1]
        for col in chat_router.MESSAGE_READ_COLUMNS:
            assert col in assistant, f"get_conversation dropped {col}"
            assert assistant[col] is not None, f"get_conversation returned NULL {col}"
        for col in chat_router._BACKEND_ONLY:
            assert col not in assistant, f"{col} is backend-only but reached the browser"

        con = connect()
        try:
            con.execute("DELETE FROM messages WHERE conversation_id=?", (conv,))
            con.execute("DELETE FROM conversations WHERE id=?", (conv,))
            con.commit()
        finally:
            con.close()


def _assert_done_fields_scalar_and_match_reload(c, conv_id, done):
    """Two properties the DERIVED form of DONE_EVENT_FIELDS depends on for
    safety, checked against a REAL `done` frame and a REAL reload — never two
    lists of names compared to each other.

    1. SCALARS ONLY. `hydrate()` JSON-parses exactly five named columns
       (sql_log, thinking, figure, suggestions, clarify — Chat.jsx:200-207)
       into native lists/dicts on the RELOAD path; the live `done` event is
       handed to the message object as-is, with no parse step anywhere. If
       DONE_EVENT_FIELDS is ever derived as an opt-OUT of MESSAGE_READ_COLUMNS
       (so a new migration column rides `done` automatically unless
       excluded), a JSON-holding column left out of `_OWN_STREAMED_EVENT`
       would put its RAW native list/dict straight onto the wire (`_sse`
       serializes whatever `done` holds) instead of the JSON-encoded TEXT
       every other consumer expects — a live message carrying a shape nothing
       else on that field ever produces. This fails the moment such a column
       is added and forwarded unserialized; it does not, and cannot, detect a
       column added as an already-`json.dumps`'d STRING (a plain string is a
       valid scalar on both sides) — that half of the asymmetry can only be
       caught by eye, or by extending `_OWN_STREAMED_EVENT`/`hydrate()`
       together, which is exactly the reviewable act the derivation is meant
       to force.
    2. LIVE == RELOAD, field by field, comparing two REAL payloads instead of
       two static lists. This is the assertion that actually pins the bug
       class: it would have caught both shipped defects (results_truncated,
       table_cells_matched missed on the live path) and the table-cell-count
       0-vs-NULL divergence, none of which the old constant-vs-constant guard
       could ever see.

    NB `True == 1` in Python, so live's booleans (results_truncated) comparing
    equal to reload's stored ints (0/1) is CORRECT and deliberate — once both
    sides are built from the same mapping, 1/0 is what reload has always sent
    and no normalizer should paper over that.
    """
    for f in chat_router.DONE_EVENT_FIELDS:
        v = done.get(f)
        assert v is None or isinstance(v, (int, float, str, bool)), (
            f"done[{f!r}] is a {type(v).__name__}, not a scalar -- a "
            "list/dict-holding column must not auto-join DONE_EVENT_FIELDS "
            f"unserialized (add it to _OWN_STREAMED_EVENT instead): {v!r}")

    msgs = c.get(f"/api/chat/conversations/{conv_id}").json()
    assistant = [m for m in msgs if m["role"] == "assistant"][-1]
    for f in chat_router.DONE_EVENT_FIELDS:
        assert done.get(f) == assistant.get(f), (
            f"live and reload disagree on {f!r}: live={done.get(f)!r} "
            f"reload={assistant.get(f)!r}")


def test_done_event_carries_every_field_on_all_three_message_bearing_paths():
    """THE regression this repo keeps paying for, this time against a REAL `done`
    frame instead of two constants compared to each other. The old guard
    (`set(DONE_EVENT_FIELDS) - set(MESSAGE_READ_COLUMNS)`) never opened a stream
    and never built a `done` dict, so deleting a key from the literal at
    chat.py:~725 left it green. This drives all THREE message-bearing branches
    — the ones with no shared AgentResult to fold into (agent / off-topic
    refusal / semantic-cache hit) — and checks PRESENCE, not truthiness: a
    field is legitimately None when nothing was graded, and that must not be
    confused with the key being absent altogether (what a `.get(...)` truthy
    check would hide). Each branch is then also checked with
    _assert_done_fields_scalar_and_match_reload — the live/reload payloads
    are the real test; presence alone can't catch a wrong VALUE."""
    with TestClient(app) as c:
        _login(c)

        # 1) a normal agent turn — has an AgentResult to fold into.
        r = _post_turn(c, "how many institutions?", answer_text="42",
                       sql_log=["SELECT 1"])
        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        done = next(e for e in events if e["type"] == "done")
        for field in chat_router.DONE_EVENT_FIELDS:
            assert field in done, f"agent-turn done event is missing {field!r}: {done}"
        conv_id = next(e["id"] for e in events if e["type"] == "conversation")
        _assert_done_fields_scalar_and_match_reload(c, conv_id, done)

        # 2) an off-topic refusal — the guard's own call IS the whole turn.
        async def _deny(question, history=None):
            return guard.Verdict(allowed=False, usage=llmhttp.Usage(prompt_tokens=1))
        orig_guard = guard.classify
        guard.classify = _deny
        try:
            r = c.post("/api/chat/stream", json={"question": "give me a recipe"})
        finally:
            guard.classify = orig_guard
        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        done = next(e for e in events if e["type"] == "done")
        assert done.get("refused") is True, done
        for field in chat_router.DONE_EVENT_FIELDS:
            assert field in done, f"refusal done event is missing {field!r}: {done}"
        conv_id = next(e["id"] for e in events if e["type"] == "conversation")
        _assert_done_fields_scalar_and_match_reload(c, conv_id, done)

        # 3) a semantic-cache hit — skips the agent entirely, still no AgentResult.
        # results_truncated=True on the cached row so the branch's ONE real
        # field (see chat.py:514-519's "cached=True still marks the hit, no
        # grounding") is exercised as truthy, not just as an absent default.
        orig_agent = chat_router.stream_agent
        orig_lookup = skills.cache_lookup
        orig_block = skills.retrieve_skills_block
        chat_router.stream_agent = _explode_agent
        skills.cache_lookup = lambda q, _uid=None: {
            "answer_md": "cached answer", "final_sql": "SELECT 1",
            "results_truncated": True}
        skills.retrieve_skills_block = lambda q: ("", [])
        try:
            r = c.post("/api/chat/stream", json={"question": "a fresh cacheable question"})
        finally:
            chat_router.stream_agent = orig_agent
            skills.cache_lookup = orig_lookup
            skills.retrieve_skills_block = orig_block
        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        done = next(e for e in events if e["type"] == "done")
        assert done.get("cached") is True, done
        for field in chat_router.DONE_EVENT_FIELDS:
            assert field in done, f"cache-hit done event is missing {field!r}: {done}"
        # THE deliberate asymmetry from chat.py:514-519, made into a test: a
        # cache hit carries NEITHER grounding mark (nothing was graded, the
        # replayed answer just doesn't say so) while still captioning a
        # truncated table -- the one field it CAN know without re-grading.
        # An untested "deliberate" asymmetry is indistinguishable from a bug
        # to the next person who "helpfully" re-grades this branch.
        assert done.get("figure_grounding") is None, done
        assert done.get("table_grounding") is None, done
        assert done.get("table_cells_checked") is None, done
        assert done.get("table_cells_matched") is None, done
        assert done.get("results_truncated"), done
        conv_id = next(e["id"] for e in events if e["type"] == "conversation")
        _assert_done_fields_scalar_and_match_reload(c, conv_id, done)


def test_message_less_turns_carry_no_done_event_fields():
    """The no-data guard and an interrupted turn (agent never yields a terminal
    `done` result) never call _persist — there's no message row to attach a
    duration/truncation/grounding mark to, so DONE_EVENT_FIELDS must be
    excluded, not merely absent by accident. Pinned explicitly, rather than
    left implied by the two message-bearing tests above, so a future change
    that starts stamping one of these fields onto a message-less `done` event
    (a dangling reference — there is no msg_id to attach it to) fails loudly
    here instead of silently rendering nothing forever."""
    with TestClient(app) as c:
        _login(c)

        # no-data guard: no ipeds.db dataset loaded.
        orig_years = chat_router.ipeds_years
        chat_router.ipeds_years = lambda: []
        try:
            r = c.post("/api/chat/stream", json={"question": "anything"})
        finally:
            chat_router.ipeds_years = orig_years
        assert r.status_code == 200, r.text
        done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
        assert done.get("no_data") is True, done
        for field in chat_router.DONE_EVENT_FIELDS:
            assert field not in done, (
                f"no-data done event unexpectedly carries {field!r}: {done}")

        # interrupted turn: the agent yields progress but never a terminal result.
        r = _post_turn_no_result(c, "a question that never finishes")
        assert r.status_code == 200, r.text
        done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
        for field in chat_router.DONE_EVENT_FIELDS:
            assert field not in done, (
                f"interrupted-turn done event unexpectedly carries {field!r}: {done}")


def test_live_and_reloaded_cell_counts_agree_for_an_ungrounded_turn():
    """THE 0-vs-null divergence CLAUDE.md flags as the same bug class as the two
    that already shipped: `_persist` NULLs table_cells_checked/matched whenever
    table_grounding is falsy (nothing was graded), but the `done` dict used to
    send AgentResult's raw int default (0) regardless of table_grounding. So an
    ordinary ungrounded turn told the LIVE viewer "0 values reproduced" while a
    reload of the exact same turn showed no mark at all — invisible unless the
    two are compared side by side, which is what this test does. The stub agent
    from _make_agent leaves table_grounding at its "" (never-checked) default
    and table_cells_checked/matched at 0, which is exactly the shape that
    trips the divergence."""
    with TestClient(app) as c:
        _login(c)
        r = _post_turn(c, "an ungrounded question", answer_text="42",
                       sql_log=["SELECT 1"])
        assert r.status_code == 200, r.text
        events = _parse_sse(r.text)
        done = next(e for e in events if e["type"] == "done")
        assert done.get("table_grounding") is None, done
        conv_id = next(e["id"] for e in events if e["type"] == "conversation")
        msgs = c.get(f"/api/chat/conversations/{conv_id}").json()
        assistant = next(m for m in msgs if m["role"] == "assistant")
        assert assistant.get("table_grounding") is None, assistant
        assert done.get("table_cells_checked") == assistant.get("table_cells_checked"), \
            ("live and reloaded table_cells_checked disagree for an ungrounded turn",
             done, assistant)
        assert done.get("table_cells_matched") == assistant.get("table_cells_matched"), \
            ("live and reloaded table_cells_matched disagree for an ungrounded turn",
             done, assistant)


def test_the_two_recent_windows_are_measured_in_different_units():
    """PINS A KNOWN, DELIBERATE DISCREPANCY so it cannot drift further.

    _load_history and _load_prior_results both LIMIT by HISTORY_TURNS, but they
    count different rows: history counts ALL messages (so 6 = ~3 conversational
    turns), while prior-results counts only rows WHERE results IS NOT NULL —
    assistant rows that ran SQL — so 6 = ~6 turns. Grounding can therefore borrow
    results from turns whose prose the model never had in context.

    This is NOT asserting the discrepancy is correct. It is asserting the actual
    measured behaviour, because the docstring claimed the windows matched
    "EXACTLY" while they were ~2x apart, and nothing caught it. Whoever
    eventually resolves it (see _load_prior_results' docstring for the decision
    and what evidence it needs) will fail this test and must update it
    deliberately — which is the whole point.
    """
    con = connect()
    conv = None
    try:
        uid = con.execute(
            "INSERT INTO users(email, created_at) VALUES "
            "('two-windows@x.edu', 0)").lastrowid
        cur = con.execute("INSERT INTO conversations(user_id, title, created_at, "
                          "updated_at) VALUES (?,'t',0,0)", (uid,))
        conv = cur.lastrowid
        # Eight full turns: each a user message + an assistant message carrying
        # one result. Long enough that BOTH windows saturate — the corpus in
        # app.db is not, which is exactly why the measurement was inconclusive.
        for n in range(1, 9):
            con.execute("INSERT INTO messages(conversation_id, role, content, "
                        "created_at) VALUES (?,?,?,?)", (conv, "user", f"q{n}", n))
            blob = json.dumps([QueryResult(columns=["v"], rows=[(n,)],
                                           row_count=1).to_storage()])
            con.execute(
                "INSERT INTO messages(conversation_id, role, content, results, "
                "created_at) VALUES (?,?,?,?,?)", (conv, "assistant", f"a{n}", blob, n))
        con.commit()

        history = chat_router._load_history(con, conv)
        results = [r.rows[0][0] for r in chat_router._load_prior_results(con, conv)]

        # History: 6 MESSAGES = the last 3 turns (q6/a6 .. q8/a8).
        assert len(history) == chat_router.HISTORY_TURNS, len(history)
        assert [m["content"] for m in history] == ["q6", "a6", "q7", "a7", "q8", "a8"], \
            [m["content"] for m in history]

        # Prior results: 6 RESULT-BEARING rows = the last 6 turns.
        assert results == [3, 4, 5, 6, 7, 8], results

        # The measurable consequence: turns 3-5 contribute grounding evidence
        # whose prose the model never saw. THIS is the asymmetry.
        prose_turns = {int(m["content"][1:]) for m in history}
        assert set(results) - prose_turns == {3, 4, 5}, set(results) - prose_turns
    finally:
        if conv is not None:
            con.execute("DELETE FROM messages WHERE conversation_id=?", (conv,))
            con.execute("DELETE FROM conversations WHERE id=?", (conv,))
        con.execute("DELETE FROM users WHERE email='two-windows@x.edu'")
        con.commit()
        con.close()


def run():
    print("chat router contract:")
    check("empty question is rejected (400)", test_empty_question_rejected)
    check("an over-long question is rejected before anything is written",
          test_oversized_question_rejected_before_anything_is_written)
    check("streaming into an unknown conversation_id 404s",
          test_stream_unknown_conversation_id_404)
    check("streaming into another user's conversation 404s",
          test_stream_conversation_not_owned_by_caller_404)
    check("edit_message_id replaces the old exchange in place",
          test_edit_message_id_replaces_old_exchange)
    check("an interrupted first turn leaves no phantom conversation",
          test_interrupted_new_turn_leaves_no_phantom_conversation)
    check("an interrupted edit turn keeps the old exchange intact",
          test_interrupted_edit_turn_keeps_the_old_exchange_intact)
    check("a semantic cache hit serves the cached answer + titles the chat",
          test_cache_hit_serves_cached_answer_and_titles_new_conversation)
    check("turn duration is measured, persisted, and in the done event",
          test_turn_duration_is_measured_persisted_and_in_the_done_event)
    check("guard spend is billed on every turn shape (agent/refusal/cache hit)",
          test_guard_spend_is_billed_on_every_turn_shape)
    check("guard tokens never pollute the schema-cache columns",
          test_guard_tokens_never_pollute_the_schema_cache_columns)
    check("_add_usage accumulates onto a committed row, MAXing cost_estimated",
          test_add_usage_accumulates_onto_a_committed_row)
    check("exhaustion status is persisted to usage_log",
          test_exhaustion_status_is_persisted_to_usage_log)
    check("a normal (non-cached) successful turn titles a new conversation",
          test_normal_flow_titles_a_new_conversation)
    check("the thinking trace is persisted and returned on reload",
          test_thinking_trace_is_persisted_and_returned_on_reload)
    check("_extract_figure parses + strips the figure fence",
          test_extract_figure_parses_and_strips_the_fence)
    check("the figure is persisted and returned on reload",
          test_figure_is_persisted_and_returned_on_reload)
    check("table grounding is persisted and carried on the done event",
          test_table_grounding_is_persisted_and_in_the_done_event)
    check("_extract_suggestions parses + strips the followups fence",
          test_extract_suggestions_parses_and_strips_the_fence)
    check("suggestions are persisted and returned on reload",
          test_suggestions_are_persisted_and_returned_on_reload)
    check("a clarify turn persists, is never cached, and records no lesson",
          test_clarify_turn_persists_is_never_cached_and_records_no_lesson)
    check("retrieved few-shot skills bump their hit count",
          test_retrieved_skills_bump_their_hit_count)
    check("a critic-driven correction records a candidate lesson",
          test_critic_revision_records_a_lesson)
    check("feedback distiller finds a rule → records a user-feedback lesson",
          test_record_feedback_lesson_records_when_distiller_finds_a_rule)
    check("feedback distiller finds nothing → records no lesson",
          test_record_feedback_lesson_noops_when_distiller_finds_nothing)
    check("feedback distiller error is swallowed (never breaks the turn)",
          test_record_feedback_lesson_swallows_distiller_errors)
    check("_fire_and_forget runs the coroutine then self-discards the task",
          test_fire_and_forget_runs_then_self_discards)
    check("a follow-up critic correction does NOT record a lesson",
          test_critic_lesson_not_recorded_on_followup_turn)
    check("UNGROUNDED_NUMBER still revises but records no lesson",
          test_ungrounded_number_category_revises_but_records_no_lesson)
    check("critic_category reaches the lesson gate through the real agent loop",
          test_critic_category_reaches_the_lesson_gate_through_the_real_agent_loop)
    check("conversation list/get/delete (+404s)", test_conversation_crud)
    check("conversation rename: trims, keeps sidebar order, rejects blank/overlong",
          test_conversation_rename)
    check("conversation rename by a non-owner 404s and changes nothing",
          test_conversation_rename_not_owned_404)
    check("no-data guard: admin sees Admin->Imports wording and stream_agent never runs",
          test_no_data_guard_admin_wording_and_skips_agent)
    check("no-data guard: non-admin sees wait-for-administrator wording, agent never runs",
          test_no_data_guard_non_admin_wording_and_skips_agent)
    check("CSV download of an unknown message 404s",
          test_download_csv_unknown_message_404)
    check("CSV download with no associated query 400s",
          test_download_csv_no_sql_log_400)
    check("CSV download of a rejected/forbidden SQL 400s",
          test_download_csv_rejected_sql_400)
    check("CSV download picks the table query, not a trailing COUNT(*)",
          test_download_csv_picks_table_query_not_a_trailing_count)
    check("CSV download prefers the LAST column-count match",
          test_download_csv_prefers_the_last_matching_query)
    check("GET /api/version is authed and returns current/latest/update_available",
          test_version_endpoint_authed_and_shape)
    check("_results_for_storage caps and drops largest over budget",
          test_results_for_storage_caps_and_drops_largest_over_budget)
    check("_results_for_storage caps a LONE oversized result too",
          test_results_for_storage_caps_a_lone_oversized_result)
    check("_results_for_storage flags a blob it halved (flag set before halving)",
          test_results_for_storage_flags_a_blob_it_halved)
    check("CSV probes are time-bounded; the winning re-run is not",
          test_csv_probes_are_time_bounded_but_the_winning_rerun_is_not)
    check("a cache hit keeps the conversation grounding chain intact",
          test_cache_hit_keeps_the_conversation_grounding_chain_intact)
    check("_load_prior_results respects the before_id window",
          test_load_prior_results_respects_before_id_window)
    check("the two recent windows are measured in different units (known)",
          test_the_two_recent_windows_are_measured_in_different_units)
    check("CSV export survives a failed query in the answer's log",
          test_csv_export_survives_a_failed_query_in_the_answers_log)
    check("every persisted turn field reaches the reader and the done event",
          test_every_persisted_turn_field_reaches_the_reader_and_the_done_event)
    check("the done event carries every DONE_EVENT_FIELDS key on all three "
          "message-bearing paths (agent/refusal/cache-hit)",
          test_done_event_carries_every_field_on_all_three_message_bearing_paths)
    check("message-less turns (no-data guard, interrupted) carry no "
          "DONE_EVENT_FIELDS at all",
          test_message_less_turns_carry_no_done_event_fields)
    check("live and reloaded cell counts agree for an ungrounded turn "
          "(0 vs NULL divergence)",
          test_live_and_reloaded_cell_counts_agree_for_an_ungrounded_turn)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL CHAT-ROUTER TESTS PASSED")


if __name__ == "__main__":
    run()
