"""Admin API: allowlist, access requests, data imports, usage, skills."""
from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, EmailStr, Field, ValidationError

import app.nces as nces
from app import estimate, importer, skills
from app.auth import canon_email, require_admin
from app.config import get_settings
from app.db import connect
from app.mailer import send_access_approved

log = logging.getLogger("ipeds.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"],
                   dependencies=[Depends(require_admin)])

# Shared cap for every /*/bulk* endpoint below (see the "BULK OPERATIONS"
# section) -- a plain module constant, not a setting, since it's an
# implementation safety limit rather than deployment-tunable config (so
# ci_env.sh needs no new entry -- see CLAUDE.md's test-env-gotcha rule).
BULK_MAX_ITEMS = 1000


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
            # email is the tiebreak so a batch of CSV-imported rows (all sharing
            # one added_at) comes back in a STABLE order, not an arbitrary one that
            # shuffles between requests.
            "ORDER BY a.added_at DESC, a.email ASC").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _set_admin(con: sqlite3.Connection, email: str, is_admin: bool) -> None:
    """Grant or drop admin for an allowlisted `email`. Shared by the single
    PATCH /allowlist/{email} endpoint, add_allowlist's own is_admin checkbox,
    and the bulk promote/demote path below so none of the three can drift.
    The caller commits."""
    if is_admin:
        con.execute(
            "INSERT INTO users(email, is_admin, created_at) VALUES (?,1,?) "
            "ON CONFLICT(email) DO UPDATE SET is_admin=1", (email, time.time()))
    else:
        con.execute("UPDATE users SET is_admin=0 WHERE email=?", (email,))


def _approve_allowlist(con: sqlite3.Connection, email: str, note: str | None,
                       admin_email: str) -> bool:
    """Grant `email` access: insert (or refresh the note on) the allowlist
    row and clear a matching denial CANONICALLY. Shared by the single POST
    /allowlist endpoint and the bulk POST /access-requests/bulk
    {action:"approve"} path so the two can never drift. The caller commits.

    DB-ONLY — no mail, and (since approval no longer mints a magic-link token)
    NO login_tokens write either. The approval email is now a "you're approved,
    request a sign-in link" notice with no token, so nothing is tied to this
    transaction that has to be carried out to the mail provider. The caller still
    sends that notice AFTER commit + close (a mail-provider round-trip must never
    run while the app.db write lock is held) — but there's no longer a minted link
    to hand off, and one fewer write in the approval transaction.

    CANONICAL match (round 3 fold-in fix 1), not exact: a denial spans every
    +tag/case variant of `email` (see app.auth.canon_email / is_denied), so
    clearing it with an EXACT 'WHERE email=?' converted only the literal
    address typed in and left a variant's denied row behind. Months later,
    offboarding (DELETE /allowlist/{email}) would find that surviving row and
    is_denied() would resurrect the block — the exact scenario this
    widened-to-'denied' UPDATE was written to prevent, reintroduced through
    the variant it forgot. Textually identical predicate to
    app.auth.is_denied's — keep it that way, or the two drift and a block
    clears for one variant but not another. This does NOT canonicalize the
    allowlist itself (the INSERT stays EXACT) — only the denial-clearing
    side, which is safe because an admin explicitly approving one member of a
    mailbox group is unambiguous intent to clear the whole group's block.

    Returns True iff `email` was a genuinely NEW member (so the caller sends the
    approval notice); False when it was already allowlisted (a re-add only
    refreshes the note — nothing to send, the _send_and_classify_invite
    "already_allowlisted" branch)."""
    newly_added = con.execute("SELECT 1 FROM allowlist WHERE email=?",
                              (email,)).fetchone() is None
    con.execute("INSERT INTO allowlist(email, note, added_by, added_at) "
                "VALUES (?,?,?,?) ON CONFLICT(email) DO UPDATE SET note=excluded.note",
                (email, note, admin_email, time.time()))
    con.execute("UPDATE access_requests SET status='approved' "
                "WHERE status IN ('pending','denied') "
                "AND COALESCE(canon_email, LOWER(email))=?",
                (canon_email(email),))
    return newly_added


def _send_and_classify_invite(email: str, newly_added: bool, settings) -> tuple[str, bool]:
    """Send (or skip) the approval notice for `email` and classify what
    happened. MUST be called only AFTER the caller's write transaction has
    been committed and its connection closed — see _approve_allowlist's
    docstring: a mail-provider round-trip must never run while the app.db write
    lock is held.

    The notice contains NO magic link (approval no longer mints a token) — it
    tells the person they're approved and to request their own sign-in link.

    Returns (delivery, invited). `delivery` is what became of the notice, as ONE
    value — there are FOUR outcomes, not two, and each asks the admin to do
    something different. Deriving that from a pair of booleans is what let "was
    already on the allowlist" masquerade as "the email failed to send"."""
    invited = False
    mail_configured = bool(settings.resend_api_key)
    if not newly_added:
        # Already on the allowlist. Nothing was sent because nothing needed to
        # be; re-adding only updates the note. They can sign in any time.
        delivery = "already_allowlisted"
    else:
        try:
            invited = send_access_approved(email)
        except Exception as e:  # noqa: BLE001 — approval must not fail if email does
            log.warning("approval email to %s failed: %s", email, e)
        if invited:
            delivery = "emailed"
        elif mail_configured:
            delivery = "failed"
            # Re-report from a logger the admin can actually READ. send_email()
            # swallows provider errors and returns False, so the except above
            # never fires, and send_email's own log line is on the `ipeds.mail`
            # logger that logbuffer.py drops WHOLESALE. Without this, a failed
            # notice leaves NOTHING in the Logs tab. The person is already
            # approved and can request a link from the sign-in page regardless.
            log.warning(
                "approval email to %s was NOT delivered — mail is configured, so the "
                "provider rejected or errored (see the server console for the provider's "
                "own error). They are approved and can request a sign-in link from the "
                "sign-in page.",
                email)
        else:
            # No key: the mailer logged the whole email to the CONSOLE (no link to
            # leak — the notice carries none). Pointedly NOT in the Logs tab.
            delivery = "logged_to_console"
    return delivery, invited


