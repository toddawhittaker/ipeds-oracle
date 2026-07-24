"""Contract for the pre-auth request-body cap (app/bodylimit.py).

THE REGRESSION THIS CATCHES: FastAPI parses a request body before it resolves
`Depends(require_admin)`, so an UNAUTHENTICATED POST to /api/admin/import used to
have its whole multipart body parsed and spooled to the temp dir and only THEN
receive a 401. The headline assertion below is that such a request is now a
**413, not a 401** — a 401 would prove the parser ran, which is the whole bug.

The paired control (an under-cap anonymous import still 401s) matters just as
much: it proves the middleware refused the body rather than breaking the route.

Group 2 drives the middleware directly as an ASGI app because Starlette's
TestClient delivers the entire body as a single `http.request` message, so it can
only reach the Content-Length tier — mid-stream rejection, the disconnect
handshake and the SSE-safety pass-through are only reachable by hand.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ -> `app`

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"
os.environ["APP_PUBLIC_URL"] = "http://testserver"
os.environ["COOKIE_SECURE"] = "false"
# Pinned here rather than in ci_env.sh: the config DEFAULTS are what CI should
# see, so pinning divergent values there would make the local gate lie about CI
# (the rule ci_env.sh states for CHAT_RATE_MAX_PER_USER). test_security.py:31
# does the same for MAX_UPLOAD_MB.
os.environ["MAX_REQUEST_BODY_MB"] = "1"
os.environ["MAX_UPLOAD_MB"] = "4"

import asyncio  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.bodylimit import (  # noqa: E402
    MB,
    MULTIPART_SLACK_MB,
    BodyLimitMiddleware,
    has_session_cookie,
    limit_for_scope,
)
from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.secheaders import SECURITY_HEADERS  # noqa: E402

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


# --- Group 1: pure policy (no HTTP, no ASGI) ---------------------------------

def test_ordinary_path_gets_the_default_tier():
    s = get_settings()
    assert limit_for_scope("/api/chat/stream", {}, s) == 1 * MB


def test_import_path_without_a_cookie_gets_the_default_tier():
    """The anti-scanner contract: no cookie, no gigabyte allowance."""
    s = get_settings()
    assert limit_for_scope("/api/admin/import", {}, s) == 1 * MB


def test_import_path_with_a_session_cookie_gets_the_upload_tier():
    s = get_settings()
    headers = {"cookie": f"{s.cookie_name}=abc123"}
    assert limit_for_scope("/api/admin/import", headers, s) == (4 + MULTIPART_SLACK_MB) * MB


def test_a_foreign_cookie_does_not_unlock_the_upload_tier():
    """Pins an exact NAME match — a substring check would let `ipeds_session_x`
    (or any attacker-chosen name containing ours) through."""
    s = get_settings()
    headers = {"cookie": f"other=1; {s.cookie_name}_x=2; not{s.cookie_name}=3"}
    assert limit_for_scope("/api/admin/import", headers, s) == 1 * MB


def test_cookie_presence_parsing():
    assert has_session_cookie("a=1; ipeds_session=xyz; b=2", "ipeds_session")
    assert has_session_cookie("ipeds_session=xyz", "ipeds_session")
    assert not has_session_cookie("", "ipeds_session")
    assert not has_session_cookie("ipeds_session_x=1", "ipeds_session")
    # A valueless cookie still counts as present — we check presence, not shape.
    assert has_session_cookie("ipeds_session", "ipeds_session")


def test_non_positive_setting_disables_the_limiter():
    class FakeSettings:
        max_request_body_mb = 0
        max_upload_mb = 2048
        cookie_name = "ipeds_session"
    assert limit_for_scope("/api/chat/stream", {}, FakeSettings()) == 0


# --- Group 2: direct ASGI drive (the streaming contract) ---------------------

def _scope(method="POST", path="/api/admin/import", headers=None, typ="http"):
    raw = [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    return {"type": typ, "method": method, "path": path, "headers": raw}


def _drive(scope, chunks, inner=None, declared=None):
    """Run the middleware over `chunks`, returning (sent_messages, inner_state)."""
    state = {"called": False, "body": b"", "receives": 0}

    async def default_inner(scope, receive, send):
        state["called"] = True
        while True:
            m = await receive()
            if m["type"] != "http.request":
                break
            state["body"] += m.get("body", b"")
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = []
    queue = list(chunks)

    async def receive():
        state["receives"] += 1
        if queue:
            return queue.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    mw = BodyLimitMiddleware(inner or default_inner)
    if declared is not None:
        scope["headers"] = scope["headers"] + [(b"content-length", str(declared).encode())]
    asyncio.run(mw(scope, receive, send))
    return sent, state


def _body(data, more=False):
    return {"type": "http.request", "body": data, "more_body": more}


def test_declared_oversize_never_enters_the_app():
    """The 'nothing parsed, nothing spooled' proof."""
    sent, state = _drive(_scope(), [_body(b"x" * 100)], declared=10 * MB)
    assert state["called"] is False, "inner app was entered despite an oversized Content-Length"
    assert state["receives"] == 0, "the body was read"
    assert sent[0]["status"] == 413, f"expected 413, got {sent[0]['status']}"


def test_streamed_body_is_rejected_mid_stream():
    """No Content-Length: the cap must still bite, and must stop pulling."""
    chunks = [_body(b"y" * (400 * 1024), more=True) for _ in range(10)]
    sent, state = _drive(_scope(), chunks)
    assert sent[0]["status"] == 413, f"expected 413, got {sent[0]['status']}"
    # 1 MB cap over 400 KB chunks -> the 3rd read crosses it; anything much
    # larger means we kept draining a body we had already refused.
    assert state["receives"] <= 4, f"kept reading after the limit ({state['receives']} reads)"


def test_the_apps_own_response_is_suppressed_after_an_overflow():
    async def chatty(scope, receive, send):
        while True:
            m = await receive()
            if m["type"] != "http.request":
                break
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"should never ship"})

    chunks = [_body(b"z" * (600 * 1024), more=True) for _ in range(4)]
    sent, _ = _drive(_scope(), chunks, inner=chatty)
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert len(starts) == 1, f"expected exactly one response start, got {len(starts)}"
    assert starts[0]["status"] == 413, f"the app's 200 leaked (got {starts[0]['status']})"
    assert not any(b"should never ship" in m.get("body", b"") for m in sent)


def test_an_exception_after_overflow_is_swallowed_but_a_normal_one_is_not():
    async def raiser(scope, receive, send):
        while True:
            m = await receive()
            if m["type"] != "http.request":
                break
            if not m.get("more_body"):
                break
        raise RuntimeError("boom")

    over = [_body(b"q" * (600 * 1024), more=True) for _ in range(4)]
    sent, _ = _drive(_scope(), over, inner=raiser)
    assert sent[0]["status"] == 413, "an overflow should still answer 413"

    raised = False
    try:
        _drive(_scope(), [_body(b"small")], inner=raiser)
    except RuntimeError:
        raised = True
    assert raised, "a genuine app exception under the limit must still propagate"


def test_under_limit_body_reaches_the_app_intact():
    chunks = [_body(b"a" * 100, more=True), _body(b"b" * 50, more=False)]
    sent, state = _drive(_scope(), chunks)
    assert state["called"] is True, "inner app was not reached"
    assert state["body"] == b"a" * 100 + b"b" * 50, "body was altered in transit"
    assert sent[0]["status"] == 200


def test_non_http_and_bodyless_scopes_are_never_wrapped():
    """SSE safety: these must get the ORIGINAL receive/send objects, not wrappers
    (the convention csrf.py documents — a wrapped send buffers the chat stream)."""
    seen = {}

    async def spy(scope, receive, send):
        seen["receive"] = receive
        seen["send"] = send

    async def real_receive():
        return {"type": "http.request", "body": b""}

    async def real_send(message):
        pass

    for scope in (_scope(typ="lifespan"), _scope(typ="websocket"),
                  _scope(method="GET", path="/api/chat/stream")):
        seen.clear()
        asyncio.run(BodyLimitMiddleware(spy)(scope, real_receive, real_send))
        what = f"{scope['type']}/{scope.get('method')}"
        assert seen["receive"] is real_receive, f"{what} wrapped receive"
        assert seen["send"] is real_send, f"{what} wrapped send"


# --- Group 3: end to end through the real ASGI stack -------------------------

def test_anonymous_oversized_import_is_413_not_401():
    """THE HEADLINE REGRESSION. A 401 here means the multipart parser ran and the
    body was spooled to the temp dir before auth — the bug this module exists for."""
    with TestClient(app) as c:
        r = c.post("/api/admin/import",
                   files={"files": ("big.accdb", b"\0" * (2 * MB), "application/octet-stream")})
    assert r.status_code == 413, f"expected 413, got {r.status_code} ({r.text[:120]})"


def test_anonymous_under_limit_import_is_still_401():
    """The control: the middleware refused a body, it did not break the route."""
    with TestClient(app) as c:
        r = c.post("/api/admin/import",
                   files={"files": ("small.accdb", b"\0" * 1024, "application/octet-stream")})
    assert r.status_code == 401, f"expected 401, got {r.status_code} ({r.text[:120]})"


def test_the_413_carries_the_security_headers():
    """Pins the middleware ORDER: BodyLimit is innermost, so its 413 still flows
    out through SecurityHeadersMiddleware. Reorder them and this goes red."""
    with TestClient(app) as c:
        r = c.post("/api/admin/import",
                   files={"files": ("big.accdb", b"\0" * (2 * MB), "application/octet-stream")})
    assert r.status_code == 413
    for key in SECURITY_HEADERS:
        assert key in r.headers, f"{key} missing from the 413"


def test_an_oversized_json_post_is_413():
    """The other half of the bug: every JSON endpoint buffered unbounded pre-auth."""
    with TestClient(app) as c:
        r = c.post("/api/chat/stream", json={"question": "x" * (2 * MB)})
    assert r.status_code == 413, f"expected 413, got {r.status_code}"


def test_a_small_post_still_reaches_the_router():
    with TestClient(app) as c:
        r = c.post("/api/chat/stream", json={"question": "how many nursing degrees?"})
    assert r.status_code != 413, "a small body was refused by the limiter"


def run():
    print("Pre-auth request-body cap:")
    check("ordinary path gets the default tier", test_ordinary_path_gets_the_default_tier)
    check("import path WITHOUT a cookie gets the default tier",
          test_import_path_without_a_cookie_gets_the_default_tier)
    check("import path with a session cookie gets the upload tier",
          test_import_path_with_a_session_cookie_gets_the_upload_tier)
    check("a foreign cookie does not unlock the upload tier",
          test_a_foreign_cookie_does_not_unlock_the_upload_tier)
    check("cookie presence parsing", test_cookie_presence_parsing)
    check("non-positive setting disables the limiter",
          test_non_positive_setting_disables_the_limiter)
    check("declared oversize never enters the app", test_declared_oversize_never_enters_the_app)
    check("streamed body is rejected mid-stream", test_streamed_body_is_rejected_mid_stream)
    check("the app's own response is suppressed after an overflow",
          test_the_apps_own_response_is_suppressed_after_an_overflow)
    check("an exception after overflow is swallowed, a normal one is not",
          test_an_exception_after_overflow_is_swallowed_but_a_normal_one_is_not)
    check("under-limit body reaches the app intact", test_under_limit_body_reaches_the_app_intact)
    check("non-http and bodyless scopes are never wrapped",
          test_non_http_and_bodyless_scopes_are_never_wrapped)
    check("anonymous oversized import is 413, NOT 401",
          test_anonymous_oversized_import_is_413_not_401)
    check("anonymous under-limit import is still 401",
          test_anonymous_under_limit_import_is_still_401)
    check("the 413 carries the security headers", test_the_413_carries_the_security_headers)
    check("an oversized JSON post is 413", test_an_oversized_json_post_is_413)
    check("a small post still reaches the router", test_a_small_post_still_reaches_the_router)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL BODY-LIMIT TESTS PASSED")


if __name__ == "__main__":
    run()
