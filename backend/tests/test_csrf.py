"""CSRF Origin-guard contract (backend/app/csrf.py + its middleware).

Defense in depth over the SameSite=Lax session cookie: a state-changing request
carrying a foreign Origin is refused with 403 before it reaches any handler,
while origin-less requests (curl, health checks, the test client) and genuine
same-origin requests pass through. Pure-ASGI so the chat SSE stream is untouched.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"
os.environ["APP_PUBLIC_URL"] = "https://ipeds.example.edu"

from fastapi.testclient import TestClient  # noqa: E402

from app.csrf import is_state_changing, origin_allowed  # noqa: E402
from app.main import app  # noqa: E402

PUB = "https://ipeds.example.edu"
FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


# --- Pure policy (origin_allowed / is_state_changing) -------------------------

def test_absent_origin_is_allowed():
    # Non-browser / origin-less clients aren't a browser CSRF vector.
    assert origin_allowed(None, "ipeds.example.edu", PUB) is True
    assert origin_allowed("", "ipeds.example.edu", PUB) is True


def test_origin_matching_host_is_allowed():
    # Same-origin request: Origin's host matches the Host header, whatever
    # host/IP/port the deployment is actually reached on.
    assert origin_allowed("http://192.168.1.5:8000", "192.168.1.5:8000", PUB) is True
    assert origin_allowed("http://localhost:8000", "localhost:8000", PUB) is True


def test_origin_matching_public_url_is_allowed():
    # A proxy that rewrites Host is still covered by APP_PUBLIC_URL.
    assert origin_allowed("https://ipeds.example.edu", "internal-upstream", PUB) is True


def test_foreign_origin_is_refused():
    assert origin_allowed("https://evil.example.com", "ipeds.example.edu", PUB) is False
    # Even a loopback carve-out never lets a foreign origin through.
    assert origin_allowed("https://evil.example.com", "ipeds.example.edu", PUB,
                          allow_loopback=True) is False


def test_loopback_origin_only_in_dev_posture():
    # The Vite dev-proxy sends Origin=http://localhost:5173 while Host is
    # localhost:8000. In the dev posture (allow_loopback) that's accepted; in
    # production (strict) it's refused.
    assert origin_allowed("http://localhost:5173", "localhost:8000", PUB,
                          allow_loopback=True) is True
    assert origin_allowed("http://127.0.0.1:5173", "localhost:8000", PUB,
                          allow_loopback=True) is True
    assert origin_allowed("http://localhost:5173", "localhost:8000", PUB,
                          allow_loopback=False) is False


def test_malformed_or_null_origin_is_refused():
    assert origin_allowed("null", "ipeds.example.edu", PUB) is False
    assert origin_allowed("http://[", "ipeds.example.edu", PUB) is False


def test_state_changing_classification():
    for m in ("POST", "put", "Patch", "DELETE"):
        assert is_state_changing(m) is True, m
    for m in ("GET", "head", "OPTIONS", "TRACE"):
        assert is_state_changing(m) is False, m


# --- Middleware wiring (end to end through the ASGI stack) --------------------

def test_safe_method_passes():
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200


def test_state_changing_foreign_origin_blocked():
    with TestClient(app) as c:
        r = c.post("/api/health", headers={"origin": "https://evil.example.com"})
        assert r.status_code == 403, f"foreign-origin POST should be 403, got {r.status_code}"
        assert "refused" in r.text.lower(), r.text


def test_state_changing_same_origin_not_blocked():
    with TestClient(app) as c:
        # TestClient's Host is 'testserver'; a matching Origin must pass the guard
        # (it then 404/405s at routing — the point is it is NOT a 403).
        r = c.post("/api/health", headers={"origin": "http://testserver"})
        assert r.status_code != 403, f"same-origin POST wrongly blocked: {r.status_code}"


def test_state_changing_no_origin_not_blocked():
    with TestClient(app) as c:
        r = c.post("/api/health")
        assert r.status_code != 403, f"origin-less POST wrongly blocked: {r.status_code}"


def run():
    print("CSRF Origin-guard contract:")
    check("absent/empty Origin is allowed", test_absent_origin_is_allowed)
    check("Origin matching the Host header is allowed", test_origin_matching_host_is_allowed)
    check("Origin matching APP_PUBLIC_URL is allowed", test_origin_matching_public_url_is_allowed)
    check("foreign Origin is refused (even with loopback carve-out)",
          test_foreign_origin_is_refused)
    check("loopback Origin accepted only in the dev posture",
          test_loopback_origin_only_in_dev_posture)
    check("malformed/null Origin is refused", test_malformed_or_null_origin_is_refused)
    check("safe vs state-changing method classification", test_state_changing_classification)
    check("safe GET passes the middleware", test_safe_method_passes)
    check("state-changing foreign-origin request is 403",
          test_state_changing_foreign_origin_blocked)
    check("state-changing same-origin request is not blocked",
          test_state_changing_same_origin_not_blocked)
    check("state-changing origin-less request is not blocked",
          test_state_changing_no_origin_not_blocked)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL CSRF TESTS PASSED")


if __name__ == "__main__":
    run()
