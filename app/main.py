"""FastAPI application entry point.

Serves the JSON API and, in production, the built React app (web/dist). On
startup it initializes app.db, seeds skill exemplars, and warms the embedder.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import ROOT, get_settings
from app.db import init_db
from app.routers import admin, auth, chat

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ipeds.app")

# Keep recent log records in memory so admins can view them in the UI.
from app.logbuffer import install as _install_logbuffer  # noqa: E402

_install_logbuffer()

WEB_DIST = ROOT / "web" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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
    log.info("IPEDS Query API ready (db=%s)", get_settings().ipeds_db_path)
    yield


app = FastAPI(title="IPEDS Query", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(admin.router)


@app.get("/api/health")
def health():
    return {"ok": True}


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
