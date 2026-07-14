"""Admin API: allowlist, access requests, data imports, usage, skills."""
from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import (APIRouter, BackgroundTasks, Depends, File, HTTPException,
                     UploadFile)
from pydantic import BaseModel, EmailStr

from app.auth import require_admin
from app.config import get_settings
from app.db import connect
from app import importer

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
    con = connect()
    try:
        con.execute("DELETE FROM allowlist WHERE email=?", (email.lower(),))
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
@router.post("/import")
async def start_import(background: BackgroundTasks, file: UploadFile = File(...),
                       admin: sqlite3.Row = Depends(require_admin)):
    if not file.filename or not file.filename.lower().endswith(".accdb"):
        raise HTTPException(400, "Please upload an IPEDS .accdb file.")
    s = get_settings()
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    dest = s.upload_dir / Path(file.filename).name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    job_id = importer.create_job(file.filename, admin["email"])
    # Run the (long) rebuild off the event loop.
    threading.Thread(target=importer.run_import, args=(job_id, dest), daemon=True).start()
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
def usage():
    con = connect()
    try:
        totals = con.execute(
            "SELECT COUNT(*) AS queries, "
            "COALESCE(SUM(prompt_tokens+completion_tokens),0) AS tokens, "
            "COALESCE(SUM(cached),0) AS cache_hits, "
            "COALESCE(SUM(escalated),0) AS escalations, "
            "COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) AS failures "
            "FROM usage_log").fetchone()
        by_day = con.execute(
            "SELECT date(created_at,'unixepoch') AS day, COUNT(*) AS queries, "
            "COALESCE(SUM(prompt_tokens+completion_tokens),0) AS tokens "
            "FROM usage_log GROUP BY day ORDER BY day DESC LIMIT 30").fetchall()
        top_users = con.execute(
            "SELECT u.email, COUNT(*) AS queries FROM usage_log l "
            "JOIN users u ON u.id=l.user_id GROUP BY u.email "
            "ORDER BY queries DESC LIMIT 10").fetchall()
        recent = con.execute(
            "SELECT question, model_used, ok, cached, created_at FROM usage_log "
            "ORDER BY id DESC LIMIT 20").fetchall()
        return {"totals": dict(totals),
                "by_day": [dict(r) for r in by_day],
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
