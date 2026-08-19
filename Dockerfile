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

# Dedicated non-root account the app runs as at runtime (see the final USER
# line). Fixed NUMERIC uid/gid 10001, chosen deliberately: not 65534/nobody
# (a shared identity, and NFS root-squash maps root -> nobody, so files owned
# by 65534 are ambiguous — did the app write them, or root-squash?) and not
# 1000 (the image default some other tool/first host user already claims;
# 1000 stays available as an override via IPEDS_UID in compose.yaml). `/srv`
# itself stays root-owned and read-only to the app — only `/srv/models`
# (below) is handed to this account, pre-created so the fastembed warm-up can
# write directly into it AS uid 10001 instead of a `chown -R` after the fact,
# which would duplicate the ~90 MB model cache into a whole new layer.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --home-dir /home/app \
    --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /srv/models \
 && chown app:app /srv/models

# Bake the local embedding model (fastembed → HF Hub) into an EARLY layer, ABOVE
# the app-code COPYs, so its build-cache key depends only on the lockfile — a
# code change doesn't re-download the ~65 MB model, and CI's `type=gha,mode=max`
# layer cache reuses it across builds. FASTEMBED_CACHE_PATH is honored by
# fastembed at BOTH build (this RUN) and runtime (the ENV persists into the
# container), so the deployed app loads the baked model instead of fetching it —
# no first-request download latency and no "unauthenticated HF Hub" warning.
# Must match config.embed_model's default; a self-hoster who overrides
# EMBED_MODEL just downloads that model on first use, as before.
#
# Run AS the app user (USER app, below) so /srv/models ends up owned by the
# same uid that reads it at runtime — checked, not assumed: huggingface_hub's
# cache-hit path still does an unconditional `mkdir(parents=True,
# exist_ok=True)` on the blob/pointer/lock directories on every call (even
# when nothing new is downloaded), which needs those directories to already
# exist and be traversable by this uid. Baking them as their eventual owner
# is what makes that safe without a runtime-writable /srv/models.
# `huggingface_hub`'s xet transport writes a separate chunk cache under
# $HF_HOME (= $HOME/.cache/huggingface/xet by default), NOT under
# FASTEMBED_CACHE_PATH, so it has to be cleaned explicitly — and in this SAME
# RUN, since a later `rm` only adds a new layer on top and does not shrink
# the image.
ENV FASTEMBED_CACHE_PATH=/srv/models
USER app
ENV HOME=/home/app
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')" \
 && rm -rf "$HOME/.cache/huggingface/xet"
USER root

# App code + the loader + the two data guides. SCHEMA.md is the system prompt's
# body; both are served to MCP clients as resources (backend/app/mcpsrv/
# resources.py), so leaving DATASET.md out works locally and 404s in every
# container build.
COPY backend/app ./backend/app
COPY scripts/ ./scripts/
COPY docs/SCHEMA.md ./docs/SCHEMA.md
COPY docs/DATASET.md ./docs/DATASET.md
# Built SPA from stage 1.
COPY --from=frontend /frontend/dist ./frontend/dist

# Data (ipeds.db, app.db, uploads) lives on a mounted volume; see compose.yaml.
# Nothing at runtime needs it inside the image itself — /srv stays root-owned
# and read-only to the app.

RUN chmod +x scripts/docker-entrypoint.sh

# The running release version, surfaced in-app (About dialog / Admin update
# banner) and used for the "newer release?" check. CI passes the git tag here on
# a v* release build (build-args APP_VERSION=<X.Y.Z>); a plain `docker build`
# leaves it "dev". Placed LAST so bumping it rebuilds only this tiny final layer,
# never the pip/model layers above.
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

EXPOSE 8000
# Numeric, not `USER app` — a `runAsNonRoot`/`runAsUser` admission check (or
# any SBOM/policy scanner) can only verify a bare uid, and this survives a
# `user:` override in compose.yaml cleanly either way. Nothing at runtime
# needs root: port 8000 is above 1024, apt-get/chmod above were build-time,
# and TLS certs (if used) are generated on the host and mounted read-only.
USER 10001:10001
# The entrypoint serves plain HTTP on :8000, or HTTPS when SSL_CERTFILE and
# SSL_KEYFILE are set (a self-signed cert — see the README). With neither set it's
# a bare `uvicorn app.main:app`, so the default is unchanged.
CMD ["/srv/scripts/docker-entrypoint.sh"]
