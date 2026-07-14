"""Token + session primitives. Raw tokens go to the user (email link / cookie);
only their SHA-256 hashes are ever stored, so a DB leak can't mint sessions.
"""
from __future__ import annotations

import hashlib
import secrets
import time

from app.config import get_settings


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def magic_link_expiry() -> float:
    return time.time() + get_settings().magic_link_ttl_minutes * 60


def session_expiry() -> float:
    return time.time() + get_settings().session_ttl_days * 86400
