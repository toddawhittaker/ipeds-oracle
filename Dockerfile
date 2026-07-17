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

# App code + the loader + the schema guide (used as the system prompt).
COPY backend/app ./backend/app
COPY scripts/ ./scripts/
COPY docs/SCHEMA.md ./docs/SCHEMA.md
# Built SPA from stage 1.
COPY --from=frontend /frontend/dist ./frontend/dist

# Data (ipeds.db, app.db, uploads) lives on a mounted volume; see compose.yaml.
# Warm the embedding model at build time so first request is fast (optional).
# RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
