"""Passwordless auth: magic-link request/verify, sessions, and the FastAPI
dependencies that gate the app. Access is restricted to a manual allowlist.
"""
from __future__ import annotations

import sqlite3
import time

from fastapi import Depends, HTTPException, Request, Response, status

from app.config import get_settings
from app.db import connect
from app.mailer import send_access_request, send_magic_link
from app.security import (hash_token, magic_link_expiry, new_token,
                          session_expiry)


def is_allowlisted(con: sqlite3.Connection, email: str) -> bool:
    return con.execute("SELECT 1 FROM allowlist WHERE email=?",
                       (email,)).fetchone() is not None


def request_login(email: str, base_url: str) -> dict:
    """Start a login. Returns a neutral message either way (never reveals whether
    an address is on the allowlist). Allowlisted → emails a link; otherwise →
    files an access request and notifies the admin."""
    email = email.strip().lower()
    con = connect()
    try:
        if is_allowlisted(con, email):
            token = new_token()
            con.execute(
                "INSERT INTO login_tokens(token_hash, email, expires_at) "
                "VALUES (?,?,?)", (hash_token(token), email, magic_link_expiry()))
            con.commit()
            link = f"{base_url.rstrip('/')}/api/auth/verify?token={token}"
            send_magic_link(email, link)
        else:
            con.execute(
                "INSERT INTO access_requests(email, created_at) VALUES (?,?)",
                (email, time.time()))
            con.commit()
            s = get_settings()
            admin_to = s.access_request_to or (s.admin_email_list[0]
                                               if s.admin_email_list else None)
            if admin_to:
                send_access_request(admin_to, email)
    finally:
        con.close()
    return {"message": "If that address is approved, a sign-in link is on its "
                       "way. Otherwise, an access request has been sent to the "
                       "administrator."}


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