@router.post("/allowlist")
def add_allowlist(body: AllowlistAdd, admin: sqlite3.Row = Depends(require_admin)):
    email = str(body.email).strip().lower()
    settings = get_settings()
    con = connect()
    try:
        if body.is_admin:
            _set_admin(con, email, True)
        newly_added = _approve_allowlist(con, email, body.note, admin["email"])
        con.commit()
    finally:
        con.close()
    # The approval notice is sent HERE — after the connection is committed and
    # closed, never while the write transaction (and the app.db write lock) is
    # open (see _approve_allowlist's docstring).
    delivery, invited = _send_and_classify_invite(email, newly_added, settings)
    return {"ok": True, "email": email, "invited": invited,
            "mail_configured": bool(settings.resend_api_key), "delivery": delivery}


# --- Bulk allowlist (CSV import) ----------------------------------------------
# The item email is a PLAIN str, not EmailStr: a single bad address in a batch of
# 200 must be skipped-and-reported, not 422 the whole request. Validate per row
# below via _valid_email() so one bad row never aborts the import.
class AllowlistBulkItem(BaseModel):
    email: str
    note: str | None = None
    is_admin: bool = False


class AllowlistBulkAdd(BaseModel):
    users: list[AllowlistBulkItem]


class _EmailProbe(BaseModel):
    email: EmailStr


def _valid_email(raw: str) -> str | None:
    """Authoritative per-row email check reusing pydantic's EmailStr. Returns the
    normalized (stripped, lowercased) address, or None if invalid."""
    try:
        return str(_EmailProbe(email=raw).email).strip().lower()
    except ValidationError:
        return None


@router.post("/allowlist/bulk")
def add_allowlist_bulk(body: AllowlistBulkAdd, tasks: BackgroundTasks,
                       admin: sqlite3.Row = Depends(require_admin)):
    """Bulk-add users from a CSV import. Inserts allowlist rows (and grants admin)
    and — after the write transaction commits and closes — emails each NEWLY-ADDED
    user a "you're approved, request a sign-in link" notice. Like every approval
    path, it mints NO sign-in token; the notice carries no link (imported users
    request their own from the sign-in page when ready), so there's nothing whose
    delivery the admin must chase. The sends go through BackgroundTasks, off the
    request path: a roster can be hundreds of rows, and we never block the response
    on that many provider round-trips. Does NOT touch access_requests. (Single-add
    clears a matching denial; bulk intentionally doesn't — a denied address
    surfaces here as any other row and, if not already allowlisted, is added, but
    no denial state is mutated.) Each row is independent: an invalid email or an
    in-request duplicate is skipped-and-reported, never fatal."""
    settings = get_settings()
    con = connect()
    added = 0
    admins_granted = 0
    skipped: list[dict] = []
    seen: set[str] = set()
    added_emails: list[str] = []
    try:
        now = time.time()
        for item in body.users:
            email = _valid_email(item.email)
            if not email:
                skipped.append({"email": item.email, "reason": "invalid email"})
                continue
            if email in seen:
                skipped.append({"email": email, "reason": "duplicate in file"})
                continue
            seen.add(email)
            if con.execute("SELECT 1 FROM allowlist WHERE email=?", (email,)).fetchone():
                skipped.append({"email": email, "reason": "already a user"})
                continue
            con.execute("INSERT INTO allowlist(email, note, added_by, added_at) VALUES (?,?,?,?)",
                        (email, item.note, admin["email"], now))
            if item.is_admin:
                con.execute("INSERT INTO users(email, is_admin, created_at) VALUES (?,1,?) "
                            "ON CONFLICT(email) DO UPDATE SET is_admin=1", (email, now))
                admins_granted += 1
            added += 1
            added_emails.append(email)
        con.commit()
    finally:
        con.close()
    # Approval notices go out AFTER commit + close, scheduled off the request path
    # (no app.db lock held, response not blocked on N sends).
    for email in added_emails:
        tasks.add_task(send_access_approved, email)
    return {"ok": True, "added": added, "admins_granted": admins_granted,
            "skipped": skipped, "mail_configured": bool(settings.resend_api_key)}


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
        _set_admin(con, email, body.is_admin)
        con.commit()
    finally:
        con.close()
    return {"ok": True, "email": email, "is_admin": body.is_admin}


def _remove_user(con: sqlite3.Connection, email: str) -> None:
    """Drop `email` from the allowlist, zero their admin flag, and kill their
    sessions. Shared by the single DELETE /allowlist/{email} endpoint and the
    bulk delete path below. Does NOT check the still-admin invariant itself —
    callers must skip a still-admin user BEFORE calling this (see both call
    sites). The caller commits."""
    con.execute("DELETE FROM allowlist WHERE email=?", (email,))
    con.execute("UPDATE users SET is_admin=0 WHERE email=?", (email,))
    con.execute(
        "DELETE FROM sessions WHERE user_id IN "
        "(SELECT id FROM users WHERE email=?)", (email,))


