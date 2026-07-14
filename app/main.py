"""FastAPI application entry point.

Serves the JSON API and, in production, the built React app (web/dist). On
startup it initializes app.db, seeds skill exemplars, and warms the embedder.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import ROOT, get_settings
from app.db import init_db
from app.routers import admin, auth, chat

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ipeds.app")

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

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # API routes are handled above; everything else serves the SPA shell.
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        web_root = WEB_DIST.resolve()
        candidate = (WEB_DIST / full_path).resolve()
        if full_path and candidate.is_relative_to(web_root) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(WEB_DIST / "index.html")
else:
    @app.get("/")
    def dev_root():
        return {"detail": "API running. Build the frontend (web/) or run Vite dev server."}
