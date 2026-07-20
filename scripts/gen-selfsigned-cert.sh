#!/bin/sh
# Generate a self-signed TLS cert so the app can serve HTTPS directly, without a
# public domain or CA — handy on a LAN or a simple self-host. Browsers warn until
# you trust the cert (or its CA). For anything public, terminate TLS at a reverse
# proxy or tunnel instead.
#
#   scripts/gen-selfsigned-cert.sh [output-dir] [hostname]
#   defaults:                       ./certs      localhost
#
# Then point the app at the files with SSL_CERTFILE / SSL_KEYFILE (see the README
# "Self-hosting" section) and set COOKIE_SECURE=true.
set -e

DIR="${1:-certs}"
HOST="${2:-localhost}"

mkdir -p "$DIR"
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout "$DIR/key.pem" -out "$DIR/cert.pem" \
  -subj "/CN=$HOST" \
  -addext "subjectAltName=DNS:$HOST,DNS:localhost,IP:127.0.0.1"
chmod 600 "$DIR/key.pem"

echo "Wrote $DIR/cert.pem and $DIR/key.pem (CN=$HOST)."
echo "Set SSL_CERTFILE=$DIR/cert.pem and SSL_KEYFILE=$DIR/key.pem (see the README)."
