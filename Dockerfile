# --- Stage 1: build the React frontend -------------------------------------
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# --- Stage 2: runtime ------------------------------------------------------
FROM python:3.12-slim
# mdbtools is required to read the source .accdb files during imports.
RUN apt-get update \
 && apt-get install -y --no-install-recommends mdbtools \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
# PYTHONPATH puts backend/ on sys.path so `uvicorn app.main:app` resolves; and
# config.py (at /srv/backend/app/) anchors ROOT = parents[2] = /srv, so
# ROOT/docs/SCHEMA.md, ROOT/frontend/dist and ROOT/scripts all line up below.
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/srv/backend

# Install from the pinned + hashed lockfile for reproducible builds.
COPY backend/requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# Bake the local embedding model (fastembed → HF Hub) into an EARLY layer, ABOVE
# the app-code COPYs, so its build-cache key depends only on the lockfile — a
# code change doesn't re-download the ~65 MB model, and CI's `type=gha,mode=max`
# layer cache reuses it across builds. FASTEMBED_CACHE_PATH is honored by
# fastembed at BOTH build (this RUN) and runtime (the ENV persists into the
# container), so the deployed app loads the baked model instead of fetching it —
# no first-request download latency and no "unauthenticated HF Hub" warning.
# Must match config.embed_model's default; a self-hoster who overrides
# EMBED_MODEL just downloads that model on first use, as before.
ENV FASTEMBED_CACHE_PATH=/srv/models
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

# App code + the loader + the schema guide (used as the system prompt).
COPY backend/app ./backend/app
COPY scripts/ ./scripts/
COPY docs/SCHEMA.md ./docs/SCHEMA.md
# Built SPA from stage 1.
COPY --from=frontend /frontend/dist ./frontend/dist

# Data (ipeds.db, app.db, uploads) lives on a mounted volume; see compose.yaml.

RUN chmod +x scripts/docker-entrypoint.sh

EXPOSE 8000
# The entrypoint serves plain HTTP on :8000, or HTTPS when SSL_CERTFILE and
# SSL_KEYFILE are set (a self-signed cert — see the README). With neither set it's
# a bare `uvicorn app.main:app`, so the default is unchanged.
CMD ["/srv/scripts/docker-entrypoint.sh"]
