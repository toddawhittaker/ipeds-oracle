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

from app import guard, skills  # noqa: E402
from app.db import connect  # noqa: E402
from app.llm import AgentResult  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import chat as chat_router  # noqa: E402
from app.tools.sql import QueryResult  # noqa: E402

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


async def _always_allow(question, history=None):
    return guard.Verdict(allowed=True, tokens=1)


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
    skills.cache_lookup = lambda q: None
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
    skills.cache_lookup = lambda q: None
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
            return "A Cached Title"

        chat_router.stream_agent = _explode
        skills.cache_lookup = lambda q: {
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
        skills.cache_lookup = lambda q: None
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
            return "A Real-Flow Title"

        chat_router.stream_agent = _make_agent("a real answer", sql_log=["SELECT 1"])
        chat_router.generate_title = _fake_title
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q: None
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
        skills.cache_lookup = lambda q: None
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
        skills.cache_lookup = lambda q: None
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
        skills.cache_lookup = lambda q: None
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
        skills.cache_lookup = lambda q: None
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
        skills.cache_lookup = lambda q: None
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
    with TestClient(app) as c:
        _login(c)
        captured = {}

        async def _critic_agent(question, *, history=None, skills_block="", prior_results=None):
            yield {"type": "answer", "text": "corrected answer"}
            yield {"type": "done", "result": AgentResult(
                answer="corrected answer", model_used="test-model", error=None,
                sql_log=["SELECT SUM(x) FROM c_a"], critic_revised=True,
                critic_headline="Add majornum=1.",
                critic_description="no majornum=1 filter; double count")}

        orig_agent = chat_router.stream_agent
        orig_block = skills.retrieve_skills_block
        orig_cache_lookup = skills.cache_lookup
        orig_cache_store = skills.cache_store
        orig_record = skills.record_lesson_from_critic
        chat_router.stream_agent = _critic_agent
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q: None
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
                critic_headline="A headline.", critic_description="a rule")}

        orig_agent = chat_router.stream_agent
        orig_block = skills.retrieve_skills_block
        orig_cache_lookup = skills.cache_lookup
        orig_cache_store = skills.cache_store
        orig_record = skills.record_lesson_from_critic
        chat_router.stream_agent = _critic_agent
        skills.retrieve_skills_block = lambda q: ("", [])
        skills.cache_lookup = lambda q: None
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
        return ("Keep the established scope.", "inherit the award-level scope on follow-ups")

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
        return None

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
        con.close()


def run():
    print("chat router contract:")
    check("empty question is rejected (400)", test_empty_question_rejected)
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
    check("_results_for_storage caps and drops largest over budget",
          test_results_for_storage_caps_and_drops_largest_over_budget)
    check("_load_prior_results respects the before_id window",
          test_load_prior_results_respects_before_id_window)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL CHAT-ROUTER TESTS PASSED")


if __name__ == "__main__":
    run()
