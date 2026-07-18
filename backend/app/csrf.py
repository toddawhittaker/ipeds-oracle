"""Origin-based CSRF defense for state-changing requests.

The session cookie is `SameSite=Lax`, which already blocks cross-site form/`fetch`
POSTs on conforming modern browsers. This adds a second, independent layer: for
every state-changing request that carries an `Origin` header, we require that
origin to match where the app actually lives — so a forged cross-site request
(whose `Origin` is the attacker's site) is rejected even if the SameSite barrier
were ever weakened or absent.

Kept deliberately config-light so it can't lock out a legitimate deployment:
 - A request with NO `Origin` (non-browser clients: curl, health checks, the
   test client; and pre-2020 browsers that omit it) is allowed — those aren't a
   browser-driven CSRF vector, and SameSite still covers real browsers.
 - A present `Origin` must match the request's own `Host` header (the normal
   same-origin case, robust to whatever host/IP/port the deployment is reached
   on) OR the configured `APP_PUBLIC_URL` (covers a proxy that rewrites `Host`).
 - Anything else — a foreign origin, or a malformed/`null` origin — is refused.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from starlette.responses import JSONResponse

from app.config import get_settings

# Methods that never change state; no CSRF check needed.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Loopback hosts accepted as an Origin ONLY in the dev posture (see
# `allow_loopback` below) — the Vite dev server proxies /api with
# `changeOrigin: true`, so the backend sees Origin=http://localhost:5173 but
# Host=localhost:8000. This carve-out never applies in production.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _netloc(url: str) -> str:
    """host[:port], lowercased, from a URL or Origin string ('' if unparseable)."""
    try:
        return urlsplit(url).netloc.lower()
    except ValueError:
        return ""


def origin_allowed(origin: str | None, host: str | None, public_url: str,
                   allow_loopback: bool = False) -> bool:
    """True if a state-changing request bearing `origin` should be allowed.

    `origin` is the request's Origin header (may be None/absent), `host` its Host
    header, `public_url` the configured APP_PUBLIC_URL. Absent origin → allowed
    (see module docstring); present origin must match the Host or the public URL.
    `allow_loopback` (dev posture only — insecure cookies) additionally accepts a
    loopback origin so the Vite dev-proxy works; it's never set in production.
    """
    if not origin:
        return True  # non-browser / origin-less request; SameSite covers browsers
    src = _netloc(origin)
    if not src:
        return False  # malformed or "null" origin — never a legitimate same-origin call
    allowed = {n for n in ((host or "").lower(), _netloc(public_url)) if n}
    if src in allowed:
        return True
    if allow_loopback:
        try:
            hostname = (urlsplit(origin).hostname or "").lower()
        except ValueError:
            return False
        return hostname in LOOPBACK_HOSTS
    return False


def is_state_changing(method: str) -> bool:
    return method.upper() not in SAFE_METHODS


class CSRFMiddleware:
    """Pure ASGI middleware enforcing `origin_allowed` on state-changing requests.

    Written as raw ASGI (not BaseHTTPMiddleware) so it never wraps or buffers the
    response — the chat endpoint's SSE stream flows through untouched. On a
    refused request it short-circuits with a 403 and never calls the inner app.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and is_state_changing(scope["method"]):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            s = get_settings()
            # Loopback origins are accepted only in the dev posture (insecure
            # cookies) so the Vite dev-proxy works; production (Secure cookies)
            # enforces strict same-origin.
            if not origin_allowed(headers.get("origin"), headers.get("host"),
                                  s.app_public_url, allow_loopback=not s.cookie_secure):
                await JSONResponse({"detail": "Cross-origin request refused."},
                                   status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
