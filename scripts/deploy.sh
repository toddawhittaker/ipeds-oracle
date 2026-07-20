#!/usr/bin/env bash
# Pull-on-the-box deploy for the IPEDS web app.
#
# The release flow is: cut a `vX.Y.Z` git tag -> CI builds, smoke-tests, and
# publishes ghcr.io/toddawhittaker/ipeds-oracle:vX.Y.Z (and moves :latest) -> run
# this on the VPS to roll the running stack onto the new image. No inbound SSH
# from GitHub and no keys leave the box.
#
# Usage (from the repo checkout on the server, beside compose.yaml + .env):
#   scripts/deploy.sh              # pull whatever IPEDS_TAG resolves to (default :latest)
#   scripts/deploy.sh v1.2.0       # pin & deploy an exact release, persisted to .env
#
# It only touches the `app` service image; Caddy and the data volume are left
# alone. app.db and ipeds.db live on the host volume and survive the swap.
set -euo pipefail

cd "$(dirname "$0")/.."

# Prefer the modern `docker compose`, fall back to legacy `docker-compose`.
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "error: neither 'docker compose' nor 'docker-compose' is installed" >&2
  exit 1
fi

# An explicit tag argument pins the release in .env so restarts stay on it.
if [[ "${1:-}" != "" ]]; then
  TAG="$1"
  if [[ -f .env ]] && grep -q '^IPEDS_TAG=' .env; then
    sed -i "s/^IPEDS_TAG=.*/IPEDS_TAG=${TAG}/" .env
  else
    echo "IPEDS_TAG=${TAG}" >> .env
  fi
  echo "Pinned IPEDS_TAG=${TAG} in .env"
fi

echo "==> Pulling app image ..."
"${COMPOSE[@]}" pull app

echo "==> Recreating the app service ..."
"${COMPOSE[@]}" up -d app

echo "==> Waiting for /api/health ..."
for _ in $(seq 1 30); do
  if "${COMPOSE[@]}" exec -T app python -c \
      "import urllib.request,sys; sys.exit(0 if b'\"ok\":true' in urllib.request.urlopen('http://localhost:8000/api/health').read() else 1)" \
      2>/dev/null; then
    echo "==> Healthy. Pruning dangling images ..."
    docker image prune -f >/dev/null || true
    echo "Deploy complete."
    exit 0
  fi
  sleep 2
done

echo "error: app did not become healthy after deploy; recent logs:" >&2
"${COMPOSE[@]}" logs --tail 40 app >&2
exit 1
