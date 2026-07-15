"""Auth routes: request a magic link, verify it, whoami, logout."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr

from app import auth
from app.auth import current_user
from app.ratelimit import client_ip, enforce_auth_rate_limit

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr


class VerifyRequest(BaseModel):
    token: str


@router.post("/request")
def request_link(body: LoginRequest, request: Request):
    email = str(body.email).strip().lower()
    enforce_auth_rate_limit(email, client_ip(request))
    base = str(request.base_url)
    return auth.request_login(email, base)


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
    return {"email": user["email"], "is_admin": bool(user["is_admin"])}


@router.post("/logout")
def logout(request: Request, response: Response):
    auth.logout(request, response)
    return {"ok": True}
