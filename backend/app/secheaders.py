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
"""
from __future__ import annotations

from starlette.datastructures import MutableHeaders

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

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in SECURITY_HEADERS.items():
                    if key not in headers:
                        headers[key] = value
            await send(message)

        await self.app(scope, receive, send_wrapper)
