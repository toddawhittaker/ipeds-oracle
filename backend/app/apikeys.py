"""Per-user API keys for the MCP endpoint.

The web app authenticates with a session cookie set by the magic-link flow
(app/auth.py). MCP clients cannot carry a cookie, so they present a static
bearer key instead. This module is the whole of that credential: minting,
verification, and revocation.

It reuses app/security.py's primitives rather than inventing new ones, so an API
key and a session token have the same strength (32 bytes from
`secrets.token_urlsafe`) and the same storage rule (only the SHA-256 hash is
ever written). A dump of app.db therefore mints neither.

The one thing to keep in step with app/auth.py: `verify` re-checks the
allowlist. `_user_from_request` drops a live session the moment its owner leaves
the allowlist, and a key that outlived that check would be a standing grant to
someone an admin believes they removed.
"""
from __future__ import annotations

import sqlite3
import time

from app.auth import is_allowlisted
from app.db import connect
from app.security import hash_token, new_token

# Prefixes the raw key so a leaked one is recognizable on sight — in a log, a
# pasted config, or a secret scanner's ruleset — rather than reading as an
# anonymous blob of base64. The scheme name is part of the string a user
# copies, so it also tells them what the value is for weeks later.
KEY_PREFIX = "ipeds_mcp_"

# How stale `last_used_at` may get before a request pays for a write. The column
# exists so a user can spot a key they forgot they had, which needs day
# resolution, not second resolution. Without this floor every MCP call would
# write to app.db purely to record that it happened.
TOUCH_INTERVAL_SECONDS = 60.0


def mint(user_id: int, label: str | None = None,
         created_by: str | None = None) -> tuple[str, sqlite3.Row]:
    """Create a key for `user_id` and return `(raw_key, row)`.

    `raw_key` is the only time the secret exists outside the caller's client;
    nothing stores it and no later read can reconstruct it. `created_by` is the
    email of the admin who minted it on someone else's behalf, and stays NULL
    when a user mints their own.
    """
    raw = KEY_PREFIX + new_token()
    now = time.time()
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO api_keys(user_id, key_hash, last4, label, created_at, "
            "created_by) VALUES (?,?,?,?,?,?)",
            (user_id, hash_token(raw), raw[-4:], label or None, now, created_by))
        con.commit()
        row = con.execute("SELECT * FROM api_keys WHERE id=?",
                          (cur.lastrowid,)).fetchone()
    finally:
        con.close()
    return raw, row


def verify(raw: str) -> sqlite3.Row | None:
    """The key's owner, or None if the key cannot be used right now.

    None covers every rejection deliberately — unknown key, revoked key, and
    owner no longer allowlisted all look identical to the caller, so a probe
    cannot tell a wrong key from a withdrawn one.
    """
    if not raw:
        return None
    con = connect()
    try:
        row = con.execute(
            "SELECT k.id AS key_id, k.revoked_at, u.id, u.email, u.is_admin "
            "FROM api_keys k JOIN users u ON u.id = k.user_id "
            "WHERE k.key_hash = ?", (hash_token(raw),)).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        # Same check, and the same reason, as auth._user_from_request: removing
        # someone from the allowlist has to end every way they can reach the
        # data, not just the browser one.
        if not is_allowlisted(con, row["email"]):
            return None
    finally:
        con.close()
    return row


def touch(key_id: int) -> None:
    """Record that `key_id` was used, at most once per TOUCH_INTERVAL_SECONDS.

    Best-effort: a lost update costs one key's "last used" precision, never a
    request, so this never raises into the caller.
    """
    now = time.time()
    con = connect()
    try:
        con.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ? "
            "AND (last_used_at IS NULL OR last_used_at < ?)",
            (now, key_id, now - TOUCH_INTERVAL_SECONDS))
        con.commit()
    except sqlite3.Error:
        pass
    finally:
        con.close()


