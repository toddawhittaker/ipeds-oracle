"""Auth routes: request a magic link, verify it, whoami, logout."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr

from app import auth
from app.auth import current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr


@router.post("/request")
def request_link(body: LoginRequest, request: Request):
    base = str(request.base_url)
    return auth.request_login(str(body.email), base)


@router.get("/verify")
def verify(token: str):
    # Set the cookie on a redirect to the app root.
    resp = RedirectResponse(url="/", status_code=303)
    try:
        auth.verify_login(token, resp)
    except Exception:
        return RedirectResponse(url="/?error=invalid_link", status_code=303)
    return resp


@router.get("/me")
def me(user: sqlite3.Row = Depends(current_user)):
    return {"email": user["email"], "is_admin": bool(user["is_admin"])}


@router.post("/logout")
def logout(request: Request, response: Response):
    auth.logout(request, response)
    return {"ok": True}
