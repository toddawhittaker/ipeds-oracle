"""Auth routes: one sign-in door per deployment (magic link, OIDC or LDAP),
plus whoami and logout.

Each door's routes are registered unconditionally and gated at request time by
the RESOLVED method -- see `_require_magic_link` / `_require_oidc` and
docs/AUTH_AND_SECURITY.md for why both halves have to be gated, not just the
new ones."""
from __future__ import annotations

import logging
import re
import sqlite3
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field, SecretStr

from app import auth, ldapauth
from app import oidc as oidc_mod
from app.auth import current_user
from app.authmethod import LDAP, MAGIC_LINK, OIDC, resolve_auth_method
from app.config import _log_safe, get_settings
from app.db import connect
from app.ratelimit import (
    client_ip,
    enforce_auth_ip_rate_limit,
    enforce_auth_rate_limit,
    record_auth_attempt,
)
from app.tools.sql import ipeds_years

log = logging.getLogger("ipeds.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr


class VerifyRequest(BaseModel):
    token: str


class LdapLoginRequest(BaseModel):
    # SecretStr so the password is masked in FastAPI's own 422 echo and in every
    # repr — a validation error on this endpoint would otherwise quote it back.
    username: str = Field(max_length=256)
    # Bounded like the username: without it the app-wide 10 MB body cap is the
    # only limit, and in direct-bind mode a multi-megabyte password is forwarded
    # verbatim to the directory.
    password: SecretStr = Field(max_length=1024)


@router.get("/config")
def public_config():
    # Unauthenticated on purpose: the login form renders before any session
    # exists and has to know which door to draw.
    #
    # The rule for this endpoint is the METHOD'S NAME and a display label, never
    # an endpoint, a credential, or a directory detail. The active method is
    # unavoidably public — any visitor sees which form they got — and the email
    # domain always was. The issuer URL, client id, client secret and every
    # group/domain fence are either credentials or free reconnaissance about the
    # institution, and none of them belong in an unauthenticated response.
    s = get_settings()
    return {"email_domain": s.email_domain,
            "auth_method": resolve_auth_method(s),
            "oidc_button_label": s.oidc_button_label}


def _require_magic_link() -> None:
    """404 unless the magic link is the method actually in force.

    Without this, "exactly one method" is what the docs say and not what the
    code does. Under an identity provider every SSO user has an allowlist row
    (auto-provisioning writes one), and `request_login` checks the allowlist
    FIRST -- so anyone the provider has since deactivated, removed from
    OIDC_REQUIRED_GROUP, or put behind MFA could simply ask for an email link
    and walk straight past all of it. The browser hid the form, which made the
    open door invisible to everyone except somebody looking for it.

    It also closes the access-request flood surface on a deployment whose docs
    say that door is shut.

    Bootstrapping is unaffected: ADMIN_EMAILS still grants the first admin, and
    an operator whose provider breaks changes AUTH_METHOD back in .env -- which
    is also what happens automatically when the OIDC config is incomplete."""
    if resolve_auth_method(get_settings()) != MAGIC_LINK:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")


@router.post("/request")
def request_link(body: LoginRequest, request: Request, tasks: BackgroundTasks):
    _require_magic_link()
    email = str(body.email).strip().lower()
    enforce_auth_rate_limit(email, client_ip(request))
    # The sign-in link is built from the canonical `app_public_url` inside
    # mint_login_link — NOT from `request.base_url`, which follows the attacker-
    # controllable Host header (link-poisoning → account takeover). `request` is
    # still needed for the rate-limiter's client IP.
    # tasks is threaded through to request_login so it can schedule its
    # outbound email (fire-and-forget) rather than send it inline — see that
    # function's docstring for why every branch must do this, not just some.
    return auth.request_login(email, tasks)


# The shape of what `security.new_token()` mints: `secrets.token_urlsafe(32)`,
# i.e. base64url. Bounds are generous so a future token size still passes.
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")


@router.get("/verify")
def verify_get(token: str):
    _require_magic_link()
    # A GET never consumes the token — email link-scanners / prefetchers that
    # follow the link must not burn a single-use sign-in link. Bounce to the
    # SPA confirmation page, which shows a button that POSTs to consume it.
    # (Kept so old-style /api/auth/verify links still land somewhere sensible.)
    #
    # The redirect target uses a FRAGMENT, matching mint_login_link. A `?token=`
    # target would be pointless here: the browser would follow it and the token
    # would land in the access log on the redirected page load anyway, making
    # the whole fix cosmetic.
    #
    # A token that isn't SHAPED like one we mint is dropped rather than
    # reflected. It cannot be a link we sent, so the only thing forwarding it
    # achieves is bouncing attacker-chosen text through our origin into a page
    # the victim just landed on. This is also what closes CodeQL's
    # py/url-redirection (alert #44): the redirect target is now constant except
    # for a value matched against a strict allowlist.
    #
    # Not a vulnerability being patched — PROBED both ways first, and neither
    # works: the `/verify#` prefix is constant, so `//evil.com` and
    # `https://evil.com` stay same-origin (they land in the fragment), and
    # Starlette percent-encodes CR/LF, so `\r\nSet-Cookie:` cannot split the
    # header. This is defence in depth plus a clean alert queue, not a fix for a
    # live hole. Dropping to a bare `/verify` lands on the SPA's own "this link
    # is missing its token" state, which is the honest outcome for a token we
    # would refuse anyway.
    if not _TOKEN_SHAPE.fullmatch(token):
        return RedirectResponse(url="/verify", status_code=303)
    return RedirectResponse(url=f"/verify#token={token}", status_code=303)


@router.post("/verify-info")
def verify_info(body: VerifyRequest):
    _require_magic_link()
    # Non-consuming lookup so the confirmation page can name the account.
    #
    # POST, not GET, for the same reason the token moved to the fragment: a
    # query-string token is written to the server's access log verbatim. No
    # email ever pointed at this endpoint — only our own SPA calls it, and the
    # SPA ships in the same image — so there is no legacy GET to keep alive.
    return auth.peek_login(body.token)


@router.post("/verify")
def verify_post(body: VerifyRequest, response: Response):
    _require_magic_link()
    # Only a deliberate POST (the user clicking "Sign in") consumes the token
    # and sets the session cookie.
    return auth.verify_login(body.token, response)


@router.get("/me")
def me(user: sqlite3.Row = Depends(current_user)):
    # ONE probe answers both questions. has_ipeds_data() is itself just
    # bool(ipeds_years()), so calling ipeds_years() directly and deriving the
    # flag from it costs one fewer read than asking twice.
    #
    # `years` exists because the chat empty state used to STATE the loaded range
    # as fact ("2019-20 through 2024-25") while every deployment picks its own
    # years via Admin -> Imports — `_years` is the only authority. The browser
    # formats the collection-year labels (year is the ENDING year, so 2020 reads
    # as "2019-20"); the server just reports the bounds.
    years = ipeds_years()
    return {"email": user["email"], "is_admin": bool(user["is_admin"]),
            "has_data": bool(years),
            "years": {"min": years[0], "max": years[-1]} if years else None,
            # Only the RESOLVED boolean crosses to the browser — never the raw
            # setting or any other config. Gates the chat privacy warning only.
            "trust_llm_provider": get_settings().trust_llm_provider_enabled,
            # Same rule, same reason as `years` above: the browser was PRINTING
            # this number ("First 200 rows · the full result is larger") from a
            # hardcoded constant, while sql_row_cap_model is env-overridable per
            # deployment. A deployment that raised or lowered it told its readers
            # a figure that was simply wrong. The resolved int crosses; the
            # setting itself does not.
            "sql_row_cap": get_settings().sql_row_cap_model}


@router.post("/logout")
def logout(request: Request, response: Response):
    auth.logout(request, response)
    return {"ok": True}


def _require_oidc() -> None:
    """404 unless OIDC is the method actually in force.

    The two routes below are registered UNCONDITIONALLY, because route
    registration that depends on an env read at import time makes any test which
    flips AUTH_METHOD tell a lie. This check is what stops a magic-link
    deployment leaving a half-working OIDC entry point exposed -- and it uses the
    RESOLVED method, so a deployment whose OIDC config is incomplete (and which
    is therefore serving magic link) closes these routes too."""
    if resolve_auth_method(get_settings()) != OIDC:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")


def _oidc_failure(code: str) -> RedirectResponse:
    """Send the browser back to the door carrying a code from the CLOSED set.

    Never a provider-supplied string. This is a redirect target, and reflecting
    text into one is exactly the py/url-redirection shape the magic-link bounce
    (`verify_get` above) already had to close."""
    if code not in oidc_mod.AUTH_ERRORS:
        code = "provider_error"
    resp = RedirectResponse(f"/?auth_error={code}",
                            status_code=status.HTTP_303_SEE_OTHER)
    # The attempt is over either way, so the binding cookie goes with it --
    # otherwise a stale one sits in the browser until its TTL and the next
    # failure is harder to read.
    oidc_mod.clear_state_cookie(resp)
    return resp


@router.post("/oidc/start")
def oidc_start(request: Request, response: Response):
    """Mint a pending login and hand the SPA the URL to send the browser to.

    A POST returning JSON rather than a GET that 302s, for three reasons: the
    door keeps its layout and can render a failure inline instead of bouncing
    the visitor somewhere; a GET redirecting from our origin to a foreign one is
    the redirection shape this repo already fought once; and a POST passes
    through CSRFMiddleware's origin check, so a foreign page cannot make a
    browser mint state rows here."""
    _require_oidc()
    # Unauthenticated, so it is rate-limited per IP. Each call writes a row AND
    # can issue an outbound discovery request from a threadpool worker, so an
    # unlimited burst is both unbounded growth in app.db and a way to park every
    # sync route's thread behind a slow provider.
    enforce_auth_ip_rate_limit(client_ip(request))
    con = connect()
    try:
        url, state = oidc_mod.begin_login(con)
        con.commit()
    except oidc_mod.OidcError as e:
        log.warning("OIDC start failed (%s): %s", e.code, _log_safe(e.detail))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Single sign-on is unavailable right now. Try again, or contact "
            "your administrator.") from e
    finally:
        con.close()
    oidc_mod.set_state_cookie(response, state)
    return {"authorization_url": url}


