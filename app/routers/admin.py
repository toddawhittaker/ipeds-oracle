"""Admin API: allowlist, access requests, data imports, usage, skills."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, EmailStr

import app.nces as nces
from app import importer
from app.auth import mint_login_link, require_admin
from app.config import get_settings
from app.db import connect
from app.mailer import send_access_approved

log = logging.getLogger("ipeds.admin")

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
    email = str(body.email).strip().lower()
    con = connect()
    invite_link = None
    try:
        newly_added = con.execute("SELECT 1 FROM allowlist WHERE email=?",
                                  (email,)).fetchone() is None
        con.execute("INSERT INTO allowlist(email, note, added_by, added_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(email) DO UPDATE SET note=excluded.note",
                    (email, body.note, admin["email"], time.time()))
        if body.is_admin:
            con.execute("INSERT INTO users(email, is_admin, created_at) VALUES (?,1,?) "
                        "ON CONFLICT(email) DO UPDATE SET is_admin=1", (email, time.time()))
        con.execute("UPDATE access_requests SET status='approved' "
                    "WHERE email=? AND status='pending'", (email,))
        # Newly approved/added people get a ready-to-use sign-in link so they can
        # get in without having to know to request one again.
        if newly_added:
            invite_link = mint_login_link(con, email, get_settings().app_public_url)
        con.commit()
    finally:
        con.close()
    invited = False
    if invite_link:
        try:
            invited = send_access_approved(email, invite_link)
        except Exception as e:  # noqa: BLE001 — approval must not fail if email does
            log.warning("approval email to %s failed: %s", email, e)
    return {"ok": True, "email": email, "invited": invited}


class AllowlistAdminPatch(BaseModel):
    is_admin: bool


@router.patch("/allowlist/{email}")
def set_allowlist_admin(email: str, body: AllowlistAdminPatch,
                        admin: sqlite3.Row = Depends(require_admin)):
    """Promote or demote an allowlisted user to/from admin. is_admin is read
    live on every request, so the change takes effect immediately (no re-login).

    You can't demote YOURSELF (ask another admin) — this both stops an accidental
    self-lockout and, since the caller is always an admin, guarantees at least one
    admin always remains, so the console can never be left with zero admins."""
    email = email.strip().lower()
    if not body.is_admin and email == admin["email"].strip().lower():
        raise HTTPException(
            400, "You can't remove your own admin access — ask another admin.")
    con = connect()
    try:
        if con.execute("SELECT 1 FROM allowlist WHERE email=?",
                       (email,)).fetchone() is None:
            raise HTTPException(404, "That email is not on the allowlist.")
        if body.is_admin:
            con.execute(
                "INSERT INTO users(email, is_admin, created_at) VALUES (?,1,?) "
                "ON CONFLICT(email) DO UPDATE SET is_admin=1", (email, time.time()))
        else:
            con.execute("UPDATE users SET is_admin=0 WHERE email=?", (email,))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "email": email, "is_admin": body.is_admin}


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


def _integrated_starts() -> set[int]:
    """Already-integrated start years, derived from the live ipeds.db's
    ending years (empty if it doesn't exist yet — e.g. a brand new deploy)."""
    s = get_settings()
    if not Path(s.ipeds_db_path).exists():
        return set()
    return {y - 1 for y in importer._years(s.ipeds_db_path)}


@router.get("/import/catalog")
def import_catalog(refresh: bool = False):
    """The NCES year catalog merged with which ending years are already
    integrated into the live DB. `status` per year: 'integrated' (any
    release, never selectable again), 'final'/'provisional' (not yet
    integrated, available, selectable), or 'unknown' (NCES doesn't have it —
    not selectable). `partial=True` flags a degraded probe (some/all years
    could not be checked) so the UI can show a retry notice. `refresh=true`
    bypasses probe_catalog's in-process TTL cache (the toolbar's "Refresh")."""
    integrated_starts = _integrated_starts()
    partial = False
    try:
        catalog = nces.probe_catalog(refresh=refresh)
    except Exception as e:  # noqa: BLE001 — a probe failure must not 500 the page
        log.warning("NCES probe_catalog failed: %s", e)
        catalog = []
        partial = True

    by_year = {e["start_year"]: e for e in catalog}
    all_start_years = sorted(set(by_year) | integrated_starts)

    years = []
    for sy in all_start_years:
        entry = by_year.get(sy)
        if entry is None:
            # We know it's integrated (from _years) but NCES's probe didn't
            # cover it — still show it correctly, and flag the response.
            partial = True
            entry = {"year_label": f"{sy}-{str(sy + 1)[-2:]}", "year": sy + 1,
                     "available": False, "release": None}
        integrated = sy in integrated_starts
        available = bool(entry["available"])
        release = entry["release"]
        if integrated:
            status, selectable = "integrated", False
        elif available and release == "Final":
            status, selectable = "final", True
        elif available and release == "Provisional":
            status, selectable = "provisional", True
        else:
            status, selectable = "unknown", False
        years.append({
            "start_year": sy, "year": entry["year"], "year_label": entry["year_label"],
            "status": status, "integrated": integrated, "available": available,
            "release": release, "selectable": selectable,
        })
    return {"probed_at": time.time(), "partial": partial, "years": years}


class IntegrateRequest(BaseModel):
    years: list[int]


@router.post("/import/integrate")
def integrate(body: IntegrateRequest, admin: sqlite3.Row = Depends(require_admin)):
    if not _import_lock.acquire(blocking=False):
        raise HTTPException(409, "An import is already running. Wait for it to finish.")
    try:
        if not body.years:
            raise HTTPException(400, "Select at least one year to integrate.")

        current_year = datetime.now().year
        integrated_starts = _integrated_starts()
        catalog_by_year = {e["start_year"]: e for e in nces.probe_catalog()}

        for y in body.years:
            if type(y) is not int or not (nces.EARLIEST_START_YEAR <= y <= current_year + 1):  # noqa: E721
                raise HTTPException(400, f"{y} is not a valid NCES start year.")
            if y in integrated_starts:
                raise HTTPException(400, f"{y} is already integrated.")
            entry = catalog_by_year.get(y)
            if entry is None or not entry.get("available"):
                raise HTTPException(400, f"{y} is not available from NCES yet.")

        label = "integrate:" + ",".join(str(y) for y in sorted(body.years))
        job_id = importer.create_job(label, admin["email"])
    except Exception:
        _import_lock.release()
        raise

    def _run():
        try:
            importer.run_integrate(job_id, body.years)
        finally:
            _import_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "status": "pending"}


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
        # Pending (unverified) lessons sort FIRST — this list is an approval queue,
        # so the rows that need action must be at the top, not buried under the
        # verified library.
        rows = con.execute(
            "SELECT id, question, lesson, canonical_sql, notes, upvotes, downvotes, "
            "hits, verified, created_by, created_at FROM skills ORDER BY verified ASC, "
            "created_at DESC, id DESC LIMIT 500").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


class SkillUpdate(BaseModel):
    verified: bool | None = None
    lesson: str | None = None
    notes: str | None = None
    canonical_sql: str | None = None


@router.patch("/skills/{skill_id}")
def update_skill(skill_id: int, body: SkillUpdate):
    sets, vals = [], []
    if body.verified is not None:
        sets.append("verified=?"); vals.append(int(body.verified))
    if body.lesson is not None:
        sets.append("lesson=?"); vals.append(body.lesson)
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
def server_logs(limit: int = 200, level: str | None = None,
                q: str | None = None, since: float | None = None,
                until: float | None = None):
    """Persisted server log records (newest last), surviving restarts.

    Filters: `level` (INFO/WARNING/ERROR), case-insensitive substring `q` over
    the message, and a `since`/`until` epoch-seconds time window."""
    from app.logbuffer import get_handler
    handler = get_handler()
    if handler is None:
        return {"records": []}
    limit = max(1, min(limit, 2000))
    return {"records": handler.records(limit=limit, level=level, q=q,
                                       since=since, until=until)}
