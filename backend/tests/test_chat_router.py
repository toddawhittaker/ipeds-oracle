"""Chat router contract (backend/app/routers/chat.py): streaming turns, the semantic
cache-hit shortcut, edit/rerun (replacing an old exchange in place),
conversation list/get/delete, critic-driven lesson recording, the
fresh-deploy no-data guard (admin/non-admin wording, no agent run, no
conversation created), and the CSV export's error branches.

The 👍/👎 feedback feature (and its `promote_from_message` lesson path) was
removed — the critic is now the sole lesson source. `POST
/messages/{id}/feedback` must 404 (route gone), and `get_conversation` no
longer selects a `feedback` column.

No LLM/API key needed: guard.classify is patched to always allow, and
chat_router.stream_agent is replaced per-test with a canned async generator
(same pattern as backend/tests/test_guard.py) so every branch runs deterministically.
"""
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

from fastapi.testclient import TestClient  # noqa: E402

from app import mailer  # noqa: E402

captured = {}
mailer.send_magic_link = lambda to, link: captured.__setitem__("link", link) or True
mailer.send_access_request = lambda *a, **k: True
mailer.send_access_approved = (
    lambda to, link: captured.__setitem__("approved_link", link) or True)

from app import guard, skills  # noqa: E402
from app.llm import AgentResult  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import chat as chat_router  # noqa: E402

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
    async def _agent(question, *, history=None, skills_block=""):
        if answer_text is not None:
            yield {"type": "answer", "text": answer_text}
        yield {"type": "done", "result": AgentResult(
            answer=answer_text or "", model_used=model, error=error,
            sql_log=sql_log or [], prompt_tokens=3, completion_tokens=2)}
    return _agent


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
        approved_link = captured["approved_link"]
        atok = approved_link.split("token=")[1]
        c2 = TestClient(app)
        assert c2.post("/api/auth/verify", json={"token": atok}).status_code == 200
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

        async def _critic_agent(question, *, history=None, skills_block=""):
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

        async def _critic_agent(question, *, history=None, skills_block=""):
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
        atok = captured["approved_link"].split("token=")[1]
        c2 = TestClient(app)
        assert c2.post("/api/auth/verify", json={"token": atok}).status_code == 200
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
        token = captured["approved_link"].split("token=")[1]
        c2 = TestClient(app)
        assert c2.post("/api/auth/verify", json={"token": token}).status_code == 200

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


def run():
    print("chat router contract:")
    check("empty question is rejected (400)", test_empty_question_rejected)
    check("streaming into an unknown conversation_id 404s",
          test_stream_unknown_conversation_id_404)
    check("streaming into another user's conversation 404s",
          test_stream_conversation_not_owned_by_caller_404)
    check("edit_message_id replaces the old exchange in place",
          test_edit_message_id_replaces_old_exchange)
    check("a semantic cache hit serves the cached answer + titles the chat",
          test_cache_hit_serves_cached_answer_and_titles_new_conversation)
    check("a normal (non-cached) successful turn titles a new conversation",
          test_normal_flow_titles_a_new_conversation)
    check("retrieved few-shot skills bump their hit count",
          test_retrieved_skills_bump_their_hit_count)
    check("a critic-driven correction records a candidate lesson",
          test_critic_revision_records_a_lesson)
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
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL CHAT-ROUTER TESTS PASSED")


if __name__ == "__main__":
    run()