@router.get("/oidc/callback")
def oidc_callback(request: Request, code: str | None = None,
                  state: str | None = None, error: str | None = None):
    """Finish the login the provider is redirecting back from.

    A GET, so CSRFMiddleware skips it (`csrf.SAFE_METHODS`) -- which is correct
    rather than a gap: the single-use `state` row IS this leg's CSRF defence, and
    an Origin check could not work here anyway because the request comes from the
    provider's redirect, not from our own page.

    Order matters and is the security property: the browser binding, the
    id_token and the domain/group fences are checked inside `complete_login`,
    THEN the local block list, THEN the provider-identity binding, THEN
    provisioning, THEN the session. A blocked or mismatched address must never
    be provisioned on its way to being refused."""
    _require_oidc()
    cookie_state = request.cookies.get(oidc_mod.state_cookie_name())
    if error or not code or not state:
        # The provider refused (consent declined, and so on), or something
        # arrived here without the two parameters a real callback carries.
        # `error` is provider-controlled text and is logged, never echoed.
        if error:
            log.warning("OIDC provider returned an error at the callback: %r",
                        _log_safe(error)[:200])
        return _oidc_failure("provider_error" if error else "invalid_state")

    # THREE PHASES, and the split is deliberate: no app.db transaction may span
    # a provider round trip. Holding the write lock across the token exchange
    # makes every other writer in the app fail on busy_timeout when the provider
    # is slow, which is routine for a cold tenant.
    con = connect()
    try:
        try:
            nonce, verifier = oidc_mod.claim_state(con, state, cookie_state)
        finally:
            # Burn the row whatever happened. Single-use has to mean a single
            # ATTEMPT, or a replayed callback URL simply gets another go.
            con.commit()
    except oidc_mod.OidcError as e:
        log.warning("OIDC sign-in failed (%s): %s", e.code, _log_safe(e.detail))
        return _oidc_failure(e.code)
    finally:
        con.close()

    # Phase 2: the network, holding no connection at all.
    try:
        identity = oidc_mod.exchange_code(code, nonce, verifier)
    except oidc_mod.OidcError as e:
        log.warning("OIDC sign-in failed (%s): %s", e.code, _log_safe(e.detail))
        return _oidc_failure(e.code)

    # Phase 3: decide and record, in one short transaction.
    email = identity.email
    con = connect()
    try:
        # The allowlist is checked FIRST, exactly as `request_login` does it --
        # `auth.is_denied`'s docstring states the invariant: allowlisting a
        # denied person always wins. Getting this order wrong locks out anyone
        # re-added through the CSV bulk import, which deliberately does not clear
        # a denial: the admin sees them listed as a user while sign-in refuses
        # them, with no signal anywhere.
        if not auth.is_allowlisted(con, email) and auth.is_denied(con, email):
            # The provider is the authority on identity, not on access to this
            # application. A directory keeps departed staff and alumni for years,
            # so the block list is the only local revocation lever there is.
            log.warning("OIDC sign-in refused: %s is blocked in this deployment",
                        _log_safe(email))
            return _oidc_failure("denied")

        # The account is keyed on the email claim, and at several providers that
        # claim is neither verified nor immutable -- so an address already bound
        # to a different provider subject is somebody else. See migration 39.
        if auth.idp_identity_conflicts(con, email, identity.issuer, identity.subject):
            log.warning("OIDC sign-in refused: %s is bound to a different provider "
                        "subject than the one presented", _log_safe(email))
            return _oidc_failure("not_authorized")

        # The only sweep that runs on this path. `verify_login` carries it for
        # magic link, and an OIDC deployment never reaches that -- so without
        # this, oidc_logins, sessions and login_tokens are swept once per
        # restart, fed by an unauthenticated endpoint.
        auth.purge_expired_auth_rows(con)
        issuer_host = (urlsplit(get_settings().oidc_issuer.strip()).hostname or "?").lower()
        auth.provision_from_idp(con, email, f"oidc:{issuer_host}")
        sess, _user = auth.create_session(con, email)
        auth.bind_idp_identity(con, email, identity.issuer, identity.subject)
        con.commit()
    finally:
        con.close()

    resp = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    auth.set_session_cookie(resp, sess)
    oidc_mod.clear_state_cookie(resp)
    return resp


