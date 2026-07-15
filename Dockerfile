# --- Stage 1: build the React frontend -------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install
COPY web/ ./
RUN npm run build

# --- Stage 2: runtime ------------------------------------------------------
FROM python:3.12-slim
# mdbtools is required to read the source .accdb files during imports.
RUN apt-get update \
 && apt-get install -y --no-install-recommends mdbtools \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# Install from the pinned + hashed lockfile for reproducible builds.
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# App code + the loader + the schema guide (used as the system prompt).
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY SCHEMA.md ./SCHEMA.md
# Built SPA from stage 1.
COPY --from=web /web/dist ./web/dist

# Data (ipeds.db, app.db, uploads) lives on a mounted volume; see compose.yaml.
# Warm the embedding model at build time so first request is fast (optional).
# RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
