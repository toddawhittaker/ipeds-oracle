"""Sliding-window rate limiting for the magic-link endpoint.

POST /api/auth/request triggers an email (to allowlisted users) or an admin
access-request notification (otherwise). Without a limit it can be abused to
email-bomb a known address or flood the admin. We cap requests per email and
per client IP over a rolling window, backed by app.db so it survives across
workers and restarts.
"""
from __future__ import annotations

import time

from fastapi import HTTPException, Request, status

from app.config import get_settings
from app.db import connect


def client_ip(request: Request) -> str:
    """Best-effort client IP. Behind Caddy/Cloudflare the real address is in
    X-Forwarded-For (left-most entry); fall back to the socket peer."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce_auth_rate_limit(email: str, ip: str) -> None:
    """Raise 429 if this email or IP has exceeded its window budget. Otherwise
    record the attempt. `email` should already be normalized (lower/stripped)."""
    s = get_settings()
    now = time.time()
    cutoff = now - s.auth_rate_window_seconds
    con = connect()
    try:
        # Opportunistic cleanup of rows well past any window.
        con.execute("DELETE FROM auth_request_attempts WHERE created_at < ?",
                    (cutoff - s.auth_rate_window_seconds,))
        by_email = con.execute(
            "SELECT COUNT(*) FROM auth_request_attempts WHERE email=? AND created_at>=?",
            (email, cutoff)).fetchone()[0]
        by_ip = con.execute(
            "SELECT COUNT(*) FROM auth_request_attempts WHERE ip=? AND created_at>=?",
            (ip, cutoff)).fetchone()[0]
        if by_email >= s.auth_rate_max_per_email or by_ip >= s.auth_rate_max_per_ip:
            # Neutral message — reveals nothing about allowlist membership.
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many sign-in requests. Please wait a few minutes and try again.")
        con.execute(
            "INSERT INTO auth_request_attempts(email, ip, created_at) VALUES (?,?,?)",
            (email, ip, now))
        con.commit()
    finally:
        con.close()
