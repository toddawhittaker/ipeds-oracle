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


def purge_expired_auth_rows(con: sqlite3.Connection) -> None:
    """Delete auth rows the code can never accept again: consumed or expired
    magic-link tokens, sessions past their expiry, and spent or expired OIDC
    login states. The caller commits.

    Behaviour-preserving by construction — `peek_login`/`verify_login` already
    reject a token whose `used_at` is set or whose `expires_at` has passed, and
    `current_user` already rejects an expired session. Afterwards the lookup
    simply misses instead of failing the timestamp check: same 400, same message.
    Nothing else sweeps these two tables (`auth_request_attempts` is swept by
    `ratelimit`), so without this they grow forever.

    Called at boot (`main.lifespan`) and on each successful sign-in — deliberately
    NOT from `mint_login_link`. That helper runs on only ONE of `request_login`'s
    branches, so a DELETE there would make "allowlisted" measurably slower than
    "pending", opening exactly the timing oracle `request_login`'s docstring
    promises stays closed. A sign-in has no such exposure: the caller already
    holds a valid token."""
    now = time.time()
    con.execute("DELETE FROM login_tokens WHERE used_at IS NOT NULL OR expires_at < ?",
                (now,))
    con.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    # Same argument as login_tokens above: `oidc.complete_login` already refuses
    # a state whose `used_at` is set or whose `expires_at` has passed, so
    # removing those rows changes no outcome -- the lookup misses instead of
    # failing the check, and both raise invalid_state.
    con.execute("DELETE FROM oidc_logins WHERE used_at IS NOT NULL OR expires_at < ?",
                (now,))


def mint_login_link(con: sqlite3.Connection, email: str) -> str:
    """Insert a single-use login token for `email` and return its verify URL.
    The caller commits.

    The URL is built from the CANONICAL origin (`app_public_url`), NOT from the
    request — a magic link from a request-derived base (`request.base_url`) follows
    the client-supplied `Host` header, so an attacker could make the server email a
    victim a genuine, signed sign-in link pointing at an attacker domain
    (link-poisoning → account takeover). Reading the configured origin here, inside
    the minter, means no caller can ever reintroduce that foot-gun."""
    token = new_token()
    con.execute(
        "INSERT INTO login_tokens(token_hash, email, expires_at) VALUES (?,?,?)",
        (hash_token(token), email.strip().lower(), magic_link_expiry()))
    # Point at the SPA confirmation page, not the consuming API endpoint: the
    # page shows a "Sign in" button that POSTs the token. Email link-scanners
    # that GET this URL therefore can't burn the single-use link.
    #
    # The token rides in the URL **FRAGMENT**, never the query string, and that
    # is a security property rather than a style choice. `/verify` is an SPA
    # route served by main.py's catch-all, so a `?token=` link wrote the raw
    # single-use token into uvicorn's access log on PAGE LOAD — before any API
    # call — and anyone who could read `docker logs` could replay it into an
    # account takeover. A fragment is never transmitted to the server at all, so
    # it cannot be logged by us, by the operator's reverse proxy, or by a
    # tunnel — none of which we control, and all of which the README's
    # self-hosting posture assumes. Verify.jsx reads it from location.hash.
    base_url = get_settings().app_public_url
    return f"{base_url.rstrip('/')}/verify#token={token}"


