"""Passwordless auth: magic-link request/verify, sessions, and the FastAPI
dependencies that gate the app. Access is restricted to a manual allowlist.
"""
from __future__ import annotations

import sqlite3
import time

from fastapi import BackgroundTasks, Depends, HTTPException, Request, Response, status

from app.config import get_settings
from app.db import connect
from app.mailer import send_access_request, send_magic_link
from app.security import hash_token, magic_link_expiry, new_token, session_expiry


def is_allowlisted(con: sqlite3.Connection, email: str) -> bool:
    """EXACT match only — and it must stay that way. Exact-string matching is
    fail-CLOSED for an allowlist (an unmatched +tag/case variant of an
    allowlisted address gets NO access — safe) but fail-OPEN for a denylist
    (an unmatched variant of a denied address is left UNBLOCKED — unsafe; see
    is_denied/canon_email below, deliberately the OPPOSITE polarity). Do not
    "make this consistent" with is_denied — that would let a stranger sign in
    as someone else's +tag variant of an allowlisted address."""
    return con.execute("SELECT 1 FROM allowlist WHERE email=?",
                       (email,)).fetchone() is not None


def canon_email(email: str) -> str:
    """Canonical form used ONLY for denylist matching (is_denied / the admin
    deny endpoint) — never for the allowlist (see is_allowlisted's comment on
    why the two must have opposite polarity). Lowercases and strips an
    RFC-5233 `+tag` local-part suffix, e.g. `Mallory+1@Example.EDU` ->
    `mallory@example.edu`: Gmail/Google Workspace/Microsoft 365 all deliver
    `user+tag@domain` to the same mailbox as `user@domain`, so an admin's
    "block this address" action must span every +tag variant or it is
    trivially bypassable by anyone who controls that mailbox.

    Deliberately does NOT collapse dots in the local part. Unlike +tags, dots
    are not guaranteed to route to the same mailbox on every provider —
    `john.smith@` and `johnsmith@` can be two different real people on many
    mail systems — so stripping them would risk blocking an innocent third
    party who was never the admin's actual target."""
    email = email.strip().lower()
    local, sep, domain = email.partition("@")
    local = local.split("+", 1)[0]
    return f"{local}{sep}{domain}"


def is_denied(con: sqlite3.Connection, email: str) -> bool:
    """True when an admin has denied this address, or a +tag/case variant of
    it (see canon_email) — any denied row for the canonical address blocks,
    so the address may not file a new access request. Checked AFTER the
    allowlist, so allowlisting a denied person always wins.

    COALESCE(canon_email, LOWER(email)): canon_email is populated on every
    row this app inserts (request_login) and backfilled for pre-existing rows
    (migration 9), so it is effectively never NULL in production — the
    fallback to a plain lowercase match only covers a row that somehow reached
    this table without going through either path, and keeps that case
    conservative (exact-match) rather than silently matching nothing.

    One more subtlety in that fallback, deliberately left alone: SQLite's
    built-in LOWER() only folds ASCII A-Z (it has no Unicode casefolding
    table), while Python's str.lower() — used by canon_email() above, and by
    whatever wrote the row's canon_email in the first place — folds full
    Unicode. So for a row with a non-ASCII uppercase local part, the
    COALESCE fallback's LOWER(email) and this function's canon_email(email)
    CAN disagree. That's fail-closed, not a live bug: it can only make a row
    LESS matchable (nothing gets denied that shouldn't be, nothing un-blocks
    that shouldn't), and it's unreachable in production because canon_email
    is populated at write time by this same Python function for every row
    the app itself ever inserts — the NULL-triggered fallback only exists
    for a row that predates that. Do not "fix" this by teaching SQLite a
    Unicode-aware LOWER() or similar; there is nothing here that needs it."""
    return con.execute(
        "SELECT 1 FROM access_requests "
        "WHERE status='denied' AND COALESCE(canon_email, LOWER(email))=? LIMIT 1",
        (canon_email(email),)).fetchone() is not None