def revoke_for_email(con: sqlite3.Connection, email: str) -> int:
    """Revoke every live key belonging to `email`. Returns how many were revoked.

    Takes an OPEN connection and does not commit, so it can join the same
    transaction that drops someone from the allowlist (app/routers/admin.py's
    `_remove_user`) rather than being a second write that can half-succeed.

    Removing someone has to end every way they can reach the data. `verify`
    already refuses a key whose owner is off the allowlist, so this is not what
    stops them today — it is what stops them TOMORROW: the allowlist row can be
    added back (a contractor returns, a removal is undone, an address is re-added
    for an unrelated reason) and every key that person ever minted would come
    back to life with it, including the leaked one that prompted the removal. The
    admin sees "sessions ended" and reasonably reads that as "access ended".
    """
    cur = con.execute(
        "UPDATE api_keys SET revoked_at = ? WHERE revoked_at IS NULL AND user_id IN "
        "(SELECT id FROM users WHERE email = ?)", (time.time(), email))
    return cur.rowcount


def active_count(user_id: int) -> int:
    """How many of `user_id`'s keys are still usable."""
    con = connect()
    try:
        return con.execute(
            "SELECT COUNT(*) FROM api_keys WHERE user_id = ? AND revoked_at IS NULL",
            (user_id,)).fetchone()[0]
    finally:
        con.close()


def list_for_user(user_id: int) -> list[dict]:
    """One user's LIVE keys, newest first. Never carries a hash or a raw key.

    Revoked keys are left out. The row itself survives — `revoke` keeps it, and
    `list_all` still shows it in the admin table — but on the owner's own page a
    revoked key is a line they can no longer do anything with, and the audit
    question it answers ("what could that withdrawn key reach?") is an
    administrator's, not theirs.

    `revoked_at` stays in the SELECT: every row it returns carries it as None,
    and the two lists then have one shape for the UI that renders both.
    """
    con = connect()
    try:
        rows = con.execute(
            "SELECT id, last4, label, created_at, created_by, last_used_at, "
            "revoked_at FROM api_keys WHERE user_id = ? AND revoked_at IS NULL "
            "ORDER BY created_at DESC", (user_id,)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def list_all() -> list[dict]:
    """Every key with its owner's email, for the admin table."""
    con = connect()
    try:
        rows = con.execute(
            "SELECT k.id, k.last4, k.label, k.created_at, k.created_by, "
            "k.last_used_at, k.revoked_at, k.user_id, u.email "
            "FROM api_keys k JOIN users u ON u.id = k.user_id "
            "ORDER BY k.created_at DESC").fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def get(key_id: int) -> sqlite3.Row | None:
    """One key row by id, or None. Used to check ownership before revoking."""
    con = connect()
    try:
        return con.execute("SELECT * FROM api_keys WHERE id = ?",
                           (key_id,)).fetchone()
    finally:
        con.close()


def set_label(key_id: int, user_id: int, label: str | None) -> sqlite3.Row | None:
    """Relabel one of `user_id`'s LIVE keys. Returns the new row, or None.

    The owner check and the live check are in the UPDATE rather than in a read
    the caller makes first, so there is no window between "this key is yours and
    usable" and the write — and one None covers both refusals, which is what
    lets the route answer 404 without saying which of the two it was.

    A revoked key is not editable for the same reason it is not listed
    (`list_for_user`): its label is part of the record an administrator reads
    later, and the owner can no longer see the row to know what they changed.
    """
    con = connect()
    try:
        cur = con.execute(
            "UPDATE api_keys SET label = ? WHERE id = ? AND user_id = ? "
            "AND revoked_at IS NULL", (label, key_id, user_id))
        con.commit()
        if cur.rowcount == 0:
            return None
        return con.execute("SELECT * FROM api_keys WHERE id = ?",
                           (key_id,)).fetchone()
    finally:
        con.close()


def revoke(key_id: int) -> bool:
    """Mark `key_id` unusable. True if this call is what revoked it.

    The row survives: an administrator asking "what did that withdrawn key have
    access to" needs it to still be there.
    """
    con = connect()
    try:
        cur = con.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? "
            "AND revoked_at IS NULL", (time.time(), key_id))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()
