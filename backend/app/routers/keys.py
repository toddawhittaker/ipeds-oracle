"""A user's own API keys: list, mint, revoke.

Everything here is scoped to the caller. The admin equivalents — seeing all
keys, minting one for somebody else — live in app/routers/admin.py behind
require_admin, so that no route in this file needs to reason about privilege.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import apikeys
from app.auth import current_user

router = APIRouter(prefix="/api/keys", tags=["keys"],
                   dependencies=[Depends(current_user)])

# Long enough for "MacBook, Claude Code, work laptop"; short enough that the
# admin table stays readable. A module constant rather than a setting: it is an
# interface decision, not something a deployment tunes (and so ci_env.sh needs
# no entry — see docs/TESTING.md's test-env-bleed note).
MAX_LABEL_LEN = 80


class KeyCreate(BaseModel):
    label: str | None = Field(default=None, max_length=MAX_LABEL_LEN)


@router.get("")
def list_keys(user: sqlite3.Row = Depends(current_user)):
    """The caller's keys. Carries `last4` for identification, never a secret."""
    return apikeys.list_for_user(int(user["id"]))


@router.post("")
def create_key(body: KeyCreate, user: sqlite3.Row = Depends(current_user)):
    """Mint a key for the caller.

    The `key` field in this response is the only time the raw value exists
    outside the client — nothing stores it, and no later request can return it.
    The UI has to show it once and say so.
    """
    label = (body.label or "").strip() or None
    raw, row = apikeys.mint(int(user["id"]), label=label)
    return {"key": raw, "id": row["id"], "last4": row["last4"],
            "label": row["label"], "created_at": row["created_at"]}


@router.delete("/{key_id}")
def revoke_key(key_id: int, user: sqlite3.Row = Depends(current_user)):
    """Revoke one of the caller's keys.

    A key belonging to someone else answers 404, not 403: a caller who is not
    the owner has no business learning that the id exists.
    """
    row = apikeys.get(key_id)
    if row is None or int(row["user_id"]) != int(user["id"]):
        raise HTTPException(404, "Key not found.")
    apikeys.revoke(key_id)
    return {"ok": True}
