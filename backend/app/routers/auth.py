"""Auth routes: request a magic link, verify it, whoami, logout."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr

from app import auth
from app.auth import current_user
from app.config import get_settings
from app.ratelimit import client_ip, enforce_auth_rate_limit
from app.tools.sql import has_ipeds_data

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


@router.get("/verify")
def verify_get(token: str):
    # A GET never consumes the token — email link-scanners / prefetchers that
    # follow the link must not burn a single-use sign-in link. Bounce to the
    # SPA confirmation page, which shows a button that POSTs to consume it.
    # (Kept so old-style /api/auth/verify links still land somewhere sensible.)
    return RedirectResponse(url=f"/verify?token={token}", status_code=303)


@router.get("/verify-info")
def verify_info(token: str):
    # Non-consuming lookup so the confirmation page can name the account.
    return auth.peek_login(token)


@router.post("/verify")
def verify_post(body: VerifyRequest, response: Response):
    # Only a deliberate POST (the user clicking "Sign in") consumes the token
    # and sets the session cookie.
    return auth.verify_login(body.token, response)


@router.get("/me")
def me(user: sqlite3.Row = Depends(current_user)):
    return {"email": user["email"], "is_admin": bool(user["is_admin"]),
            "has_data": has_ipeds_data(),
            # Only the RESOLVED boolean crosses to the browser — never the raw
            # setting or any other config. Gates the chat privacy warning only.
            "trust_llm_provider": get_settings().trust_llm_provider_enabled}


@router.post("/logout")
def logout(request: Request, response: Response):
    auth.logout(request, response)
    return {"ok": True}