@router.delete("/allowlist/{email}")
def remove_allowlist(email: str, admin: sqlite3.Row = Depends(require_admin)):
    """Remove a user from the allowlist (drops their admin + kills their sessions).

    You can't remove YOURSELF — like the self-demote guard on the PATCH endpoint,
    this stops an accidental self-lockout and keeps at least one admin in place.

    You also can't remove someone who still HOLDS admin — demote them first. The
    UI disables the trash on an admin's row (Admin.jsx), and this is the
    authoritative enforcement of the same rule (defense in depth)."""
    email = email.strip().lower()
    if email == admin["email"].strip().lower():
        raise HTTPException(
            400, "You can't remove your own access — ask another admin.")
    con = connect()
    try:
        # Refuse to remove a still-admin user (a never-signed-in allowlist entry
        # has no users row → not admin → removal proceeds, as before).
        row = con.execute(
            "SELECT is_admin FROM users WHERE email=?", (email,)).fetchone()
        if row and row["is_admin"]:
            raise HTTPException(
                400, "Demote this user from admin before removing them.")
        _remove_user(con, email)
        con.commit()
    finally:
        con.close()
    return {"ok": True}


# --- Bulk allowlist actions (row-selection bulk actions on the Users table) ---
# See /home/todd/.claude/plans/bulk-operations-for-greedy-coral.md. Distinct
# from add_allowlist_bulk (POST /allowlist/bulk) above, which is the CSV-import
# path — this is promote/demote/delete over an explicit selection of already-
# allowlisted emails.
class AllowlistBulkAction(BaseModel):
    action: Literal["promote", "demote", "delete"]
    emails: list[str]


def _run_item(con: sqlite3.Connection, mutate, label) -> tuple[bool, object]:
    """Run one bulk-item's DB mutation inside its own SAVEPOINT, so a GENUINE
    per-item failure (a real exception raised by `mutate`) can be undone
    without discarding whatever already succeeded earlier in the same batch,
    or aborting the batch outright. Shared by every POST /*/bulk* endpoint
    below (code review #4 / M1) — before this, each one hardcoded
    `failed: []`, so the plan's "keep failed ids selected on partial
    failure" had nothing real behind it.

    On success: RELEASEs the savepoint (folding the change into the caller's
    own still-open outer transaction — see each caller's explicit
    `con.execute("BEGIN")`) and returns (True, mutate()'s return value).

    On a genuine exception: ROLLBACK TO the savepoint (undoing ONLY this
    item — everything committed by earlier items in the loop is untouched),
    RELEASEs it too (so it doesn't linger for the next item's SAVEPOINT of
    the same name), logs it, and returns (False, None). Never re-raises —
    one bad item must not sink ids that already succeeded or ids still to
    come; the caller appends {email|id: ..., "reason": "could not be
    updated"} to `failed` and continues the loop."""
    con.execute("SAVEPOINT bulk_item")
    try:
        value = mutate()
    except Exception as e:  # noqa: BLE001 — one bad item must not sink the whole batch
        con.execute("ROLLBACK TO bulk_item")
        con.execute("RELEASE bulk_item")
        log.warning("bulk action item %r failed: %s", label, e)
        return False, None
    con.execute("RELEASE bulk_item")
    return True, value


