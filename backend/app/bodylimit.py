"""Pre-auth cap on the size of every request body.

FastAPI parses the request body while resolving a route's parameters, which
happens BEFORE `solve_dependencies` evaluates `Depends(require_admin)`. So an
**unauthenticated** POST to `/api/admin/import` has its whole multipart body
parsed — and, past a 1 MB threshold, spooled to `tempfile.gettempdir()` by
Starlette's `UploadFile` — and only THEN receives its 401. The handler's own cap
(`routers/admin.py`, `max_upload_mb`) lives inside the handler, which never runs.
Measured: 64 MB accepted in 0.17s against a 401. In a container the temp dir is
the writable layer, so a loop fills the disk and takes the app down with it. The
same ordering means every JSON endpoint buffers an unbounded body pre-auth.

This refuses the body first, before the inner app is entered at all.

Two tiers:
 - Every request gets `max_request_body_mb` (10 MB by default) — far above any
   JSON this API accepts, far below what it takes to fill a disk.
 - `/api/admin/import` legitimately needs gigabytes, so it gets `max_upload_mb`
   — but ONLY when the request carries a session cookie.

**What the cookie condition does and does not buy.** It is presence only: no
signature check, no DB lookup, no duplication of `require_admin` (which would
couple this layer to app.db and give the codebase a second place to get
authentication right). It drops an anonymous scanner from a free 2 GB per
request to 10 MB, refused with zero bytes read when `Content-Length` is present.
It does NOT stop an attacker who adds one junk `Cookie:` header, and it does not
stop a signed-in non-admin — both still reach the large tier and are refused
later by `require_admin`, after the body has been spooled. Closing that would
require resolving the session here, which is an explicit non-goal. The
load-bearing protection is that the small tier covers every other endpoint and
the exemption is scoped to exactly one path.
"""
from __future__ import annotations

from starlette.responses import JSONResponse

from app.config import get_settings

MB = 1024 * 1024

# Methods that never legitimately carry a body. Passed through with the ORIGINAL
# receive/send so a GET SSE stream or the CSV download's StreamingResponse is
# never wrapped. Deliberately a local constant rather than csrf.SAFE_METHODS —
# that set means "does not change state", which only coincidentally matches.
BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# The app's only upload endpoint (routers/admin.py `start_import`).
IMPORT_PATH = "/api/admin/import"

# Slack over max_upload_mb for the multipart envelope (part boundaries + headers).
# This exists so the HANDLER stays the authoritative decider for a merely-over-cap
# upload: it counts decoded file bytes exactly and returns a specific 413 naming
# the limit, and `test_security.py` pins that it releases the import lock when it
# does. Without slack this middleware would pre-empt that path at essentially the
# same threshold and silently retire it — and void that test's intent. Here we
# only catch a GROSSLY oversized body.
MULTIPART_SLACK_MB = 8


def has_session_cookie(cookie_header: str, name: str) -> bool:
    """True if `name` appears as a cookie NAME in the header. Presence only — see
    the module docstring for exactly what that is worth."""
    return any(c.strip().split("=", 1)[0] == name for c in (cookie_header or "").split(";"))


def limit_for_scope(path: str, headers: dict[str, str], s) -> int:
    """Bytes this request may send. 0 means unlimited (the limiter is off)."""
    if s.max_request_body_mb <= 0:
        return 0
    if path == IMPORT_PATH and has_session_cookie(headers.get("cookie", ""), s.cookie_name):
        if s.max_upload_mb <= 0:
            return 0
        return (s.max_upload_mb + MULTIPART_SLACK_MB) * MB
    return s.max_request_body_mb * MB


def _too_large() -> JSONResponse:
    return JSONResponse({"detail": "Request body too large."}, status_code=413)


class BodyLimitMiddleware:
    """Pure ASGI middleware that refuses an oversized request body.

    Raw ASGI (not BaseHTTPMiddleware) so it never buffers a response — the chat
    SSE stream flows through untouched. Non-http scopes and bodyless methods are
    passed through with the original `receive`/`send` objects, unwrapped.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method", "").upper() in BODYLESS_METHODS:
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        limit = limit_for_scope(scope.get("path", ""), headers, get_settings())
        if limit <= 0:
            await self.app(scope, receive, send)
            return

        # Tier 1 — a declared length over the cap is refused before the inner app
        # is entered, so nothing is read and nothing is spooled.
        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await _too_large()(scope, receive, send)
                    return
            except ValueError:
                pass  # malformed header — fall through and count instead

        # Tier 2 — chunked / undeclared: count as it streams.
        state = {"seen": 0, "exceeded": False, "started": False}

        async def limited_receive():
            # Once over the limit this is terminal: we never await the real
            # receive again, so we can't block on a client that stopped sending.
            # http.disconnect is the spec's way to tell the app "no more body",
            # which Starlette surfaces as ClientDisconnect and FastAPI's body
            # reader turns into a 400 — swallowed by limited_send below.
            if state["exceeded"]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                state["seen"] += len(message.get("body", b""))
                if state["seen"] > limit:
                    state["exceeded"] = True
                    return {"type": "http.disconnect"}
            return message

        async def limited_send(message):
            # Drop whatever the app produces after an overflow (its own 400, or
            # ServerErrorMiddleware's 500-then-reraise) so our 413 is the only
            # response that reaches the transport.
            if state["exceeded"]:
                return
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if not state["exceeded"]:
                raise  # a genuine bug still surfaces normally

        # Nothing was forwarded, so http.response.start hasn't been sent and the
        # 413 is the first and only response. JSONResponse never touches receive,
        # so passing the original one through cannot deadlock (as csrf.py does).
        # `started` can only be true if a handler began streaming a response
        # before finishing the request body; no such handler exists today, and if
        # one appeared we'd stop sending rather than inject a late 413.
        if state["exceeded"] and not state["started"]:
            await _too_large()(scope, receive, send)