def admin_recipients(con: sqlite3.Connection) -> list[str]:
    """Every address that should be notified of an access request: all current
    admins (`users.is_admin=1`, whether bootstrapped from ADMIN_EMAILS or
    promoted at runtime), plus the configured `access_request_to` override and
    the bootstrap admin list. Deduped, lower-cased, order-stable."""
    s = get_settings()
    seen: list[str] = []

    def add(email: str | None) -> None:
        if not email:
            return
        email = email.strip().lower()
        if email and email not in seen:
            seen.append(email)

    for row in con.execute("SELECT email FROM users WHERE is_admin=1"):
        add(row["email"])
    add(s.access_request_to)
    for email in s.admin_email_list:
        add(email)
    return seen


def may_request_access(email: str) -> bool:
    """True when `email` is allowed to file an access request. `EMAIL_DOMAIN`, when
    set, keeps unsolicited requests to the institution's own addresses so a stranger
    can't burn Resend quota or flood the admins' inboxes. Empty = no restriction.
    Sign-in is NOT gated by this — see `request_login`."""
    domain = get_settings().email_domain.strip().lower().lstrip("@")
    if not domain:
        return True
    return email.rsplit("@", 1)[-1] == domain


def mint_login_link(con: sqlite3.Connection, email: str, base_url: str) -> str:
    """Insert a single-use login token for `email` and return its verify URL.
    The caller commits. Reused by the login flow and by admin approval."""
    token = new_token()
    con.execute(
        "INSERT INTO login_tokens(token_hash, email, expires_at) VALUES (?,?,?)",
        (hash_token(token), email.strip().lower(), magic_link_expiry()))
    # Point at the SPA confirmation page, not the consuming API endpoint: the
    # page shows a "Sign in" button that POSTs the token. Email link-scanners
    # that GET this URL therefore can't burn the single-use link.
    return f"{base_url.rstrip('/')}/verify?token={token}"


def request_login(email: str, base_url: str, tasks: BackgroundTasks) -> dict:
    """Start a login. Returns a neutral message either way (never reveals whether
    an address is on the allowlist, denied, or simply unknown). Allowlisted →
    emails a link; denied → nothing; otherwise (in-domain, not yet decided) →
    files an access request and notifies the admin.

    An allowlisted address ALWAYS gets its link, whatever its domain — the allowlist
    is the sole authority on sign-in, so a cross-domain admin or contractor keeps
    working on an `EMAIL_DOMAIN`-configured deployment.

    Branch ORDER is the security property, not just its outcome:
    - allowlisted must be checked FIRST, so allowlisting a previously-denied
      address un-blocks it for free (see app.routers.admin.add_allowlist,
      which also converts the denied row to 'approved' so the block can't
      resurrect if the address is later removed from the allowlist). An admin
      can also clear a denial WITHOUT allowlisting — see
      app.routers.admin.clear_access_denial (DELETE
      /access-requests/{email}/denial) — which un-blocks the address but
      grants no access and sends no email, unlike allowlisting.
    - denied must be checked SECOND, before may_request_access — otherwise a
      denied in-domain address would keep inserting rows and emailing admins
      on every retry, i.e. the deny feature would do nothing.

    Every outbound send is SCHEDULED via `tasks.add_task`, never called
    inline, on EVERY branch that has one — this is a security property, not
    an optimization. With the default EMPTY `EMAIL_DOMAIN`, `may_request_access`
    is True for everyone, so a denied address is the ONLY branch that skips
    outbound network I/O; if the allowlisted/fresh-pending branches sent their
    email inline (a real Resend round-trip, ~100s of ms) while denied returned
    immediately, wall-clock alone would tell a caller "denied" from every
    other outcome — a 400x+ timing oracle, measured. Returning before any
    network I/O happens, on every branch, closes that channel: the caller
    can no longer distinguish "your email is being sent right now" from
    "nothing is happening" by how long the response took."""
    email = email.strip().lower()
    con = connect()
    try:
        if is_allowlisted(con, email):
            link = mint_login_link(con, email, base_url)
            con.commit()
            tasks.add_task(send_magic_link, email, link)
        elif is_denied(con, email):
            pass  # Blocked: nothing stored, nothing sent, no distinguishing work.
        elif may_request_access(email):
            con.execute(
                "INSERT INTO access_requests(email, canon_email, created_at) "
                "VALUES (?,?,?)",
                (email, canon_email(email), time.time()))
            con.commit()
            admins = admin_recipients(con)
            if admins:
                tasks.add_task(send_access_request, admins, email)
        # An out-of-domain stranger falls through: nothing stored, nothing sent —
        # but it still returns the message below verbatim. Saying anything else
        # would reveal which domains the deployment serves.
    finally:
        con.close()
    return {"message": "If that address is approved, a sign-in link is on its "
                       "way. Otherwise, an access request has been sent to the "
                       "administrator."}