@router.post("/allowlist/bulk-action")
def allowlist_bulk_action(body: AllowlistBulkAction, admin: sqlite3.Row = Depends(require_admin)):
    """Bulk promote/demote/delete over an explicit list of allowlist emails —
    one connection, one commit. Eligibility (already-admin / not-admin /
    still-admin) is RECOMPUTED per email against live DB state, exactly like
    the single PATCH/DELETE endpoints above (whose _set_admin/_remove_user
    mutation helpers this reuses), never trusted from whatever the browser's
    stale list showed.

    demote/delete reject the WHOLE batch with a 400 (nothing changes, no
    email even looked up) if the caller's own email is included — the same
    self-lockout guard as the single endpoints, checked BEFORE any write.
    promote never self-guards: promoting an already-admin self is simply a
    no-op, reported as a skip, same as any other already-admin row.

    Each item's actual mutation runs through _run_item's per-item SAVEPOINT
    (M1): a genuine per-item failure lands in `failed` (never in `skipped`,
    which is reserved for expected ineligibility) so the caller can re-select
    exactly those ids for a retry.

    A cheap explicit assert that at least one admin remains runs right before
    the commit, as defense in depth — self-exclusion from demote/delete
    already guarantees this structurally (the caller is always an admin and
    is never itself touched), but this catches the invariant breaking for any
    reason without relying solely on that structural argument."""
    if len(body.emails) > BULK_MAX_ITEMS:
        raise HTTPException(400, f"Send at most {BULK_MAX_ITEMS} emails at a time.")
    caller = admin["email"].strip().lower()
    emails = [e.strip().lower() for e in body.emails]
    if body.action in ("demote", "delete") and caller in emails:
        raise HTTPException(
            400, "You can't include your own account in a bulk demote/remove.")

    affected = 0
    skipped: list[dict] = []
    failed: list[dict] = []
    con = connect()
    try:
        # One explicit outer transaction spans every item's own SAVEPOINT
        # below (see _run_item). Without it, RELEASEing the FIRST item's
        # savepoint would auto-commit that item outright (nothing else holds
        # a transaction open yet), and the last-admin-invariant rollback
        # further down would have nothing left to undo.
        con.execute("BEGIN")
        for email in emails:
            row = con.execute(
                "SELECT COALESCE(u.is_admin, 0) AS is_admin FROM allowlist a "
                "LEFT JOIN users u ON u.email=a.email WHERE a.email=?",
                (email,)).fetchone()
            if row is None:
                skipped.append({"email": email, "reason": "not on the allowlist"})
                continue
            is_admin = bool(row["is_admin"])
            if body.action == "promote":
                if is_admin:
                    skipped.append({"email": email, "reason": "already an administrator"})
                    continue
                ok, _value = _run_item(con, lambda e=email: _set_admin(con, e, True), email)
            elif body.action == "demote":
                if not is_admin:
                    skipped.append({"email": email, "reason": "not an administrator"})
                    continue
                ok, _value = _run_item(con, lambda e=email: _set_admin(con, e, False), email)
            else:  # delete
                if is_admin:
                    skipped.append(
                        {"email": email, "reason": "is an administrator — demote first"})
                    continue
                ok, _value = _run_item(con, lambda e=email: _remove_user(con, e), email)
            if not ok:
                failed.append({"email": email, "reason": "could not be updated"})
                continue
            affected += 1

        admin_count = con.execute(
            "SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0]
        if admin_count == 0:
            con.rollback()
            raise HTTPException(400, "This would leave no administrators — nothing was changed.")
        con.commit()
    finally:
        con.close()
    return {"ok": True, "affected": affected, "skipped": skipped, "failed": failed}


@router.get("/access-requests")
def access_requests():
    """Pending access requests, collapsed one row per address. Both outcomes
    (approve and deny) are per-address and flip EVERY pending row for that
    address, so a list showing each raw row would render duplicate
    Approve/Reject pairs for one person, all doing the identical thing.
    MIN(id) gives a stable React key; MAX(created_at) is the most recent
    request, which is what the DESC sort should mean; MAX(reason) is a no-op
    in practice (request_login's INSERT never sets reason) but is used rather
    than relying on SQLite's bare-column-in-GROUP-BY behavior."""
    con = connect()
    try:
        rows = con.execute(
            "SELECT MIN(id) AS id, email, MAX(reason) AS reason, status, "
            "MAX(created_at) AS created_at "
            "FROM access_requests WHERE status='pending' "
            "GROUP BY email ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


@router.get("/access-requests/denied")
def access_requests_denied():
    """Every address an admin has denied, collapsed one row per CANONICAL
    group — deliberately a DIFFERENT grouping from access_requests() above.
    Approve is EXACT (add_allowlist inserts the literal address), so the
    pending list groups by the raw `email`; deny/undo are CANONICAL (a block
    spans +tag/case variants — see app.auth.canon_email/is_denied), so this
    list groups by COALESCE(canon_email, LOWER(email)) instead. One Undo
    control per group clears every variant in it — see clear_access_denial.

    `emails` carries every distinct ORIGINAL address in the group, sorted —
    the UI renders these, never `canon_email` itself, which exists only as
    the argument to DELETE .../denial. Post-processed in Python rather than
    with SQL's json_group_array so this stays a plain array; safe to split
    on a comma because addresses reach this table only through
    LoginRequest.email (EmailStr), which cannot contain one."""
    con = connect()
    try:
        # created_at = when the group was REQUESTED (MAX = most recent request);
        # denied_at = when it was REJECTED (MAX = most recent denial). Kept
        # separate — the Blocked-users table shows both columns. denied_at is NULL
        # for rows denied before migration 11 (rendered "—" client-side).
        rows = con.execute(
            "SELECT MIN(id) AS id, "
            "COALESCE(canon_email, LOWER(email)) AS canon_email, "
            "GROUP_CONCAT(DISTINCT email) AS emails, "
            "MAX(created_at) AS created_at, "
            "MAX(denied_at) AS denied_at "
            "FROM access_requests WHERE status='denied' "
            "GROUP BY COALESCE(canon_email, LOWER(email)) "
            "ORDER BY denied_at DESC").fetchall()
        return [
            {"id": r["id"], "canon_email": r["canon_email"],
             "emails": sorted(set(r["emails"].split(","))),
             "created_at": r["created_at"], "denied_at": r["denied_at"]}
            for r in rows
        ]
    finally:
        con.close()


def _deny_group(con: sqlite3.Connection, canon: str) -> int:
    """UPDATE every PENDING row sharing `canon`'s canonical address to
    denied, stamping denied_at now (never rewrites created_at). Shared by the
    single POST /access-requests/{email}/deny endpoint and the bulk
    POST /access-requests/bulk {action:"reject"} path so the two can never
    drift. Returns the rowcount (0 = nothing pending for that canonical
    address). COALESCE fallback matches app.auth.is_denied's — see that
    function's docstring for why. The caller commits."""
    cur = con.execute(
        "UPDATE access_requests SET status='denied', denied_at=? "
        "WHERE status='pending' AND COALESCE(canon_email, LOWER(email))=?",
        (time.time(), canon))
    return cur.rowcount


@router.post("/access-requests/{email}/deny")
def deny_access_request(email: str):
    """Deny every pending request sharing `email`'s CANONICAL address (see
    app.auth.canon_email) and block that whole group from filing new ones
    (app.auth.is_denied). Keyed on the canonical address, not a row id or the
    raw address: an address can have several pending rows (request_login
    never dedupes) across +tag/case variants that all reach the same
    mailbox, and denying only the exact string typed in would leave those
    variants — and hence the underlying mailbox — un-denied and bypassable.

    The row is UPDATEd, never deleted — its persistence IS the block.
    Reversible WITHOUT granting access via
    DELETE /access-requests/{email}/denial (see clear_access_denial below),
    which un-blocks the whole canonical group and sends no email. Allowlisting
    the address also clears the block (see add_allowlist) but is a stronger
    action — it grants full access AND emails a welcome link, which is not
    always what an admin undoing a mistaken denial wants."""
    email = email.strip().lower()
    target = canon_email(email)
    con = connect()
    try:
        rowcount = _deny_group(con, target)
        con.commit()
        if rowcount == 0:
            raise HTTPException(404, "No pending access request for that address.")
    finally:
        con.close()
    return {"ok": True, "email": email}


def _clear_denial_group(con: sqlite3.Connection, canon: str) -> int:
    """DELETE every DENIED row sharing `canon`'s canonical address, returning
    the whole group to "never requested". Shared by the single DELETE
    /access-requests/{email}/denial endpoint and the bulk
    POST /access-requests/denial/bulk {action:"unblock"} path so the two can
    never drift. Idempotent (clearing an already-cleared group is a 0-rowcount
    no-op, never an error). `status='denied'` is a guard, never widen it — see
    clear_access_denial's docstring. The caller commits."""
    cur = con.execute(
        "DELETE FROM access_requests WHERE status='denied' "
        "AND COALESCE(canon_email, LOWER(email))=?", (canon,))
    return cur.rowcount


@router.delete("/access-requests/{email}/denial")
def clear_access_denial(email: str):
    """Undo a denial: return `email`'s canonical group to "never requested"
    by DELETING its denied rows outright, never re-statusing them. An admin
    calling this is declaring the underlying request fictitious, and "never
    requested" is the intended terminal state (see .plan-undeny.md). The verb
    matches the handler exactly: unlike /deny (which must UPDATE, never
    DELETE — see that endpoint's docstring), this genuinely IS a DELETE FROM,
    on a different resource (the denial, not the request itself).

    Grants NO access and sends NO email — that absence is the whole
    requirement. The only prior way to un-block a denied address was
    allowlisting it, which grants full access AND emails a welcome link;
    reproducing that here would be the exact bug this endpoint exists to fix.

    `status='denied'` is a guard, never widen it — it protects the group's
    inert 'approved'/'pending' history from being swept up too.

    Canonical, and the predicate is textually identical to
    app.auth.is_denied's on purpose — keep it that way, or a block clears for
    one variant of a mailbox but leaves another still blocked.

    Always 200, never 404 — deliberately unlike /deny's 404-on-nothing-to-do:
    DELETE is idempotent by contract, so clearing an already-cleared denial
    is a success by definition, same as the neighbouring
    DELETE /allowlist/{email}. `cleared` lets a caller distinguish if it
    cares; the UI reloads the list either way, so a double-click
    self-corrects visually instead of flashing a spurious error."""
    email = email.strip().lower()
    target = canon_email(email)
    con = connect()
    try:
        cleared = _clear_denial_group(con, target)
        con.commit()
    finally:
        con.close()
    return {"ok": True, "email": email, "cleared": cleared}


# --- Bulk access-request actions (row-selection bulk actions on the Pending
# requests / Blocked users tables) --------------------------------------------
# See /home/todd/.claude/plans/bulk-operations-for-greedy-coral.md. Both
# endpoints key on the access_requests row id (the Pending/Blocked tables'
# MIN(id) group representative), never the raw email — an id is stable across
# a status transition, so it doubles as the cross-table safety check: an id
# that isn't in the state the caller's endpoint expects (e.g. a denied id
# posted to the pending-only /access-requests/bulk) is recognized as
# no-longer-eligible and skipped, never mutated. The SERVER is the sole
# authority here — the frontend only ever sends a table's own ids, but nothing
# stops a stale tab, a replayed request, or a bug from sending the wrong ones.
class AccessRequestsBulkAction(BaseModel):
    action: Literal["approve", "reject"]
    ids: list[int]


@router.post("/access-requests/bulk")
def access_requests_bulk(body: AccessRequestsBulkAction,
                         admin: sqlite3.Row = Depends(require_admin)):
    """Bulk approve/reject over an explicit list of access_requests row ids —
    one connection, one commit. Each id is resolved to its row FRESH (ANY
    status, not just pending), so a row already resolved one way or the
    other — approved, or denied/blocked — is recognized as no longer pending
    and skipped ("already resolved"), never silently re-mutated; this is what
    makes a denied id posted here safe (it can never be approved this way,
    closing the cross-table hole described above).

    approve reuses _approve_allowlist — the same allowlist insert + canonical
    denial-clear as the single Approve button (minting a sign-in link for
    each genuinely NEW member DURING the transaction below), additionally
    skipping an id whose address is already allowlisted (a stale/re-requested
    address). reject reuses _deny_group — the same canonical UPDATE
    (denied_at=now, created_at preserved) as the single Reject button.

    The DB transaction commits and its connection closes BEFORE any approval
    email goes out: every newly-added address approved this call is collected
    during the loop and its notice sent afterward, never while the app.db write
    lock is held. Each item's mutation also runs through _run_item's per-item
    SAVEPOINT (M1): a genuine per-item failure lands in `failed`, never `skipped`
    (reserved for expected ineligibility), and its notice is never queued."""
    if len(body.ids) > BULK_MAX_ITEMS:
        raise HTTPException(400, f"Send at most {BULK_MAX_ITEMS} ids at a time.")
    settings = get_settings()
    approver = admin["email"]
    affected = 0
    skipped: list[dict] = []
    failed: list[dict] = []
    # Emails newly added THIS call — the approval notice is sent only after
    # commit + close, per the docstring.
    to_invite: list[str] = []
    con = connect()
    try:
        # See allowlist_bulk_action's identical comment: one explicit outer
        # transaction must span every item's SAVEPOINT, or RELEASEing the
        # first one would auto-commit it immediately.
        con.execute("BEGIN")
        for rid in body.ids:
            row = con.execute(
                "SELECT email, status FROM access_requests WHERE id=?", (rid,)).fetchone()
            if row is None:
                skipped.append({"id": rid, "reason": "not found"})
                continue
            if row["status"] != "pending":
                skipped.append({"id": rid, "reason": "already resolved"})
                continue
            email = row["email"].strip().lower()
            if body.action == "approve":
                if con.execute("SELECT 1 FROM allowlist WHERE email=?", (email,)).fetchone():
                    skipped.append({"id": rid, "reason": "already allowlisted"})
                    continue
                note = f"approved via bulk action by {approver}"
                ok, newly_added = _run_item(
                    con,
                    lambda e=email, n=note: _approve_allowlist(con, e, n, approver),
                    rid)
                if not ok:
                    failed.append({"id": rid, "reason": "could not be updated"})
                    continue
                if newly_added:
                    to_invite.append(email)
            else:  # reject
                ok, _value = _run_item(con, lambda e=email: _deny_group(con, canon_email(e)), rid)
                if not ok:
                    failed.append({"id": rid, "reason": "could not be updated"})
                    continue
            affected += 1
        con.commit()
    finally:
        con.close()
    for email in to_invite:
        _send_and_classify_invite(email, True, settings)
    return {"ok": True, "affected": affected, "skipped": skipped, "failed": failed}


class DenialBulkAction(BaseModel):
    action: Literal["unblock"]
    ids: list[int]


@router.post("/access-requests/denial/bulk")
def access_requests_denial_bulk(body: DenialBulkAction,
                                admin: sqlite3.Row = Depends(require_admin)):
    """Bulk-clear denials over an explicit list of access_requests row ids —
    one connection, one commit. Each id is resolved to its CANONICAL address,
    then reuses _clear_denial_group — the same idempotent canonical DELETE as
    the single DELETE .../denial endpoint: grants NO access, sends NO email.
    An id that isn't currently blocked (unknown, not denied, or already
    cleared by an earlier item in the same canonical group) is skipped "not
    blocked", never a 500 or a mutation to any other row.

    Each item's actual DELETE runs through _run_item's per-item SAVEPOINT
    (M1): a genuine per-item failure lands in `failed`, never `skipped`."""
    if len(body.ids) > BULK_MAX_ITEMS:
        raise HTTPException(400, f"Send at most {BULK_MAX_ITEMS} ids at a time.")
    affected = 0
    skipped: list[dict] = []
    failed: list[dict] = []
    con = connect()
    try:
        # See allowlist_bulk_action's identical comment: one explicit outer
        # transaction must span every item's SAVEPOINT, or RELEASEing the
        # first one would auto-commit it immediately.
        con.execute("BEGIN")
        for rid in body.ids:
            row = con.execute(
                "SELECT email, status FROM access_requests WHERE id=?", (rid,)).fetchone()
            if row is None or row["status"] != "denied":
                skipped.append({"id": rid, "reason": "not blocked"})
                continue
            target = canon_email(row["email"].strip().lower())
            ok, cleared = _run_item(con, lambda t=target: _clear_denial_group(con, t), rid)
            if not ok:
                failed.append({"id": rid, "reason": "could not be updated"})
                continue
            if cleared == 0:
                skipped.append({"id": rid, "reason": "not blocked"})
                continue
            affected += 1
        con.commit()
    finally:
        con.close()
    return {"ok": True, "affected": affected, "skipped": skipped, "failed": failed}


# --- Data import --------------------------------------------------------------
# Only one import may run at a time: concurrent rebuilds would race the same
# ipeds_staging.db / atomic swap. Held from upload through run_import.
_import_lock = threading.Lock()


@router.post("/import")
async def start_import(background: BackgroundTasks,
                       files: list[UploadFile] = File(...),
                       admin: sqlite3.Row = Depends(require_admin)):
    if not files:
        raise HTTPException(400, "Please upload at least one IPEDS .accdb file.")
    for uf in files:
        if not uf.filename or not uf.filename.lower().endswith(".accdb"):
            raise HTTPException(400, "Every uploaded file must be an IPEDS .accdb file.")
    if not _import_lock.acquire(blocking=False):
        raise HTTPException(409, "An import is already running. Wait for it to finish.")

    s = get_settings()
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = s.max_upload_mb * 1024 * 1024
    dests: list[Path] = []
    try:
        # Stream each file to disk; the size cap is the TOTAL across the batch.
        written = 0
        for uf in files:
            dest = s.upload_dir / Path(uf.filename).name
            dests.append(dest)
            with dest.open("wb") as f:
                while True:
                    chunk = await uf.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise HTTPException(
                            413, f"Upload exceeds the {s.max_upload_mb} MB limit.")
                    f.write(chunk)
        names = ", ".join(p.name for p in dests)
        label = names if len(dests) == 1 else f"{len(dests)} files: {names}"
        job_id = importer.create_job(label, admin["email"])
    except Exception:
        for d in dests:
            d.unlink(missing_ok=True)
        _import_lock.release()
        raise

    def _run():
        try:
            importer.run_import(job_id, dests)
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
    ending years (empty if it doesn't exist yet -- e.g. a brand new deploy).

    NOTE: kept calling `importer._years(...)` (a bare module attribute) rather
    than the newer `app.tools.sql.ipeds_years()` non-raising probe, even
    though the two are functionally equivalent -- eval/test_admin_router.py's
    `_patch_catalog`/`_patch_years` helpers monkeypatch
    `admin_router.importer._years` and document that the router must call it
    "through the module ... for these patches to take effect." Switching to
    `ipeds_years()` would silently break that (untouched-by-this-feature)
    test contract. See the no-data-onboarding implementer's report."""
    s = get_settings()
    if not Path(s.ipeds_db_path).exists():
        return set()
    return {y - 1 for y in importer._years(s.ipeds_db_path)}


def _year_provenance() -> dict[int, str | None]:
    """start_year -> the release it was integrated as ('Final'/'Provisional'),
    or None if unknown (no year_provenance row at all — e.g. integrated before
    this feature existed — or a manual upload, which records release=NULL).
    Both "no row" and "NULL release" collapse to the same None here, which is
    exactly what _derive_status wants: neither is ever an "update"."""
    con = connect()
    try:
        rows = con.execute("SELECT start_year, release FROM year_provenance").fetchall()
        return {r["start_year"]: r["release"] for r in rows}
    finally:
        con.close()


def _derive_status(integrated: bool, available: bool,
                   release: str | None, provenance_release: str | None) -> tuple[str, bool]:
    """The single source of truth for a year's catalog status+selectability,
    shared by GET /import/catalog and POST /import/integrate's validation so
    the two can never drift apart.

    - integrated + provenance was Provisional + NCES now offers Final ->
      "update" (selectable — re-integrating picks up the better release).
    - integrated otherwise (Final-as-Final, or provenance unknown/NULL) ->
      "integrated" (never selectable again).
    - not integrated + available -> "final"/"provisional" (selectable).
    - not integrated + not available -> "unknown" (not selectable)."""
    if integrated:
        if provenance_release == "Provisional" and available and release == "Final":
            return "update", True
        return "integrated", False
    if available and release == "Final":
        return "final", True
    if available and release == "Provisional":
        return "provisional", True
    return "unknown", False


@router.get("/import/catalog")
def import_catalog(refresh: bool = False):
    """The NCES year catalog merged with which ending years are already
    integrated into the live DB. `status` per year: 'integrated' (never
    selectable again, unless a Provisional integration now has a Final
    release out — see 'update'), 'update' (integrated as Provisional, Final
    is now available — selectable), 'final'/'provisional' (not yet
    integrated, available, selectable), or 'unknown' (NCES doesn't have it —
    not selectable). `partial=True` flags a degraded probe (some/all years
    could not be checked) so the UI can show a retry notice. `refresh=true`
    bypasses probe_catalog's in-process TTL cache (the toolbar's "Refresh").
    Also carries `disk` (free/total/used bytes on the ipeds.db volume) and
    `calibration` (the estimator's knobs + derived per-year-db-size) so the
    Imports tab can render a live disk-headroom meter."""
    integrated_starts = _integrated_starts()
    provenance = _year_provenance()
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
                     "available": False, "release": None, "zip_bytes": None}
        integrated = sy in integrated_starts
        available = bool(entry["available"])
        release = entry["release"]
        status, selectable = _derive_status(
            integrated, available, release, provenance.get(sy))
        years.append({
            "start_year": sy, "year": entry["year"], "year_label": entry["year_label"],
            "status": status, "integrated": integrated, "available": available,
            "release": release, "selectable": selectable,
            "zip_bytes": entry.get("zip_bytes"),
        })

    s = get_settings()
    dc = estimate.disk_and_calibration(s, integrated_year_count=len(integrated_starts))
    du = shutil.disk_usage(Path(s.ipeds_db_path).parent)
    return {"probed_at": time.time(), "partial": partial, "years": years,
           "disk": {"free_bytes": du.free, "total_bytes": du.total, "used_bytes": du.used},
           "calibration": dc["calibration"]}


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
        provenance = _year_provenance()
        catalog_by_year = {e["start_year"]: e for e in nces.probe_catalog()}

        for y in body.years:
            if type(y) is not int or not (nces.EARLIEST_START_YEAR <= y <= current_year + 1):  # noqa: E721
                raise HTTPException(400, f"{y} is not a valid NCES start year.")
            entry = catalog_by_year.get(y)
            integrated = y in integrated_starts
            available = bool(entry and entry.get("available"))
            release = entry.get("release") if entry else None
            _status, selectable = _derive_status(
                integrated, available, release, provenance.get(y))
            if not selectable:
                if integrated:
                    raise HTTPException(400, f"{y} is already integrated.")
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


@router.delete("/import/year/{start_year}")
def deintegrate(start_year: int, admin: sqlite3.Row = Depends(require_admin)):
    """Remove an already-integrated year from the live database (the
    "trashcan"): a full offline rebuild of live minus that year's rows, with
    its own de-integration integrity checks — see importer.run_deintegrate.
    Single-flight with /import and /import/integrate via the same
    `_import_lock` (they'd otherwise race the same ipeds_staging.db)."""
    if not _import_lock.acquire(blocking=False):
        raise HTTPException(409, "An import is already running. Wait for it to finish.")
    try:
        integrated_starts = _integrated_starts()
        if start_year not in integrated_starts:
            raise HTTPException(400, f"{start_year} is not integrated.")
        if len(integrated_starts) <= 1:
            raise HTTPException(400, "Can't remove the only integrated year.")

        job_id = importer.create_job(f"deintegrate:{start_year}", admin["email"])
    except Exception:
        _import_lock.release()
        raise

    def _run():
        try:
            importer.run_deintegrate(job_id, start_year)
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
            "COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
            "COALESCE(SUM(cached_prompt_tokens),0) AS cached_prompt_tokens, "
            "COALESCE(SUM(first_call_prompt_tokens),0) AS first_call_prompt_tokens, "
            "COALESCE(SUM(first_call_cached_prompt_tokens),0) AS first_call_cached_prompt_tokens, "
            "COALESCE(SUM(cost),0.0) AS spend, "
            "COALESCE(SUM(cached),0) AS cache_hits, "
            "COALESCE(SUM(escalated),0) AS escalations, "
            "COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) AS failures, "
            # Figure grounding (app/grounding.py), observe-only: of the turns
            # that led with a hero figure AND had query results to check it
            # against, how many carried a number reproducible from those
            # results. 'no_figure'/'unchecked'/NULL turns are excluded from BOTH
            # counts — they are not evidence either way, and counting them as
            # grounded would flatter the rate.
            "COALESCE(SUM(CASE WHEN figure_grounding IN "
            "('exact','rounded','derived','ungrounded') THEN 1 ELSE 0 END),0) "
            "AS figures_checked, "
            "COALESCE(SUM(CASE WHEN figure_grounding='ungrounded' THEN 1 ELSE 0 END),0) "
            "AS figures_ungrounded, "
            # Structured-emission telemetry (PR-1): of the real agent turns (an
            # emit_mode was recorded — excludes cache hits/refusals/NULL), how
            # many used the structured tool, and how many LEAKED residual debris
            # into the shipped prose. The leak rate → 0 under structured emission
            # is the win that justifies flipping the default.
            "COALESCE(SUM(CASE WHEN emit_mode IN ('structured','fence') THEN 1 ELSE 0 END),0) "
            "AS emit_turns, "
            "COALESCE(SUM(CASE WHEN emit_mode='structured' THEN 1 ELSE 0 END),0) "
            "AS structured_turns, "
            "COALESCE(SUM(answer_leaked),0) AS leaked_turns "
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
        # Spend-health probe: is the "Spend" number trustworthy, or silently 0?
        # Spend is provider-reported (OpenRouter's usage.cost); a provider that
        # doesn't report it leaves cost=0 unless the admin sets fallback prices.
        # This is deliberately DECOUPLED from the selected window (a fixed 30-day
        # look-back) so the warning is a stable config-health signal, not a
        # per-range artifact that vanishes when you pick a quiet hour. It fires
        # only when there's real LLM activity (a non-answer-cache turn that spent
        # tokens) whose cost never landed AND no fallback prices are set — the two
        # conditions in one: with prices set, cost>0 for those turns, so
        # priced_turns>0 and the warning clears; likewise once the provider starts
        # reporting. See app/llm.py effective_cost + docs/ADMIN_GUIDE.md.
        s = get_settings()
        prices_configured = (s.llm_input_cost_per_mtok > 0
                             or s.llm_output_cost_per_mtok > 0)
        health = con.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN cached=0 AND (prompt_tokens+completion_tokens)>0 "
            "THEN 1 ELSE 0 END),0) AS llm_turns, "
            "COALESCE(SUM(CASE WHEN cost>0 THEN 1 ELSE 0 END),0) AS priced_turns "
            "FROM usage_log WHERE created_at >= ?", (now - 30 * 86400,)).fetchone()
        cost_warning = (health["llm_turns"] > 0 and health["priced_turns"] == 0
                        and not prices_configured)
        return {"since": since, "until": until, "bucket": "hour" if hourly else "day",
                "totals": dict(totals),
                "series": [dict(r) for r in series],
                "top_users": [dict(r) for r in top_users],
                "cost_warning": cost_warning}
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
            "SELECT id, question, headline, lesson, canonical_sql, notes, upvotes, "
            "downvotes, hits, verified, created_by, created_at FROM skills "
            "ORDER BY verified ASC, created_at DESC, id DESC LIMIT 500").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


