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
    """Client IP for per-IP rate limiting, resilient to X-Forwarded-For spoofing.

    X-Forwarded-For is a client-settable header. A trusted reverse proxy/tunnel
    APPENDS the connecting peer to it, so the genuine client is the Nth entry
    counting FROM THE RIGHT, where N = `trusted_proxy_count`. Reading the
    left-most entry (as we used to) trusts whatever the client prepended, letting
    an attacker set a random IP per request and evade the per-IP cap entirely.

    With `trusted_proxy_count == 0` (the default, and CI) we don't trust XFF at
    all and use the socket peer, so a spoofed header is inert. When there are
    fewer hops than configured (a request that didn't traverse all proxies) we
    also fall back to the socket peer rather than trusting a short chain."""
    n = get_settings().trusted_proxy_count
    if n > 0:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if len(parts) >= n:
                return parts[-n]
    return request.client.host if request.client else "unknown"


def enforce_chat_rate_limit(user_id: int) -> None:
    """Raise 429 if this user has exceeded their chat-turn budget over the rolling
    window; otherwise record the turn. Mirrors enforce_auth_rate_limit, keyed on
    the authenticated user id (not email/IP) since the chat path is already gated
    to an allowlisted user — this caps a single user's runaway loop/script from
    burning unbounded provider spend. A non-positive `chat_rate_max_per_user`
    DISABLES the limiter entirely (no table writes), the off-switch for tests and
    self-hosters who don't want a per-user cap."""
    s = get_settings()
    if s.chat_rate_max_per_user <= 0:
        return
    now = time.time()
    cutoff = now - s.chat_rate_window_seconds
    con = connect()
    try:
        # Opportunistic cleanup of rows well past any window.
        con.execute("DELETE FROM chat_request_attempts WHERE created_at < ?",
                    (cutoff - s.chat_rate_window_seconds,))
        recent = con.execute(
            "SELECT COUNT(*) FROM chat_request_attempts WHERE user_id=? AND created_at>=?",
            (user_id, cutoff)).fetchone()[0]
        if recent >= s.chat_rate_max_per_user:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many requests — please slow down and try again in a moment.")
        con.execute(
            "INSERT INTO chat_request_attempts(user_id, created_at) VALUES (?,?)",
            (user_id, now))
        con.commit()
    finally:
        con.close()


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
