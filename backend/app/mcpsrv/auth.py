"""The bearer-key gate in front of the MCP endpoint.

Written as an ASGI callable that wraps the transport app rather than as a
path-scoped middleware on the parent FastAPI app. Wrapping is explicit about
what it protects and cannot be bypassed by a route added later; a middleware
that checks `scope["path"] == "/mcp"` is one refactor away from guarding
nothing.

THE ONE THING NOT TO "FIX" HERE: a rejection carries no `WWW-Authenticate`
header and the app serves no `/.well-known/oauth-protected-resource`. That is
not an oversight. This deployment issues static keys (app/apikeys.py) and runs
no authorization server, and clients that see OAuth resource metadata advertised
have been reported to abandon a perfectly good configured header and go hunting
for a login flow that does not exist. Passing `auth=`/`token_verifier=` to the
SDK's `streamable_http_app()` is what would bring both back — see
`mcp/server/lowlevel/server.py`, where those routes and that header are added
only when they are set.
"""
from __future__ import annotations

import contextvars
import typing

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app import apikeys, ratelimit

_BEARER = "bearer "


class Caller(typing.NamedTuple):
    """Who is behind the request the gate just admitted.

    Only what a handler downstream actually needs, rather than the `sqlite3.Row`
    `apikeys.verify` returns, whose `id`/`key_id` pair is one mis-read away from
    charging the wrong thing to the wrong person.
    """
    user_id: int
    email: str
    key_id: int


# The admitted caller, for the duration of one request.
#
# A tool handler needs to know WHO is calling — `ask` charges a user's chat
# budget, reads that user's answer cache, and bills that user's spend — and the
# MCP tier hands handlers no route to the HTTP request. `ctx.transport.headers`
# looks like the way and is NOT: it is None on both protocol legs here (the
# session manager builds its TransportContext without them), so a handler
# reading it would silently see nobody.
#
# Measured before relying on it, twice, because the failure would be silent and
# would cross users: the value set here IS visible in the handler on both legs,
# and two overlapping requests on two different keys each saw their own caller
# either side of an interleaving await. That isolation is not a coincidence —
# the handler runs in a task descended from this one, which is the same reason
# the value propagates at all.
#
# Read it with `current_caller()`, and treat None as a refusal, never as a
# default: an unset caller means the plumbing changed, and the safe answer to
# "whose budget is this?" is to not answer at all.
_CALLER: contextvars.ContextVar[Caller | None] = contextvars.ContextVar(
    "mcp_caller", default=None)


def current_caller() -> Caller | None:
    """The caller `RequireApiKey` admitted for this request, or None."""
    return _CALLER.get()


def bearer_token(headers: list[tuple[bytes, bytes]]) -> str:
    """The raw key from an `Authorization: Bearer …` header, or "" if absent.

    The scheme name is matched case-insensitively (RFC 7235 says it is), which
    is the difference between working with a client that sends `bearer` and
    failing one for a reason nobody can see from either end.
    """
    for name, value in headers:
        if name.lower() == b"authorization":
            raw = value.decode("latin-1")
            if raw[:len(_BEARER)].lower() == _BEARER:
                return raw[len(_BEARER):].strip()
            return ""
    return ""


def admit(raw: str) -> tuple[Caller | None, tuple[int, str] | None]:
    """The whole gate, as blocking code: `(caller, None)` to admit, else
    `(None, (status, detail))`.

    Three app.db round trips — verify, rate limit, touch — all of them blocking
    `sqlite3`, which is why this is a plain function that its caller hands to a
    worker thread rather than an `async def` that would run them on the event
    loop and stall every live chat stream in the process while it waited on a
    lock.

    It RETURNS the caller rather than setting the context variable itself, which
    it cannot: `run_in_threadpool` copies the context into the worker thread, so
    a `set()` in here would be discarded on the way back out.
    """
    key = apikeys.verify(raw)
    if key is None:
        return None, (401, "A valid API key is required.")
    try:
        ratelimit.enforce_mcp_rate_limit(key["key_id"])
    except HTTPException as e:
        return None, (e.status_code, e.detail)
    # Best-effort and at most once a minute (apikeys.TOUCH_INTERVAL_SECONDS), so
    # "last used" costs a write on the odd request rather than on all of them.
    # Never fails a call.
    apikeys.touch(key["key_id"])
    return Caller(user_id=int(key["id"]), email=key["email"],
                  key_id=int(key["key_id"])), None


class RequireApiKey:
    """ASGI wrapper: verify the bearer key, charge the rate limit, then delegate.

    A rejection is a bare JSON body with the status and nothing else. Unknown
    key, revoked key, and de-allowlisted owner are already indistinguishable
    inside `apikeys.verify`, and they stay that way here, so probing tells an
    attacker nothing about which keys exist.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        caller, refusal = await run_in_threadpool(
            admit, bearer_token(scope.get("headers", [])))
        if refusal is not None:
            status_code, detail = refusal
            await JSONResponse({"detail": detail},
                               status_code=status_code)(scope, receive, send)
            return
        _CALLER.set(caller)
        await self.app(scope, receive, send)
