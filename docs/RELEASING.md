# Releasing and deployment

How a build becomes a published image, and the two container defaults that read
as breakage if you forget they are deliberate. Operator-facing instructions live
in the README's **Self-hosting** section; this is the developer's half.

## The image job and the published tags

CI's **image** job builds + smoke-tests the Docker
image on every PR/`main` push (so a broken Dockerfile can't merge), but publishes
to GHCR **only on a `v*` git tag** — `:X.Y.Z` + `:X.Y` + `:latest` (metadata-action
strips the leading `v`, so the Docker tag is `0.1.0`, not `v0.1.0`). No rolling
`:edge`/`:sha` images are published (deliberate — release tags are the only
artifacts kept). Self-hosters run the published image
(`docker compose pull && docker compose up -d`, pin via `IPEDS_TAG`) — TLS is the
operator's own reverse proxy/tunnel or an optional self-signed cert
(`scripts/gen-selfsigned-cert.sh` + `SSL_CERTFILE`/`SSL_KEYFILE`, served by
`scripts/docker-entrypoint.sh`). Details in the README's **Self-hosting** section.

## Two deliberate container defaults

These read as breakage if you forget they are deliberate.

**`compose.yaml` publishes :8000 on LOOPBACK** (`BIND_ADDR`, default
`127.0.0.1`), which is a security control rather than a convenience: Docker
inserts published ports into its own iptables chain, which a host
`ufw`/`firewalld` policy does **not** filter, so `0.0.0.0` is reachable from the
network however the host firewall is set. A deployment reached by LAN address
therefore stops working after an upgrade until it sets `BIND_ADDR` explicitly.
And **the container runs as the numeric uid/gid 10001** with
`no-new-privileges` + `cap_drop: ALL`; Docker never chowns a bind mount, so
`/data` must be owned by it (`sudo chown -R 10001:10001 ./srv-data`, or override
`IPEDS_UID`/`IPEDS_GID`). `python -m app.startup_checks` runs from the
entrypoint BEFORE `exec uvicorn` and exits 1 with the exact command and the live
uid — it has to be a separate process, because `app/main.py` calls
`_install_logbuffer()` at IMPORT time and an unwritable data directory would
otherwise surface as `sqlite3.OperationalError` inside uvicorn's app import,
a traceback that never mentions ownership.