def peek_login(token: str) -> dict:
    """Look up the email for a pending magic-link token WITHOUT consuming it, so
    the sign-in confirmation page can say whom the link signs in. Raises if the
    token is unknown, already used, or expired. Only a holder of a valid token
    (i.e. an allowlisted user who was emailed one) can learn anything here."""
    th = hash_token(token)
    con = connect()
    try:
        row = con.execute(
            "SELECT email, expires_at, used_at FROM login_tokens WHERE token_hash=?",
            (th,)).fetchone()
    finally:
        con.close()
    if not row or row["used_at"] is not None or row["expires_at"] < time.time():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "This sign-in link is invalid or expired.")
    return {"email": row["email"]}


def verify_login(token: str, response: Response) -> dict:
    """Consume a magic-link token, upsert the user, and set a session cookie."""
    s = get_settings()
    th = hash_token(token)
    con = connect()
    try:
        row = con.execute(
            "SELECT email, expires_at, used_at FROM login_tokens WHERE token_hash=?",
            (th,)).fetchone()
        if not row or row["used_at"] is not None or row["expires_at"] < time.time():
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "This sign-in link is invalid or expired.")
        email = row["email"]
        con.execute("UPDATE login_tokens SET used_at=? WHERE token_hash=?",
                    (time.time(), th))
        # upsert user
        con.execute("INSERT INTO users(email, created_at, last_login) VALUES (?,?,?) "
                    "ON CONFLICT(email) DO UPDATE SET last_login=excluded.last_login",
                    (email, time.time(), time.time()))
        user = con.execute("SELECT id, email, is_admin FROM users WHERE email=?",
                           (email,)).fetchone()
        # create session
        sess = new_token()
        con.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) "
            "VALUES (?,?,?,?)",
            (hash_token(sess), user["id"], time.time(), session_expiry()))
        con.commit()
    finally:
        con.close()
    response.set_cookie(
        s.cookie_name, sess, max_age=s.session_ttl_days * 86400,
        httponly=True, secure=s.cookie_secure, samesite="lax", path="/")
    return {"email": email, "is_admin": bool(user["is_admin"])}


def logout(request: Request, response: Response) -> None:
    s = get_settings()
    tok = request.cookies.get(s.cookie_name)
    if tok:
        con = connect()
        try:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (hash_token(tok),))
            con.commit()
        finally:
            con.close()
    response.delete_cookie(s.cookie_name, path="/")


def _user_from_request(request: Request) -> sqlite3.Row | None:
    s = get_settings()
    tok = request.cookies.get(s.cookie_name)
    if not tok:
        return None
    con = connect()
    try:
        row = con.execute(
            "SELECT u.id, u.email, u.is_admin, s.expires_at "
            "FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=?", (hash_token(tok),)).fetchone()
        if row and not is_allowlisted(con, row["email"]):
            return None
    finally:
        con.close()
    if not row or row["expires_at"] < time.time():
        return None
    return row


def current_user(request: Request) -> sqlite3.Row:
    user = _user_from_request(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")
    return user


def require_admin(user: sqlite3.Row = Depends(current_user)) -> sqlite3.Row:
    if not user["is_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only.")
    return user
