#!/bin/sh
# Container entrypoint. Starts the app under uvicorn on :8000.
#
# Plain HTTP by default. If BOTH SSL_CERTFILE and SSL_KEYFILE are set (e.g. a
# self-signed cert mounted at /certs — see the README "Self-hosting" section),
# uvicorn serves HTTPS on the same port instead. With neither set, behaviour is
# identical to a bare `uvicorn app.main:app` — so the default (and the CI smoke
# test) is unchanged.
set -e

set -- app.main:app --host 0.0.0.0 --port 8000
if [ -n "${SSL_CERTFILE:-}" ] && [ -n "${SSL_KEYFILE:-}" ]; then
  set -- "$@" --ssl-certfile "$SSL_CERTFILE" --ssl-keyfile "$SSL_KEYFILE"
fi
exec uvicorn "$@"
