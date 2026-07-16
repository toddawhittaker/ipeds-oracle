"""Rate-limit contract for POST /api/auth/request (written test-first, TDD).

Spams the magic-link endpoint and asserts the limiter returns 429 once the
per-email or per-IP window is exceeded, that attempts outside the window don't
count, and that limiting is allowlist-neutral. Runs against a throwaway app.db
with tight limits set via env. EXPECTED TO FAIL until the limiter exists.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"
# Tight, deterministic limits for the test.
os.environ["AUTH_RATE_MAX_PER_EMAIL"] = "3"
os.environ["AUTH_RATE_MAX_PER_IP"] = "5"
os.environ["AUTH_RATE_WINDOW_SECONDS"] = "3600"

from fastapi.testclient import TestClient

from app import auth as auth_mod

# Never send real mail from the test.
auth_mod.send_magic_link = lambda *a, **k: True
auth_mod.send_access_request = lambda *a, **k: True

from app.db import connect, init_db
from app.main import app

init_db()  # ensure app.db schema exists before any direct DB setup below

ALLOW = "admin@example.edu"
FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _clear():
    con = connect()
    con.execute("DELETE FROM auth_request_attempts")
    con.commit()
    con.close()


def test_per_email_limit():
    _clear()
    with TestClient(app) as c:
        for i in range(3):
            r = c.post("/api/auth/request", json={"email": ALLOW})
            assert r.status_code == 200, f"req {i}: {r.status_code} {r.text}"
        r = c.post("/api/auth/request", json={"email": ALLOW})
        assert r.status_code == 429, f"4th should be 429, got {r.status_code}"


def test_per_ip_limit_distinct_emails():
    _clear()
    with TestClient(app) as c:
        # 5 distinct emails (each under the per-email cap) share one client IP.
        for i in range(5):
            r = c.post("/api/auth/request", json={"email": f"user{i}@example.edu"})
            assert r.status_code == 200, f"req {i}: {r.status_code}"
        r = c.post("/api/auth/request", json={"email": "user99@example.edu"})
        assert r.status_code == 429, \
            f"6th distinct-email req should be 429 (IP), got {r.status_code}"


def test_sliding_window_expires_old_attempts():
    _clear()
    # Pre-seed expired attempts (older than the window); they must not count.
    con = connect()
    old = time.time() - 3600 - 60
    for _ in range(3):
        con.execute(
            "INSERT INTO auth_request_attempts(email, ip, created_at) VALUES (?,?,?)",
            (ALLOW, "1.2.3.4", old))
    con.commit()
    con.close()
    with TestClient(app) as c:
        r = c.post("/api/auth/request", json={"email": ALLOW})
        assert r.status_code == 200, f"expired attempts must not count, got {r.status_code}"


def test_limit_is_allowlist_neutral():
    # A non-allowlisted email is limited too (prevents access-request spam).
    _clear()
    with TestClient(app) as c:
        for _ in range(3):
            assert c.post("/api/auth/request",
                          json={"email": "nope@example.edu"}).status_code == 200
        r = c.post("/api/auth/request", json={"email": "nope@example.edu"})
        assert r.status_code == 429, f"non-allowlisted should also be limited, got {r.status_code}"


def run():
    print("Rate-limit contract for /api/auth/request:")
    check("per-email limit returns 429 after cap", test_per_email_limit)
    check("per-IP limit returns 429 after cap (distinct emails)", test_per_ip_limit_distinct_emails)
    check("expired attempts outside window don't count", test_sliding_window_expires_old_attempts)
    check("limiting is allowlist-neutral (non-allowlisted also limited)",
          test_limit_is_allowlist_neutral)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL RATE-LIMIT TESTS PASSED")


if __name__ == "__main__":
    run()
