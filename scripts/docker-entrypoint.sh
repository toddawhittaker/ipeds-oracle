#!/bin/sh
# Container entrypoint. Starts the app under uvicorn on :8000.
#
# Plain HTTP by default. If BOTH SSL_CERTFILE and SSL_KEYFILE are set (e.g. a
# self-signed cert mounted at /certs — see the README "Self-hosting" section),
# uvicorn serves HTTPS on the same port instead.
#
# --no-proxy-headers is LOAD-BEARING, not tidiness. uvicorn defaults
# proxy_headers=True with forwarded_allow_ips trusting 127.0.0.1, and its
# ProxyHeadersMiddleware REWRITES scope["client"] from X-Forwarded-For whenever
# the socket peer is loopback. Behind a loopback-adjacent ingress (ssh -L,
# cloudflared, or a host-network reverse proxy — all shapes the README endorses)
# that hands an attacker control of the address app/ratelimit.py:client_ip reads,
# re-opening the per-IP spoofing hole #86 closed. The app does its own
# XFF handling, honouring TRUSTED_PROXY_COUNT, so it never wants uvicorn's.
set -e

set -- app.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers
if [ -n "${SSL_CERTFILE:-}" ] && [ -n "${SSL_KEYFILE:-}" ]; then
  set -- "$@" --ssl-certfile "$SSL_CERTFILE" --ssl-keyfile "$SSL_KEYFILE"
fi
exec uvicorn "$@"
