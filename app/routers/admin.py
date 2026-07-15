"""Admin API: allowlist, access requests, data imports, usage, skills."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, EmailStr

from app import importer
from app.auth import require_admin
from app.config import get_settings
from app.db import connect

router = APIRouter(prefix="/api/admin", tags=["admin"],
                   dependencies=[Depends(require_admin)])


# --- Allowlist ----------------------------------------------------------------
class AllowlistAdd(BaseModel):
    email: EmailStr
    note: str | None = None
    is_admin: bool = False


@router.get("/allowlist")
def list_allowlist():
    con = connect()
    try:
        rows = con.execute(
            "SELECT a.email, a.note, a.added_by, a.added_at, "
            "COALESCE(u.is_admin,0) AS is_admin, u.last_login "
            "FROM allowlist a LEFT JOIN users u ON u.email=a.email "
            "ORDER BY a.added_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


@router.post("/allowlist")
def add_allowlist(body: AllowlistAdd, admin: sqlite3.Row = Depends(require_admin)):
    email = str(body.email).lower()
    con = connect()
    try:
        con.execute("INSERT INTO allowlist(email, note, added_by, added_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(email) DO UPDATE SET note=excluded.note",
                    (email, body.note, admin["email"], time.time()))
        if body.is_admin:
            con.execute("INSERT INTO users(email, is_admin, created_at) VALUES (?,1,?) "
                        "ON CONFLICT(email) DO UPDATE SET is_admin=1", (email, time.time()))
        con.execute("UPDATE access_requests SET status='approved' "
                    "WHERE email=? AND status='pending'", (email,))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "email": email}


@router.delete("/allowlist/{email}")
def remove_allowlist(email: str):
    email = email.lower()
    con = connect()
    try:
        con.execute("DELETE FROM allowlist WHERE email=?", (email,))
        con.execute("UPDATE users SET is_admin=0 WHERE email=?", (email,))
        con.execute(
            "DELETE FROM sessions WHERE user_id IN "
            "(SELECT id FROM users WHERE email=?)", (email,))
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.get("/access-requests")
def access_requests():
    con = connect()
    try:
        rows = con.execute(
            "SELECT id, email, reason, status, created_at FROM access_requests "
            "WHERE status='pending' ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# --- Data import --------------------------------------------------------------
# Only one import may run at a time: concurrent rebuilds would race the same
# ipeds_staging.db / atomic swap. Held from upload through run_import.
_import_lock = threading.Lock()


@router.post("/import")
async def start_import(background: BackgroundTasks, file: UploadFile = File(...),
                       admin: sqlite3.Row = Depends(require_admin)):
    if not file.filename or not file.filename.lower().endswith(".accdb"):
        raise HTTPException(400, "Please upload an IPEDS .accdb file.")
    if not _import_lock.acquire(blocking=False):
        raise HTTPException(409, "An import is already running. Wait for it to finish.")

    s = get_settings()
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    dest = s.upload_dir / Path(file.filename).name
    max_bytes = s.max_upload_mb * 1024 * 1024
    try:
        written = 0
        with dest.open("wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        413, f"Upload exceeds the {s.max_upload_mb} MB limit.")
                f.write(chunk)
        job_id = importer.create_job(file.filename, admin["email"])
    except Exception:
        dest.unlink(missing_ok=True)
        _import_lock.release()
        raise

    def _run():
        try:
            importer.run_import(job_id, dest)
        finally:
            _import_lock.release()

    # Run the (long) rebuild off the event loop.
    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "status": "pending"}


@router.get("/import/jobs")
def import_jobs():
    con = connect()
    try:
        rows = con.execute(
            "SELECT id, filename, status, created_by, created_at, updated_at "
            "FROM import_jobs ORDER BY id DESC LIMIT 50").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


@router.get("/import/jobs/{job_id}")
def import_job(job_id: int):
    con = connect()
    try:
        row = con.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Job not found.")
        return dict(row)
    finally:
        con.close()


# --- Usage dashboard ----------------------------------------------------------
@router.get("/usage")
def usage(since: float | None = None, until: float | None = None):
    """Usage/spend over a time window [since, until] (unix seconds; default: the
    last 7 days). Returns totals, a time-bucketed series (hourly for short
    windows, else daily) for charting, top users, and recent activity."""
    now = time.time()
    until = float(until) if until else now
    since = float(since) if since else (now - 7 * 86400)
    if since > until:
        since, until = until, since
    hourly = (until - since) <= 2 * 86400 + 1
    bucket_fmt = "%Y-%m-%d %H:00" if hourly else "%Y-%m-%d"
    win = "WHERE created_at BETWEEN ? AND ?"
    args = (since, until)

    con = connect()
    try:
        totals = con.execute(
            "SELECT COUNT(*) AS queries, "
            "COALESCE(SUM(prompt_tokens+completion_tokens),0) AS tokens, "
            "COALESCE(SUM(cost),0.0) AS spend, "
            "COALESCE(SUM(cached),0) AS cache_hits, "
            "COALESCE(SUM(escalated),0) AS escalations, "
            "COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) AS failures "
            f"FROM usage_log {win}", args).fetchone()
        series = con.execute(
            f"SELECT strftime('{bucket_fmt}', created_at,'unixepoch') AS t, "
            "COUNT(*) AS queries, "
            "COALESCE(SUM(prompt_tokens+completion_tokens),0) AS tokens, "
            "COALESCE(SUM(cost),0.0) AS spend "
            f"FROM usage_log {win} GROUP BY t ORDER BY t", args).fetchall()
        top_users = con.execute(
            "SELECT u.email, COUNT(*) AS queries, "
            "COALESCE(SUM(l.prompt_tokens+l.completion_tokens),0) AS tokens, "
            "COALESCE(SUM(l.cost),0.0) AS spend FROM usage_log l "
            "JOIN users u ON u.id=l.user_id WHERE l.created_at BETWEEN ? AND ? "
            "GROUP BY u.email ORDER BY queries DESC LIMIT 10", args).fetchall()
        recent = con.execute(
            "SELECT question, model_used, ok, cached, cost, created_at "
            f"FROM usage_log {win} ORDER BY id DESC LIMIT 20", args).fetchall()
        return {"since": since, "until": until, "bucket": "hour" if hourly else "day",
                "totals": dict(totals),
                "series": [dict(r) for r in series],
                "top_users": [dict(r) for r in top_users],
                "recent": [dict(r) for r in recent]}
    finally:
        con.close()


# --- Skills review ------------------------------------------------------------
@router.get("/skills")
def list_skills():
    con = connect()
    try:
        rows = con.execute(
            "SELECT id, question, canonical_sql, notes, upvotes, downvotes, hits, "
            "verified, created_by, created_at FROM skills ORDER BY verified DESC, "
            "hits DESC, id DESC LIMIT 500").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


class SkillUpdate(BaseModel):
    verified: bool | None = None
    notes: str | None = None
    canonical_sql: str | None = None


@router.patch("/skills/{skill_id}")
def update_skill(skill_id: int, body: SkillUpdate):
    sets, vals = [], []
    if body.verified is not None:
        sets.append("verified=?"); vals.append(int(body.verified))
    if body.notes is not None:
        sets.append("notes=?"); vals.append(body.notes)
    if body.canonical_sql is not None:
        sets.append("canonical_sql=?"); vals.append(body.canonical_sql)
    if not sets:
        return {"ok": True}
    vals.append(skill_id)
    con = connect()
    try:
        con.execute(f"UPDATE skills SET {', '.join(sets)} WHERE id=?", vals)
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.delete("/skills/{skill_id}")
def delete_skill(skill_id: int):
    con = connect()
    try:
        con.execute("DELETE FROM skills WHERE id=?", (skill_id,))
        con.commit()
    finally:
        con.close()
    return {"ok": True}


# --- Server logs --------------------------------------------------------------
@router.get("/logs")
def server_logs(limit: int = 200, level: str | None = None):
    """Recent in-memory server log records (newest last). Optionally filter by
    level (INFO/WARNING/ERROR)."""
    from app.logbuffer import get_handler
    handler = get_handler()
    if handler is None:
        return {"records": []}
    limit = max(1, min(limit, 500))
    return {"records": handler.records(limit=limit, level=level)}
