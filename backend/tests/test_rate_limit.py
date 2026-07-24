"""Rate-limit contract for POST /api/auth/request AND POST /api/chat/stream.

Auth: spams the magic-link endpoint and asserts the limiter returns 429 once the
per-email or per-IP window is exceeded, that attempts outside the window don't
count, and that limiting is allowlist-neutral. Chat (SEC-3): asserts the per-user
chat throttle refuses a user's turns past their window budget, is independent
per-user, ignores expired rows, disables at max<=0, and is actually wired into the
stream endpoint. Runs against a throwaway app.db with tight limits set via env.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
# No dataset → the stream endpoint takes the fast "no data" path (a 200 stream),
# so the chat-throttle endpoint test never invokes the agent/LLM.
os.environ["IPEDS_DB_PATH"] = str(Path(tmp) / "missing-ipeds.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"
# Tight, deterministic limits for the test.
os.environ["AUTH_RATE_MAX_PER_EMAIL"] = "3"
os.environ["AUTH_RATE_MAX_PER_IP"] = "5"
os.environ["AUTH_RATE_WINDOW_SECONDS"] = "3600"
os.environ["CHAT_RATE_MAX_PER_USER"] = "3"
os.environ["CHAT_RATE_WINDOW_SECONDS"] = "3600"

from fastapi.testclient import TestClient

from app import auth as auth_mod

# Never send real mail; capture the magic link so the chat-endpoint test can sign in.
captured = {}
auth_mod.send_magic_link = lambda to, link: captured.__setitem__("link", link) or True
auth_mod.send_access_request = lambda *a, **k: True

from app.db import connect, init_db
from app.main import app
from app.ratelimit import enforce_chat_rate_limit

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


def _clear_chat():
    con = connect()
    con.execute("DELETE FROM chat_request_attempts")
    con.commit()
    con.close()


def _login(c, email=ALLOW):
    c.post("/api/auth/request", json={"email": email})
    token = captured["link"].split("token=")[1]
    assert c.post("/api/auth/verify", json={"token": token}).status_code == 200


# --- SEC-3: per-user chat throttle -----------------------------------------

def test_chat_limit_per_user():
    # A single user's turns are capped at chat_rate_max_per_user (3) per window;
    # the 4th raises 429. Guards the runaway-loop spend risk.
    from fastapi import HTTPException
    _clear_chat()
    for _ in range(3):
        enforce_chat_rate_limit(7)  # under the cap → records the turn
    try:
        enforce_chat_rate_limit(7)
        raise AssertionError("4th turn should have raised 429")
    except HTTPException as e:
        assert e.status_code == 429, f"expected 429, got {e.status_code}"


def test_chat_limit_is_per_user():
    # One user exhausting their budget must not throttle a different user.
    _clear_chat()
    for _ in range(3):
        enforce_chat_rate_limit(1)
    enforce_chat_rate_limit(2)  # distinct user → still allowed, no raise


def test_chat_window_expires_old_attempts():
    # Rows older than the window don't count toward the cap.
    _clear_chat()
    con = connect()
    old = time.time() - 3600 - 60
    for _ in range(5):
        con.execute("INSERT INTO chat_request_attempts(user_id, created_at) VALUES (?,?)",
                    (9, old))
    con.commit()
    con.close()
    enforce_chat_rate_limit(9)  # only expired rows present → allowed


def test_chat_limit_disabled_at_nonpositive_cap():
    # chat_rate_max_per_user <= 0 disables the limiter entirely (no raise, no rows).
    from app.config import get_settings
    _clear_chat()
    os.environ["CHAT_RATE_MAX_PER_USER"] = "0"
    get_settings.cache_clear()
    try:
        for _ in range(50):
            enforce_chat_rate_limit(3)
        con = connect()
        n = con.execute("SELECT COUNT(*) FROM chat_request_attempts").fetchone()[0]
        con.close()
        assert n == 0, f"disabled limiter must not write attempt rows, got {n}"
    finally:
        os.environ["CHAT_RATE_MAX_PER_USER"] = "3"
        get_settings.cache_clear()


def test_chat_stream_endpoint_enforces_limit():
    # The endpoint actually calls the limiter (wiring regression): the 4th
    # streamed turn from one signed-in user is refused 429, before any agent work.
    _clear_chat()
    with TestClient(app) as c:
        _login(c)
        for i in range(3):
            r = c.post("/api/chat/stream", json={"question": f"q{i}"})
            assert r.status_code == 200, f"turn {i}: {r.status_code} {r.text}"
        r = c.post("/api/chat/stream", json={"question": "q-over"})
        assert r.status_code == 429, f"4th turn should be 429, got {r.status_code}"


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


def _fake_request(xff, peer="203.0.113.9"):
    from types import SimpleNamespace
    headers = {} if xff is None else {"x-forwarded-for": xff}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=peer))


def test_client_ip_ignores_xff_without_trusted_proxy():
    # Regression: reading the left-most X-Forwarded-For entry let an attacker
    # spoof a fresh IP per request and evade the per-IP cap. With no trusted
    # proxy configured (the default) XFF must be ignored entirely.
    from app.config import get_settings
    from app.ratelimit import client_ip
    os.environ.pop("TRUSTED_PROXY_COUNT", None)
    get_settings.cache_clear()
    try:
        assert client_ip(_fake_request("9.9.9.9, 203.0.113.9")) == "203.0.113.9", \
            "XFF must not be trusted when TRUSTED_PROXY_COUNT=0"
        assert client_ip(_fake_request(None, peer="10.0.0.5")) == "10.0.0.5"
    finally:
        get_settings.cache_clear()


def test_client_ip_uses_rightmost_hop_behind_one_proxy():
    # Behind one trusted proxy (which appends the real peer as the RIGHT-most
    # hop), a spoofed left-most entry is ignored: both requests map to the same
    # real-IP bucket, so the per-IP limiter can't be split by header spoofing.
    from app.config import get_settings
    from app.ratelimit import client_ip
    os.environ["TRUSTED_PROXY_COUNT"] = "1"
    get_settings.cache_clear()
    try:
        assert client_ip(_fake_request("9.9.9.9, 198.51.100.7")) == "198.51.100.7"
        assert client_ip(_fake_request("1.1.1.1, 198.51.100.7")) == "198.51.100.7"
        # No XFF at all still falls back to the socket peer.
        assert client_ip(_fake_request(None, peer="10.0.0.5")) == "10.0.0.5"
    finally:
        os.environ.pop("TRUSTED_PROXY_COUNT", None)
        get_settings.cache_clear()


def run():
    print("Rate-limit contract for /api/auth/request:")
    check("client_ip ignores spoofable XFF without a trusted proxy",
          test_client_ip_ignores_xff_without_trusted_proxy)
    check("client_ip uses the right-most hop behind one trusted proxy",
          test_client_ip_uses_rightmost_hop_behind_one_proxy)
    check("per-email limit returns 429 after cap", test_per_email_limit)
    check("per-IP limit returns 429 after cap (distinct emails)", test_per_ip_limit_distinct_emails)
    check("expired attempts outside window don't count", test_sliding_window_expires_old_attempts)
    check("limiting is allowlist-neutral (non-allowlisted also limited)",
          test_limit_is_allowlist_neutral)
    check("chat throttle caps a user's turns per window (429)", test_chat_limit_per_user)
    check("chat throttle is independent per user", test_chat_limit_is_per_user)
    check("chat throttle ignores expired rows", test_chat_window_expires_old_attempts)
    check("chat throttle disabled at max<=0", test_chat_limit_disabled_at_nonpositive_cap)
    check("chat stream endpoint enforces the throttle (429)",
          test_chat_stream_endpoint_enforces_limit)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL RATE-LIMIT TESTS PASSED")


if __name__ == "__main__":
    run()
