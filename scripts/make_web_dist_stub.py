#!/usr/bin/env python3
"""Create a minimal stub `frontend/dist` so the SPA-serving block in
backend/app/main.py is active during backend tests even when the real frontend
hasn't been built (e.g. the CI backend job, which never runs `npm run build`).

Why it matters: backend/app/main.py mounts the SPA + the path-traversal-guarded
catch-all only `if WEB_DIST.exists()` at import time. Without a dist, that whole
block — including the security-critical traversal guard that
backend/tests/test_security.py is meant to exercise — is never loaded, so the
test passes trivially (bare 404) and the code shows as uncovered.

Idempotent and non-destructive: if a real (or prior stub) build already exists,
it does nothing, so a local dev's real `frontend/dist` is never clobbered.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "frontend" / "dist"


def main() -> None:
    index = DIST / "index.html"
    if index.exists():
        return  # real or prior build present — leave it alone
    (DIST / "assets").mkdir(parents=True, exist_ok=True)
    # No SCHEMA.md/secret-like tokens here: test_security asserts a traversal
    # never serves 200 + a docs/SCHEMA.md sentinel, and this shell is served.
    index.write_text("<!doctype html><title>IPEDS Query (stub)</title>\n")
    (DIST / "assets" / "stub.js").write_text("/* stub asset for backend tests */\n")
    print(f"Created stub frontend/dist at {DIST}")


if __name__ == "__main__":
    main()