def request_login(email: str, tasks: BackgroundTasks) -> dict:
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
    "nothing is happening" by how long the response took.

    A residual, DB-local difference is ACCEPTED, not equalized: the denied and
    unknown branches skip the INSERT+commit that the allowlisted and pending
    branches do, so they do marginally less work (sub-millisecond, swamped by
    network jitter). Crucially it does NOT isolate the sensitive states — it
    groups {allowlisted, pending} against {denied, unknown}, so it can't tell
    "denied" from "unknown" nor "allowlisted" from "pending". Equalizing it would
    mean performing throwaway INSERT/commit work on a path whose whole contract
    is "store nothing on deny", trading a real invariant for a non-exploitable
    micro-signal — deliberately not done."""
    email = email.strip().lower()
    con = connect()
    try:
        if is_allowlisted(con, email):
            link = mint_login_link(con, email)
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


def idp_identity_conflicts(con: sqlite3.Connection, email: str,
                           issuer: str, subject: str) -> bool:
    """True when `email` is already bound to a DIFFERENT provider subject.

    The account key in this app is the email address, and at several providers
    the `email` claim is neither verified nor immutable -- see migration 39 for
    the takeover this prevents. `sub` is the identifier a provider does promise
    is stable, so the first OIDC sign-in for an address records the pair and
    every later one has to match it.

    Trust on first use: an unbound account (never signed in via OIDC, or created
    before migration 39) does not conflict -- it binds on this sign-in.
    """
    row = con.execute("SELECT oidc_iss, oidc_sub FROM users WHERE email=?",
                      (email,)).fetchone()
    if not row or not row["oidc_sub"]:
        return False
    return row["oidc_iss"] != issuer or row["oidc_sub"] != subject


def bind_idp_identity(con: sqlite3.Connection, email: str,
                      issuer: str, subject: str) -> None:
    """Record the provider identity for `email`, if it has none yet.

    Only ever fills a blank: an established binding is what
    `idp_identity_conflicts` checks against, so overwriting it here would undo
    the guard the moment an attacker got one sign-in through. Does NOT commit.
    """
    con.execute(
        "UPDATE users SET oidc_iss=?, oidc_sub=? WHERE email=? AND oidc_sub IS NULL",
        (issuer, subject, email))


def provision_from_idp(con: sqlite3.Connection, email: str, added_by: str) -> None:
    """Grant `email` access on the strength of an external provider's word.

    What this writes is an ALLOWLIST row, and that is the whole design. The
    allowlist is re-checked on EVERY authenticated request
    (`_user_from_request`) and on every MCP call (`apikeys.verify`), and those
    two checks are the app's only per-request kill switch. Provisioning by
    writing the row keeps them -- and Admin -> Users, offboarding, and API-key
    revocation -- working exactly as they already do. The alternative, making
    those checks method-aware, would leave a removed user's 30-day session
    cookie and their API keys alive until they happened to expire.

    `ON CONFLICT DO NOTHING`, never `DO UPDATE`: a row from the ADMIN_EMAILS
    bootstrap, or one an admin added by hand, carries a note and an `added_by`
    recording where that access came from. Somebody's first SSO sign-in must not
    overwrite that with "auto-provisioned".

    Does NOT commit -- it joins the caller's sign-in transaction, so a login that
    fails after this point grants nothing.
    """
    con.execute(
        "INSERT INTO allowlist(email, note, added_by, added_at) VALUES (?,?,?,?) "
        "ON CONFLICT(email) DO NOTHING",
        (email, "auto-provisioned on first sign-in", added_by, time.time()))


def create_session(con: sqlite3.Connection, email: str) -> tuple[str, sqlite3.Row]:
    """Upsert the user for `email` and mint one session row for them.

    THE ONLY PLACE A SESSION ROW IS EVER WRITTEN. Every way in converges here:
    whatever proves the address -- a consumed magic-link token today, an OIDC
    id_token or an LDAP bind later -- does its own proving and then calls this,
    so there is one definition of what being signed in means.

    Takes an OPEN connection and deliberately does NOT commit, so it joins the
    caller's transaction. That is what stops a session existing for a magic-link
    token that was never marked used: the UPDATE and this INSERT commit together
    or not at all.

    Only the SHA-256 hash reaches `sessions` -- the raw token is returned and
    never stored, so a dump of app.db mints nothing.
    """
    now = time.time()
    con.execute("INSERT INTO users(email, created_at, last_login) VALUES (?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET last_login=excluded.last_login",
                (email, now, now))
    user = con.execute("SELECT id, email, is_admin FROM users WHERE email=?",
                       (email,)).fetchone()
    sess = new_token()
    con.execute(
        "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) "
        "VALUES (?,?,?,?)",
        (hash_token(sess), user["id"], now, session_expiry()))
    return sess, user


def set_session_cookie(response: Response, token: str) -> None:
    """THE ONLY PLACE THE SESSION COOKIE IS SET.

    Kept separate from `create_session` because the two halves run at different
    moments: the DB write joins the caller's transaction, while the cookie is
    applied AFTER the connection closes and -- for a redirect-based sign-in --
    onto a RedirectResponse the handler builds for itself. Folding them into one
    function would force such a caller to construct its response before opening
    the database, purely to satisfy a signature.

    `samesite="lax"` is load-bearing rather than a default worth tightening:
    `strict` would drop this cookie on the cross-site top-level GET that an
    external identity provider redirects back with, landing the user signed out
    with no error to read.
    """
    s = get_settings()
    response.set_cookie(
        s.cookie_name, token, max_age=s.session_ttl_days * 86400,
        httponly=True, secure=s.cookie_secure, samesite="lax", path="/")


def verify_login(token: str, response: Response) -> dict:
    """Consume a magic-link token, upsert the user, and set a session cookie."""
    th = hash_token(token)
    con = connect()
    try:
        # Sweep dead rows first, before this token is marked used — so the token
        # being consumed right now survives until the NEXT sign-in.
        purge_expired_auth_rows(con)
        row = con.execute(
            "SELECT email, expires_at, used_at FROM login_tokens WHERE token_hash=?",
            (th,)).fetchone()
        if not row or row["used_at"] is not None or row["expires_at"] < time.time():
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "This sign-in link is invalid or expired.")
        email = row["email"]
        con.execute("UPDATE login_tokens SET used_at=? WHERE token_hash=?",
                    (time.time(), th))
        sess, user = create_session(con, email)
        con.commit()
    finally:
        con.close()
    set_session_cookie(response, sess)
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
