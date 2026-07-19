"""Chat API: streaming NL→answer, conversation history, CSV export."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app import guard, skills
from app.auth import current_user
from app.db import connect
from app.llm import generate_title, stream_agent
from app.tools.sql import SQLValidationError, ipeds_years, run_sql

router = APIRouter(prefix="/api/chat", tags=["chat"])

HISTORY_TURNS = 6  # prior messages fed back to the model for context

# Fresh-deploy "no data" guard wording (module-level constants for testability
# -- see chat_stream). Admin wording routes to Admin -> Imports; non-admin
# wording just asks them to wait, and must never mention the admin-only UI.
NO_DATA_ADMIN = (
    "No IPEDS dataset is loaded yet. Open Admin → Imports to fetch a year "
    "from NCES (or upload an .accdb). Once a year is integrated, you can ask "
    "data questions here."
)
NO_DATA_USER = (
    "No IPEDS dataset is loaded yet. An administrator needs to load data "
    "before questions can be answered — please check back soon."
)


class ChatRequest(BaseModel):
    question: str
    conversation_id: int | None = None
    # When re-asking an edited/rerun prompt: drop this message and everything
    # after it first, so the new turn REPLACES the old exchange in place.
    edit_message_id: int | None = None


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _trace_item(ev: dict) -> dict | None:
    """Map a stream event to a persisted "Thinking" trace item, mirroring the
    frontend's live addThought() 1:1 so a reloaded trace renders identically to
    the in-session one (Chat.jsx ThinkingTrace). Non-trace events -> None."""
    t = ev.get("type")
    if t == "status":
        return {"kind": "status", "text": ev.get("text", "")}
    if t == "sql":
        return {"kind": "sql", "text": ev.get("sql", "")}
    if t == "thinking":
        return {"kind": "reason", "text": ev.get("text", "")}
    if t == "tool":
        return {"kind": "tool", "text": f"{ev.get('name', '')}{' ✓' if ev.get('ok') else ' ✗'}"}
    return None


def _load_history(con: sqlite3.Connection, conv_id: int) -> list[dict]:
    rows = con.execute(
        "SELECT role, content FROM messages WHERE conversation_id=? "
        "ORDER BY id DESC LIMIT ?", (conv_id, HISTORY_TURNS)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


@router.post("/stream")
async def chat_stream(req: ChatRequest, user: sqlite3.Row = Depends(current_user)):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "Empty question.")

    # Fresh-deploy "no data" guard: before touching app.db or the agent at
    # all, bail out with a friendly notice if there's no ipeds.db dataset
    # loaded yet. Creates no conversation, persists nothing, runs no agent.
    if not ipeds_years():
        msg = NO_DATA_ADMIN if bool(user["is_admin"]) else NO_DATA_USER

        async def _no_data_gen():
            yield _sse({"type": "answer", "text": msg})
            yield _sse({"type": "done", "no_data": True})

        return StreamingResponse(_no_data_gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    con = connect()
    is_new = not req.conversation_id
    try:
        conv_id = req.conversation_id
        if conv_id:
            owns = con.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?",
                               (conv_id, user["id"])).fetchone()
            if not owns:
                raise HTTPException(404, "Conversation not found.")
            # Editing/rerunning: delete the target message and everything after
            # it so the incoming turn replaces the old exchange.
            if req.edit_message_id:
                con.execute(
                    "DELETE FROM messages WHERE conversation_id=? AND id>=?",
                    (conv_id, req.edit_message_id))
                con.commit()
        else:
            cur = con.execute(
                "INSERT INTO conversations(user_id, title, created_at, updated_at) "
                "VALUES (?,?,?,?)", (user["id"], question[:80], time.time(), time.time()))
            conv_id = cur.lastrowid
            con.commit()
        history = _load_history(con, conv_id)
    finally:
        con.close()

    async def gen():
        yield _sse({"type": "conversation", "id": conv_id})

        # 0) Topical guardrail: refuse anything that isn't a good-faith IPEDS
        # question (off-topic requests, prompt-injection) BEFORE any cache or
        # model/tool work, so an adversarial message never drives the agent.
        verdict = await guard.classify(question, history)
        if not verdict.allowed:
            answer = guard.REFUSAL
            yield _sse({"type": "answer", "text": answer})
            user_msg_id, msg_id = await run_in_threadpool(
                _persist, user["id"], conv_id, question, answer,
                sql_log=[], model="guard", tokens=verdict.tokens,
                cached=False, ok=True)
            yield _sse({"type": "done", "refused": True, "message_id": msg_id,
                        "user_message_id": user_msg_id})
            return

        # 1) Semantic cache: reuse SQL for a near-identical past question.
        # Only a valid shortcut for a fresh, first-turn question — a follow-up
        # inside an existing conversation depends on prior context, so it must
        # never be served a cached answer from a different conversation.
        cached = await run_in_threadpool(skills.cache_lookup, question) if not history else None
        if cached:
            answer = cached["answer_md"]
            status = "Matched a recent question — reusing its query."
            yield _sse({"type": "status", "text": status})
            yield _sse({"type": "answer", "text": answer})
            user_msg_id, msg_id = await run_in_threadpool(
                _persist, user["id"], conv_id, question, answer,
                sql_log=[cached["final_sql"]] if cached["final_sql"] else [],
                model="cache", tokens=0, cached=True, ok=True,
                thinking=[{"kind": "status", "text": status}])
            done = {"type": "done", "cached": True, "message_id": msg_id,
                    "user_message_id": user_msg_id}
            if is_new and answer:
                title = await generate_title(question, answer)
                if title:
                    await run_in_threadpool(_update_title, conv_id, title)
                    done["title"] = title
            yield _sse(done)
            return

        # 2) Retrieve learned skills as few-shot context.
        skills_block, skill_ids = await run_in_threadpool(skills.retrieve_skills_block, question)
        if skill_ids:
            await run_in_threadpool(skills.bump_hits, skill_ids)

        # 3) Run the agent, streaming progress. Accumulate the same trace the
        # frontend builds live, so it can be persisted and the "Thinking"
        # disclosure survives a reload (not just the in-session turn).
        result = None
        answer = ""
        thinking: list[dict] = []
        async for ev in stream_agent(question, history=history, skills_block=skills_block):
            if ev["type"] == "done":
                result = ev["result"]
                continue
            if ev["type"] == "answer":
                answer = ev["text"]
            item = _trace_item(ev)
            if item:
                thinking.append(item)
            yield _sse(ev)

        if result is None:
            yield _sse({"type": "done"})
            return

        user_msg_id, msg_id = await run_in_threadpool(
            _persist, user["id"], conv_id, question, answer or (result.error or ""),
            sql_log=result.sql_log, model=result.model_used,
            tokens=result.total_tokens, cached=False,
            ok=result.error is None, escalated=result.escalated,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost=result.cost, thinking=thinking)

        # 4) Cache the successful answer for reuse (first-turn, context-free only).
        if not history and result.error is None and answer and result.sql_log:
            await run_in_threadpool(skills.cache_store, question, result.sql_log[-1], answer)

        # 4b) If the critic caught a real mistake and forced a correction, capture
        # its finding as an unverified lesson (self-learning from actual errors).
        # First-turn only (like the cache above): a follow-up's bare question
        # ("and for Ohio?") is a context-less, useless retrieval key.
        if (not history and result.critic_revised
                and (result.critic_headline or result.critic_description)
                and result.error is None and result.sql_log):
            await run_in_threadpool(
                skills.record_lesson_from_critic, question,
                result.sql_log[-1], result.critic_headline, result.critic_description)

        done = {"type": "done", "escalated": result.escalated,
                "model": result.model_used, "tokens": result.total_tokens,
                "message_id": msg_id, "user_message_id": user_msg_id}
        # 5) Let the model name a brand-new conversation (better than the raw query).
        if is_new and result.error is None and answer:
            title = await generate_title(question, answer)
            if title:
                await run_in_threadpool(_update_title, conv_id, title)
                done["title"] = title
        yield _sse(done)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _persist(user_id, conv_id, question, answer, *, sql_log, model, tokens,
             cached, ok, escalated=False, prompt_tokens=0, completion_tokens=0,
             cost=0.0, thinking=None):
    """Persist the user + assistant messages and usage row. Returns the new
    assistant message id (so the stream can hand it to the client without a
    full conversation reload)."""
    con = connect()
    try:
        now = time.time()
        ucur = con.execute(
            "INSERT INTO messages(conversation_id, role, content, created_at) "
            "VALUES (?,?,?,?)", (conv_id, "user", question, now))
        user_msg_id = ucur.lastrowid
        cur = con.execute(
            "INSERT INTO messages(conversation_id, role, content, sql_log, "
            "thinking, model_used, tokens, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (conv_id, "assistant", answer, json.dumps(sql_log),
             json.dumps(thinking or []), model, tokens, now))
        assistant_id = cur.lastrowid
        con.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
        con.execute(
            "INSERT INTO usage_log(user_id, question, model_used, escalated, "
            "prompt_tokens, completion_tokens, ok, cached, cost, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user_id, question, model, int(escalated), prompt_tokens,
             completion_tokens, int(ok), int(cached), float(cost), now))
        con.commit()
        return user_msg_id, assistant_id
    finally:
        con.close()


def _update_title(conv_id: int, title: str) -> None:
    con = connect()
    try:
        con.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
        con.commit()
    finally:
        con.close()


@router.get("/conversations")
def list_conversations(user: sqlite3.Row = Depends(current_user)):
    con = connect()
    try:
        rows = con.execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "WHERE user_id=? ORDER BY updated_at DESC LIMIT 100", (user["id"],)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


@router.get("/conversations/{conv_id}")
def get_conversation(conv_id: int, user: sqlite3.Row = Depends(current_user)):
    con = connect()
    try:
        owns = con.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?",
                           (conv_id, user["id"])).fetchone()
        if not owns:
            raise HTTPException(404, "Not found.")
        rows = con.execute(
            "SELECT id, role, content, sql_log, thinking, model_used, created_at "
            "FROM messages WHERE conversation_id=? ORDER BY id", (conv_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


class RenameRequest(BaseModel):
    title: str


# The UI truncates sidebar titles anyway; anything longer than this is
# noise (and an unbounded write). Mirrored client-side by the rename input's
# maxLength — keep the two in sync.
MAX_TITLE_LEN = 200


@router.patch("/conversations/{conv_id}")
def rename_conversation(conv_id: int, body: RenameRequest,
                        user: sqlite3.Row = Depends(current_user)):
    """Rename a conversation the caller owns.

    Metadata-only by contract: deliberately does NOT touch updated_at, so
    renaming an old chat never jumps it to the top of the recency-ordered
    sidebar (list_conversations orders by updated_at DESC)."""
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "Title can't be empty.")
    if len(title) > MAX_TITLE_LEN:
        raise HTTPException(400, f"Title is too long (max {MAX_TITLE_LEN} characters).")
    con = connect()
    try:
        owns = con.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?",
                           (conv_id, user["id"])).fetchone()
        if not owns:
            raise HTTPException(404, "Not found.")
        con.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "title": title}


@router.delete("/conversations/{conv_id}")
def delete_conversation(conv_id: int, user: sqlite3.Row = Depends(current_user)):
    con = connect()
    try:
        owns = con.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?",
                           (conv_id, user["id"])).fetchone()
        if not owns:
            raise HTTPException(404, "Not found.")
        con.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
        con.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.get("/messages/{message_id}/download.csv")
def download_csv(message_id: int, request: Request, user: sqlite3.Row = Depends(current_user)):
    """Re-execute the answer's final SQL (higher row cap) and stream a CSV.

    Re-running is intentional: it guarantees the download reflects current data
    and avoids relying on any per-request in-memory result.
    """
    from app.config import get_settings
    con = connect()
    try:
        row = con.execute(
            "SELECT m.sql_log FROM messages m JOIN conversations c "
            "ON c.id=m.conversation_id WHERE m.id=? AND c.user_id=?",
            (message_id, user["id"])).fetchone()
    finally:
        con.close()
    if not row:
        raise HTTPException(404, "Message not found.")
    sql_list = json.loads(row["sql_log"] or "[]")
    if not sql_list:
        raise HTTPException(400, "No query is associated with this answer.")
    try:
        result = run_sql(sql_list[-1], limit=get_settings().sql_row_cap_download)
    except SQLValidationError as e:
        raise HTTPException(400, str(e)) from e

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(result.columns)
    w.writerows(result.rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="ipeds_result_{message_id}.csv"'})
