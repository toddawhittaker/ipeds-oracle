"""Security response headers for every response (SPA, assets, and API).

Defense in depth for the client. The app renders attacker-influenceable
LLM-generated markdown on every answer; that path is safe today only because
react-markdown emits no raw HTML (no `rehype-raw`, default URL sanitizer intact).
A restrictive Content-Security-Policy is the missing second line of defense: with
`script-src 'self'` (no `'unsafe-inline'`/`'unsafe-eval'`), an injected `<script>`
or inline handler simply won't execute even if the markdown posture ever regresses.

The policy is tuned to the actual Vite build, which is fully self-contained:
 - one external module script from `/assets` → `script-src 'self'` (the built
   index.html has NO inline script).
 - React inline `style={{…}}` attributes + the bundled stylesheet →
   `style-src 'self' 'unsafe-inline'` (style injection is far lower risk; inline
   styles are unavoidable with React).
 - chart export renders `data:image/svg+xml`/`data:image/png` via `<img>` →
   `img-src 'self' data:`.
 - fetch + SSE (EventSource) hit same-origin `/api` → covered by `default-src 'self'`.
 - no CDNs/web fonts/plugins/iframes → `object-src 'none'`, `frame-ancestors 'none'`,
   `base-uri 'none'`.

Strict-Transport-Security is deliberately NOT in the always-on set above: it is
sent only when the deployment's `app_public_url` is https — the same posture
signal `main._insecure_cookie_warning` already reads
(`app_public_url.strip().lower().startswith("https://")`). The README documents
two https deployment shapes: a reverse proxy/tunnel terminating TLS in front of
the app (which itself speaks plain http on loopback), and the app terminating
TLS itself with a self-signed cert at `https://your-host:8000`. HSTS is
host-scoped but PORT-agnostic, so a blanket policy issued from that second shape
would force https onto anything ELSE the same host serves on port 80 — so this
also never sends `includeSubDomains` or `preload`, both of which widen the same
way. Plain http is also the ordinary local/dev posture (`make up`, and
`docker-entrypoint.sh` unless both SSL_CERTFILE/SSL_KEYFILE are set), where an
unconditional HSTS header would simply break the site.

`max-age=15552000` (180 days): long enough that a returning visitor's browser
still enforces https weeks after their last visit — the entire point of HSTS is
to survive a *future* stripped first request, so a short window defeats it — but
short enough that a deployment which permanently drops back to http (stops
using the proxy/tunnel, or removes its self-signed cert) ages out of every
visiting browser's enforcement within six months rather than needing every
visitor to have a fresh un-expired cache entry cleared by hand.
"""
from __future__ import annotations

from starlette.datastructures import MutableHeaders

from app.config import get_settings

# 180 days; see the module docstring for why this length and not longer/shorter.
HSTS_MAX_AGE_SECONDS = 15552000

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)

# All lower-case keys (ASGI header names are lower-cased). Applied with
# set-if-absent so a route that deliberately sets its own (e.g. a different CSP
# for a special page) is never clobbered.
SECURITY_HEADERS: dict[str, str] = {
    "content-security-policy": CONTENT_SECURITY_POLICY,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",              # legacy backstop for frame-ancestors
    "referrer-policy": "no-referrer",
}


class SecurityHeadersMiddleware:
    """Pure ASGI middleware that stamps the security headers onto every HTTP
    response. Injects on the `http.response.start` event only and passes body
    chunks through untouched, so the chat SSE stream is never buffered."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Read per request (like csrf.py's CSRFMiddleware), not once at import,
        # so a runtime posture change (or a test flipping APP_PUBLIC_URL) takes
        # effect immediately rather than needing a process restart.
        https_posture = get_settings().app_public_url.strip().lower().startswith("https://")

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in SECURITY_HEADERS.items():
                    if key not in headers:
                        headers[key] = value
                if https_posture and "strict-transport-security" not in headers:
                    headers["strict-transport-security"] = f"max-age={HSTS_MAX_AGE_SECONDS}"
            await send(message)

        await self.app(scope, receive, send_wrapper)
