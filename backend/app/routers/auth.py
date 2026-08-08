"""Auth routes: request a magic link, verify it, whoami, logout."""
from __future__ import annotations

import re
import sqlite3

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr

from app import auth
from app.auth import current_user
from app.config import get_settings
from app.ratelimit import client_ip, enforce_auth_rate_limit
from app.tools.sql import ipeds_years

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr


class VerifyRequest(BaseModel):
    token: str


@router.get("/config")
def public_config():
    # Unauthenticated on purpose: the login form renders before any session exists
    # and needs the domain to build its placeholder hint. Expose NOTHING else here —
    # the institution's email domain is public, the rest of the settings are not.
    return {"email_domain": get_settings().email_domain}


@router.post("/request")
def request_link(body: LoginRequest, request: Request, tasks: BackgroundTasks):
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
    # Non-consuming lookup so the confirmation page can name the account.
    #
    # POST, not GET, for the same reason the token moved to the fragment: a
    # query-string token is written to the server's access log verbatim. No
    # email ever pointed at this endpoint — only our own SPA calls it, and the
    # SPA ships in the same image — so there is no legacy GET to keep alive.
    return auth.peek_login(body.token)


@router.post("/verify")
def verify_post(body: VerifyRequest, response: Response):
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