class SkillUpdate(BaseModel):
    verified: bool | None = None
    headline: str | None = Field(default=None, max_length=300)
    lesson: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=4000)
    canonical_sql: str | None = Field(default=None, max_length=8000)


@router.patch("/skills/{skill_id}")
def update_skill(skill_id: int, body: SkillUpdate):
    sets, vals = [], []
    if body.verified is not None:
        sets.append("verified=?"); vals.append(int(body.verified))
    if body.headline is not None:
        sets.append("headline=?"); vals.append(body.headline)
    if body.lesson is not None:
        sets.append("lesson=?"); vals.append(body.lesson)
    if body.notes is not None:
        sets.append("notes=?"); vals.append(body.notes)
    if body.canonical_sql is not None:
        sets.append("canonical_sql=?"); vals.append(body.canonical_sql)
    if not sets:
        return {"ok": True}
    con = connect()
    try:
        # The embedding derives from headline+lesson (app.skills._embed_source),
        # so editing either one makes the stored vector stale — recompute it in
        # the same request. A verify-only PATCH touches neither field and skips
        # this entirely.
        if body.headline is not None or body.lesson is not None:
            row = con.execute(
                "SELECT headline, lesson FROM skills WHERE id=?", (skill_id,)).fetchone()
            new_headline = body.headline if body.headline is not None else (row["headline"] or "")
            new_lesson = body.lesson if body.lesson is not None else (row["lesson"] or "")
            v = skills.embed(skills._embed_source(new_headline, new_lesson))
            # Only write a vector we actually have. embed() returns None when
            # fastembed didn't load, and NULLing here would drop the lesson out
            # of retrieval for good — reembed_skills_if_needed() won't rescue it
            # once the source-version marker is set. A stale vector still
            # retrieves; NULL never does. Same principle that function already
            # applies to a rule-less row.
            if v is not None:
                sets.append("embedding=?")
                vals.append(skills._to_blob(v))
        vals.append(skill_id)
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


