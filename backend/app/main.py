"""FastAPI application entry point.

Serves the JSON API and, in production, the built React app (web/dist). On
startup it initializes app.db, seeds skill exemplars, and warms the embedder.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import version
from app.auth import current_user
from app.bodylimit import BodyLimitMiddleware
from app.config import PRODUCT_NAME, ROOT, get_settings
from app.csrf import CSRFMiddleware
from app.db import init_db
from app.routers import admin, auth, chat
from app.secheaders import SecurityHeadersMiddleware

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Quiet a benign third-party WARNING: fastembed downloads its embedding model
# from the HF Hub on first use, and huggingface_hub logs "you are sending
# unauthenticated requests … set a HF_TOKEN" — harmless (downloads still work
# and the model is cached after the first fetch), but the root logbuffer handler
# would otherwise file it in the admin Logs tab and tick the log-attention badge,
# muddying a signal that should mean "something's actually wrong". A REAL HF
# failure still surfaces (ERROR), and skills._embedder logs its own warning if
# embeddings end up unavailable, so nothing actionable is lost.
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
log = logging.getLogger("ipeds.app")

# Keep recent log records in memory so admins can view them in the UI.
from app.logbuffer import install as _install_logbuffer  # noqa: E402

_install_logbuffer()

WEB_DIST = ROOT / "frontend" / "dist"


def _insecure_cookie_warning(s) -> str | None:
    """The boot-time cookie-posture check. Returns a CRITICAL message when an HTTPS
    public URL is served with an insecure session cookie, else None. COOKIE_SECURE
    False both drops the cookie's `Secure` flag (sniffable over any plain-HTTP hop)
    AND relaxes the CSRF Origin guard's loopback carve-out (csrf.py
    `allow_loopback=not cookie_secure`), so the whole posture pivots on one env var
    whose default is the unsafe one. Logged, not raised — dev (http `app_public_url`)
    and the tests are silent; a production https deployment that forgot
    `COOKIE_SECURE=true` gets a screaming CRITICAL on every boot (stderr + the admin
    Logs tab's attention badge)."""
    if s.app_public_url.strip().lower().startswith("https://") and not s.cookie_secure:
        return ("INSECURE COOKIE POSTURE: APP_PUBLIC_URL is https:// but COOKIE_SECURE "
                "is false — the session cookie is served without Secure AND the CSRF "
                "Origin guard keeps its loopback exception. Set COOKIE_SECURE=true.")
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    _warning = _insecure_cookie_warning(get_settings())
    if _warning:
        log.critical(_warning)
    init_db()
    try:
        from app.auth import purge_expired_auth_rows
        from app.db import connect
        con = connect()
        try:
            purge_expired_auth_rows(con)
            con.commit()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 -- a housekeeping sweep must never block boot
        log.warning("auth row purge skipped: %s", e)
    try:
        from app.skills import seed_from_schema_examples
        n = seed_from_schema_examples()
        if n:
            log.info("seeded %d skill exemplars", n)
    except Exception as e:  # noqa: BLE001
        log.warning("skill seeding skipped: %s", e)
    try:
        from app.skills import upgrade_seed_lessons
        n = upgrade_seed_lessons()
        if n:
            log.info("upgraded %d seed lesson(s) to the generalized headline/description shape", n)
    except Exception as e:  # noqa: BLE001
        log.warning("seed lesson upgrade skipped: %s", e)
    try:
        from app.skills import reembed_skills_if_needed
        n = reembed_skills_if_needed()
        if n:
            log.info("re-embedded %d skill(s) onto the headline+description embedding source", n)
    except Exception as e:  # noqa: BLE001
        log.warning("skill re-embed skipped: %s", e)
    log.info("IPEDS Oracle API ready (db=%s)", get_settings().ipeds_db_path)
    yield


app = FastAPI(title=PRODUCT_NAME, lifespan=lifespan)
# Three pure-ASGI layers, none of which buffers the chat SSE stream. Starlette
# builds the stack so the LAST added is OUTERMOST, so a request travels:
#   SecurityHeaders -> CSRF -> BodyLimit -> router
# BodyLimit is innermost on purpose: a cross-origin oversized POST is refused by
# CSRF having read zero bytes, and BodyLimit's own 413 still flows outward
# through SecurityHeaders and gets stamped.
#
# Pre-auth request-body cap — FastAPI parses the body before it resolves
# Depends(require_admin), so without this an unauthenticated upload is spooled to
# disk before its 401. See app/bodylimit.py.
app.add_middleware(BodyLimitMiddleware)
# Origin-based CSRF guard (defense in depth over the SameSite=Lax session
# cookie); pure-ASGI so it never buffers the chat SSE stream. See app/csrf.py.
app.add_middleware(CSRFMiddleware)
# Security response headers (CSP + anti-framing + nosniff) on EVERY response —
# added last so it's the OUTERMOST layer and stamps even the CSRF 403. See
# app/secheaders.py.
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(admin.router)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/version")
def get_version(_=Depends(current_user)):
    """Running version + whether a newer GitHub release is available. Signed-in
    only (the About dialog and Admin banner that consume it are both authed);
    the GitHub call is cached + fails open (see app/version.py)."""
    return version.version_info()


# --- Serve the built React app (production) -----------------------------------
if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    # Registered BEFORE the GET-only SPA catch-all below, for every method:
    # Starlette resolves a route by PATH first, so without a dedicated
    # any-method route here, a POST/PUT/PATCH/DELETE to an unmatched (e.g.
    # removed) /api/* endpoint would still match the GET-only catch-all's path
    # pattern and get a misleading 405 Method Not Allowed instead of 404.
    @app.api_route("/api/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def api_404(full_path: str):
        return JSONResponse({"detail": "Not found"}, status_code=404)

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # Real API routes, and the api_404 catch-all above, are matched first;
        # everything else serves the SPA shell.
        web_root = WEB_DIST.resolve()
        candidate = (WEB_DIST / full_path).resolve()
        if full_path and candidate.is_relative_to(web_root) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(WEB_DIST / "index.html")
else:
    @app.get("/")
    def dev_root():
        return {"detail": "API running. Build the frontend (web/) or run Vite dev server."}