# One sentence for EVERY LDAP rejection — wrong password, unknown user, not in
# the group, locally blocked, directory unreachable. Anything that varies by
# cause is a directory enumeration oracle; the reason goes to the log, which
# only an admin reads.
_LDAP_REFUSED = "Sign-in failed. Check your username and password."


@router.post("/ldap")
def ldap_login(body: LdapLoginRequest, request: Request, response: Response):
    """Bind a username and password against the directory and sign in.

    Rate-limited BEFORE the directory is touched: this is the one method where
    online password guessing is possible, and an unlimited endpoint would also
    let an attacker use the directory's round trips as a work amplifier. The
    limiter's `email` column holds a username here — it is a TEXT bucket key,
    and that is fine."""
    if resolve_auth_method(get_settings()) != LDAP:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    username = body.username.strip()
    ip = client_ip(request)
    # Two budgets, charged differently on purpose.
    #
    # The per-IP one is charged on EVERY request, success included: each call is
    # a directory round trip and a session row with a ~30-day TTL, so one valid
    # credential in a loop must still be bounded.
    #
    # The per-USERNAME one is only charged on FAILURE. A password form is
    # mistyped far more often than an email address is, and counting successes
    # too would walk somebody who signs in daily into a lockout for no reason.
    # What bounds guessing is that every WRONG answer still counts, against both.
    enforce_auth_ip_rate_limit(ip)
    enforce_auth_rate_limit(username.lower(), ip, record=False)

    try:
        email = ldapauth.authenticate(username, body.password.get_secret_value())
    except ldapauth.LdapError as e:
        record_auth_attempt(username.lower(), ip)
        log.warning("LDAP sign-in refused for %s: %s",
                    _log_safe(username), _log_safe(str(e)))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _LDAP_REFUSED) from e
    except Exception as e:  # noqa: BLE001 -- an unreachable directory is a refusal too
        record_auth_attempt(username.lower(), ip)
        log.exception("LDAP sign-in errored for %s", _log_safe(username))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _LDAP_REFUSED) from e

    con = connect()
    try:
        # Same order and the same reasoning as the OIDC callback: the allowlist
        # wins over a denial (auth.is_denied's documented invariant), and a
        # blocked address is never provisioned on its way to being refused.
        if not auth.is_allowlisted(con, email) and auth.is_denied(con, email):
            record_auth_attempt(username.lower(), ip)
            log.warning("LDAP sign-in refused: %s is blocked in this deployment",
                        _log_safe(email))
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, _LDAP_REFUSED)
        auth.purge_expired_auth_rows(con)
        auth.provision_from_idp(con, email, "ldap")
        sess, user = auth.create_session(con, email)
        con.commit()
    finally:
        con.close()
    auth.set_session_cookie(response, sess)
    return {"email": email, "is_admin": bool(user["is_admin"])}