# --- Attention counts ---------------------------------------------------------
# Powers the admin "attention" badges (top-bar total + per-section counts): how
# much work is waiting in each area. Keys match the frontend ADMIN_TABS names so
# the client can map counts[tab] straight onto a nav badge. Only the areas with an
# actionable backlog appear — users (pending access), skills (unverified lessons),
# logs (problems since this admin last looked). imports/usage have no backlog.
@router.get("/attention")
def attention_counts(admin: sqlite3.Row = Depends(require_admin)):
    from app.logbuffer import get_handler
    con = connect()
    try:
        # Grouped like the pending list (one badge unit per distinct address).
        users = con.execute(
            "SELECT COUNT(DISTINCT email) FROM access_requests "
            "WHERE status='pending'").fetchone()[0]
        skills_pending = con.execute(
            "SELECT COUNT(*) FROM skills WHERE verified=0").fetchone()[0]
        row = con.execute("SELECT seen_ts FROM admin_log_seen WHERE email=?",
                          (admin["email"],)).fetchone()
        seen_ts = float(row[0]) if row else 0.0
    finally:
        con.close()
    handler = get_handler()
    logs = handler.count_problems(seen_ts) if handler is not None else 0
    return {"users": int(users), "skills": int(skills_pending), "logs": int(logs)}


@router.post("/logs/seen")
def mark_logs_seen(admin: sqlite3.Row = Depends(require_admin)):
    """Acknowledge the current log problems for THIS admin: advance their
    seen_ts to now, so the Logs attention badge clears and only counts problems
    logged afterward. Per-admin so it never clears the badge for someone else."""
    con = connect()
    try:
        con.execute(
            "INSERT INTO admin_log_seen(email, seen_ts) VALUES(?, ?) "
            "ON CONFLICT(email) DO UPDATE SET seen_ts=excluded.seen_ts",
            (admin["email"], time.time()))
        con.commit()
    finally:
        con.close()
    return {"logs": 0}
