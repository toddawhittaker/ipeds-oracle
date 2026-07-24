"""Security/correctness regression tests for a batch of confirmed review findings.

These are written FIRST, TDD-style, and are EXPECTED TO FAIL against current
code. Each test function encodes one contract; the implementer's fix should
turn it green. Do not weaken these to force a pass.

Runs against a throwaway app.db (same bootstrap as backend/tests/test_backend.py):
isolates app.db via APP_DB_PATH + a tempdir BEFORE importing app, sets
ADMIN_EMAILS, and patches app.mailer so magic links are captured instead of
emailed. No API key required.

Run:
    /home/todd/projects/ipeds/.venv/bin/python backend/tests/test_security.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# isolate app.db before importing settings
tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"
# Tiny import cap so the oversized-upload test can trip it with a small payload.
os.environ["MAX_UPLOAD_MB"] = "1"
# This suite logs in many times as the same few addresses; keep the per-email /
# per-IP auth rate limiter out of the way so it never masks a real assertion.
os.environ["AUTH_RATE_MAX_PER_EMAIL"] = "1000"
os.environ["AUTH_RATE_MAX_PER_IP"] = "1000"

from fastapi.testclient import TestClient  # noqa: E402

from app import mailer  # noqa: E402

captured = {}
mailer.send_magic_link = lambda to, link: captured.__setitem__("link", link) or True
mailer.send_access_request = lambda *a, **k: True

from app import skills  # noqa: E402
from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402

FAILURES: list[str] = []


def check(name: str, fn) -> None:
    """Run one contract test; print a clear ✓/✗ and never let one failure hide
    the rest — that's the whole point of a red report covering a batch."""
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        print(f"  ✗ {name}: {e}")
        FAILURES.append(name)
    except Exception as e:  # noqa: BLE001 — surface unexpected exceptions as failures too
        print(f"  ✗ {name}: unexpected {type(e).__name__}: {e}")
        FAILURES.append(name)


def _login(client: TestClient, email: str) -> None:
    """Drive the magic-link request -> verify flow for `email` on `client`.
    Leaves a valid session cookie set on `client`."""
    r = client.post("/api/auth/request", json={"email": email})
    assert r.status_code == 200, r.text
    token = captured["link"].split("token=")[1]
    v = client.post("/api/auth/verify", json={"token": token})
    assert v.status_code == 200, f"verify failed: {v.status_code} {v.text}"


# ---------------------------------------------------------------------------
# 1. CRITICAL — path traversal in the SPA catch-all (backend/app/main.py)
# ---------------------------------------------------------------------------
def test_path_traversal_blocked() -> None:
    # Escape frontend/dist up to a REAL repo-root file so a broken guard would
    # actually leak. docs/SCHEMA.md sits at ROOT (same ../../ depth the old
    # requirements.txt target used before it moved into backend/); the sentinel
    # is a phrase from its H1 that the served SPA shell never contains.
    sentinel = "schema & query guide"
    paths = [
        "/%2e%2e/%2e%2e/docs/SCHEMA.md",
        "/..%2f..%2fdocs%2fSCHEMA.md",
        "/%2e%2e%2f%2e%2e%2fdocs%2fSCHEMA.md",
    ]
    with TestClient(app) as c:
        for p in paths:
            r = c.get(p, follow_redirects=False)
            leaked = r.status_code == 200 and sentinel in r.text
            assert not leaked, (
                f"{p} returned 200 and leaked docs/SCHEMA.md contents "
                f"(body starts: {r.text[:60]!r})")


# ---------------------------------------------------------------------------
# 2. HIGH — de-authorization must revoke access
#    (backend/app/routers/admin.py remove_allowlist + backend/app/auth.py current_user)
# ---------------------------------------------------------------------------
def test_deauthorization_revokes_session_and_admin() -> None:
    with TestClient(app) as admin_c, TestClient(app) as prof_c:
        _login(admin_c, "admin@example.edu")

        # admin adds prof, granting admin too (so we can also check is_admin clears)
        r = admin_c.post("/api/admin/allowlist",
                         json={"email": "prof@example.edu", "note": "colleague",
                               "is_admin": True})
        assert r.status_code == 200, r.text

        # prof logs in via the real magic-link flow
        _login(prof_c, "prof@example.edu")
        me = prof_c.get("/api/auth/me")
        assert me.status_code == 200 and me.json().get("is_admin") is True, me.text

        # admin revokes prof's access. A user who still HOLDS admin can't be
        # removed directly (the demote-first guard) — so the removal is a demote
        # (clears is_admin) followed by the remove (kills the session).
        blocked = admin_c.delete("/api/admin/allowlist/prof@example.edu")
        assert blocked.status_code == 400, (
            f"removing a still-admin user must be refused (demote first), "
            f"got {blocked.status_code}: {blocked.text}")
        assert admin_c.patch("/api/admin/allowlist/prof@example.edu",
                             json={"is_admin": False}).status_code == 200
        d = admin_c.delete("/api/admin/allowlist/prof@example.edu")
        assert d.status_code == 200, d.text

        # prof's EXISTING session must now be rejected
        me2 = prof_c.get("/api/auth/me")
        assert me2.status_code == 401, (
            f"expected 401 for a de-authorized user's existing session, "
            f"got {me2.status_code}: {me2.text}")

    con = connect()
    try:
        row = con.execute("SELECT is_admin FROM users WHERE email=?",
                          ("prof@example.edu",)).fetchone()
    finally:
        con.close()
    assert row is not None and row["is_admin"] == 0, (
        "is_admin must be cleared once a user is removed from the allowlist "
        f"(got is_admin={row['is_admin'] if row else None!r})")


# ---------------------------------------------------------------------------
# 3. HIGH — malformed tool-call JSON must not raise (backend/app/tools/registry.py dispatch)
# ---------------------------------------------------------------------------
def test_malformed_tool_json_does_not_raise() -> None:
    from app.tools.registry import dispatch
    result = dispatch("run_sql", '{"sql": "SELECT 1')  # truncated/invalid JSON
    assert isinstance(result, str), f"dispatch must return a string, got {type(result)}"
    assert "ERROR" in result.upper(), (
        f"dispatch should signal an error string instead of raising, got: {result!r}")


# ---------------------------------------------------------------------------
# 4. HIGH — critic-emitted lessons must be admin-gated before retrieval
#    (backend/app/skills.py record_lesson_from_critic + retrieve_skills_block)
#
# Originally pinned against the now-removed 👍-feedback path
# (promote_from_message); the thumbs feature was ripped out and the critic is
# now the SOLE lesson source, so this retargets the same unverified-gate
# security invariant onto record_lesson_from_critic — a real critic-sourced
# lesson must still start unverified and be excluded from retrieval until an
# admin approves it.
# ---------------------------------------------------------------------------
_SKILL4_Q = "how many underwater basket-weaving certificates were awarded in Guam last year"
_SKILL4_SQL = "SELECT 1 AS placeholder_contract_4"
_SKILL4_HEADLINE = "Filter to the exact award, not a rollup."
_SKILL4_DESCRIPTION = "a placeholder rule for contract test 4"
_skill4_state: dict = {}


def _setup_promoted_skill() -> None:
    """Runs once, before the two check()s below, so both assertions are
    independently visible even if the first one fails."""
    skills.record_lesson_from_critic(_SKILL4_Q, _SKILL4_SQL, _SKILL4_HEADLINE, _SKILL4_DESCRIPTION)
    con = connect()
    try:
        row = con.execute(
            "SELECT id, verified FROM skills WHERE question=? AND canonical_sql=?",
            (_SKILL4_Q, _SKILL4_SQL)).fetchone()
    finally:
        con.close()
    if row is None:
        raise AssertionError("record_lesson_from_critic did not create a skill row")
    _skill4_state["id"] = row["id"]
    _skill4_state["verified"] = row["verified"]


def test_promoted_skill_starts_unverified() -> None:
    assert _skill4_state.get("verified") == 0, (
        "a critic-emitted lesson must start UNVERIFIED (verified=0) pending "
        f"admin review; got verified={_skill4_state.get('verified')!r}")


def test_unverified_skill_excluded_from_retrieval() -> None:
    if skills._embedder() is None:
        print("    ⚠ fastembed not installed — retrieval-gating assertion skipped")
        return
    _, ids = skills.retrieve_skills_block(_SKILL4_Q)
    assert _skill4_state["id"] not in ids, (
        "an unverified (verified=0) skill must not be returned by retrieve_skills_block")


def test_admin_verified_skill_becomes_retrievable() -> None:
    # Admin action: mark it verified (equivalent to PATCH /api/admin/skills/{id}).
    con = connect()
    try:
        con.execute("UPDATE skills SET verified=1 WHERE id=?", (_skill4_state["id"],))
        con.commit()
    finally:
        con.close()
    if skills._embedder() is None:
        print("    ⚠ fastembed not installed — retrieval assertion skipped")
        return
    _, ids = skills.retrieve_skills_block(_SKILL4_Q)
    assert _skill4_state["id"] in ids, (
        "a verified skill should be retrievable by retrieve_skills_block")


# ---------------------------------------------------------------------------
# 5. MEDIUM — semantic cache must not serve context-dependent turns
#    (backend/app/skills.py cache_lookup/cache_store; used in backend/app/routers/chat.py)
#
# CONTRACT PINNED HERE: the cache is only a valid shortcut for a fresh,
# first-turn question. Once a conversation has prior history, a (near-)
# duplicate question may mean something different in context ("and for
# 2023?" style follow-ups depend on what came before), so the cached answer
# from a *different* conversation must not be blindly replayed. We test this
# at the public seam (POST /api/chat/stream) rather than calling cache_lookup
# directly, since the bug is in whether chat.py consults the cache at all
# when history is non-empty — write the strongest test possible here; the
# implementer's fix should make it pass by conditioning the cache_lookup call
# in backend/app/routers/chat.py on `not history`.
# ---------------------------------------------------------------------------
_CACHE_Q = "zzz-quux distinctive cache contract question about enrollment trends"
_CACHE_SQL = "SELECT 1 AS x"
_CACHE_ANSWER_MARKER = "CACHED-ANSWER-MARKER-MUST-NOT-LEAK-INTO-FOLLOWUP"


def test_cache_not_served_when_history_present() -> None:
    if skills._embedder() is None:
        print("    ⚠ fastembed not installed — semantic cache test skipped")
        return

    with TestClient(app) as c:
        _login(c, "admin@example.edu")
        me = c.get("/api/auth/me").json()

        con = connect()
        try:
            uid = con.execute("SELECT id FROM users WHERE email=?",
                              (me["email"],)).fetchone()["id"]
        finally:
            con.close()

        # Seed a cache entry for the exact question (as if answered before).
        skills.cache_store(_CACHE_Q, _CACHE_SQL, _CACHE_ANSWER_MARKER)

        # Build an EXISTING conversation that already has prior turns.
        con = connect()
        try:
            now = time.time()
            cur = con.execute(
                "INSERT INTO conversations(user_id,title,created_at,updated_at) "
                "VALUES (?,?,?,?)", (uid, "prior convo", now, now))
            conv_id = cur.lastrowid
            con.execute(
                "INSERT INTO messages(conversation_id,role,content,created_at) "
                "VALUES (?,?,?,?)", (conv_id, "user", "some earlier unrelated question", now))
            con.execute(
                "INSERT INTO messages(conversation_id,role,content,created_at) "
                "VALUES (?,?,?,?)", (conv_id, "assistant", "some earlier unrelated answer", now))
            con.commit()
        finally:
            con.close()

        # Ask the SAME question again, but in that existing (history-bearing)
        # conversation. Without an LLM_API_KEY configured, the real
        # agent path deterministically yields a config error instead of
        # hanging or calling the network — so if the cache is (wrongly)
        # consulted, the marker answer comes back instead of that error.
        r = c.post("/api/chat/stream",
                  json={"question": _CACHE_Q, "conversation_id": conv_id})
        assert r.status_code == 200, r.text
        body = r.text
        assert _CACHE_ANSWER_MARKER not in body, (
            "a follow-up question inside an existing conversation (history "
            "present) must not be served the semantic cache's answer from a "
            "prior, unrelated conversation")


# ---------------------------------------------------------------------------
# Lower priority / TODO for the implementer's seam
# ---------------------------------------------------------------------------
def test_import_rejects_oversized_upload() -> None:
    # backend/app/routers/admin.py start_import caps uploads at settings.max_upload_mb
    # (chunked read-and-count loop → 413). MAX_UPLOAD_MB=1 is set at module top,
    # so a ~2 MB .accdb must be rejected with a 4xx, not a 200 that silently
    # truncates or a crash. The rejected upload must also leave no import lock
    # held (a later import must still be able to start).
    with TestClient(app) as c:
        _login(c, "admin@example.edu")
        oversized = b"\0" * (2 * 1024 * 1024)  # 2 MB > 1 MB cap
        r = c.post(
            "/api/admin/import",
            files={"files": ("big.accdb", oversized, "application/octet-stream")},
        )
        assert 400 <= r.status_code < 500, (
            f"oversized upload must be rejected with a 4xx, got {r.status_code}: "
            f"{r.text[:200]!r}")
        assert r.status_code == 413, (
            f"expected 413 for an over-cap upload, got {r.status_code}: {r.text[:200]!r}")
        # The failed upload must have released the single-import lock: a second
        # (also-rejected) attempt should fail on size (413), not on lock (409).
        r2 = c.post(
            "/api/admin/import",
            files={"files": ("big2.accdb", oversized, "application/octet-stream")},
        )
        assert r2.status_code == 413, (
            f"import lock leaked after a rejected upload (got {r2.status_code}: "
            f"{r2.text[:200]!r})")


def test_get_verify_does_not_consume_token() -> None:
    # A GET to the verify endpoint (as an email link-scanner / prefetcher does)
    # must NOT consume the single-use token — it only bounces to the SPA confirm
    # page. Only a deliberate POST consumes it and signs the user in. Otherwise
    # a scanner following the emailed link would burn the link before the user.
    with TestClient(app) as c:
        r = c.post("/api/auth/request", json={"email": "admin@example.edu"})
        assert r.status_code == 200, r.text
        link = captured["link"]
        # Emailed link points at the SPA confirm route, not the consuming API GET.
        assert "/verify?token=" in link and "/api/auth/verify" not in link, link
        token = link.split("token=")[1]

        # A scanner GET must redirect to the confirm page and NOT consume.
        g = c.get(f"/api/auth/verify?token={token}", follow_redirects=False)
        assert g.status_code == 303 and "/verify?token=" in g.headers["location"], (
            f"GET should bounce to the SPA confirm page, got {g.status_code} "
            f"{g.headers.get('location')!r}")

        # Token is still live: the non-consuming peek names the account…
        info = c.get(f"/api/auth/verify-info?token={token}")
        assert info.status_code == 200 and info.json()["email"] == "admin@example.edu", (
            f"verify-info should name the pending account, got {info.status_code}: {info.text}")

        # …and the deliberate POST consumes it and signs in.
        v = c.post("/api/auth/verify", json={"token": token})
        assert v.status_code == 200, f"POST verify should sign in, got {v.status_code}: {v.text}"
        assert c.get("/api/auth/me").status_code == 200, "session cookie not set by POST verify"

        # The now-consumed token must not verify a second time.
        again = c.post("/api/auth/verify", json={"token": token})
        assert again.status_code == 400, (
            f"a consumed token must be rejected, got {again.status_code}: {again.text}")


def test_signing_in_purges_dead_auth_rows_only() -> None:
    """A sign-in sweeps consumed/expired magic-link tokens and expired sessions —
    and touches nothing that's still live. Regression it catches, both ways: (a)
    nothing swept these two tables at all, so every token ever minted and every
    session ever issued accumulated in app.db forever; (b) an over-broad sweep
    that also took live rows would invalidate un-clicked sign-in links and log
    every signed-in user out."""
    from app.config import get_settings
    from app.security import hash_token
    now = time.time()
    con = connect()
    try:
        uid = con.execute("INSERT INTO users(email, created_at) VALUES (?,?) "
                          "ON CONFLICT(email) DO UPDATE SET created_at=excluded.created_at "
                          "RETURNING id", ("sweep@example.edu", now)).fetchone()["id"]
        con.executemany(
            "INSERT INTO login_tokens(token_hash, email, expires_at, used_at) VALUES (?,?,?,?)",
            [("sweep-used", "sweep@example.edu", now + 900, now - 60),   # consumed
             ("sweep-expired", "sweep@example.edu", now - 60, None),     # timed out
             ("sweep-live", "sweep@example.edu", now + 900, None)])      # emailed, unclicked
        con.executemany(
            "INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            [("sweep-sess-dead", uid, now - 7200, now - 60),
             ("sweep-sess-live", uid, now, now + 7200)])
        con.commit()
    finally:
        con.close()

    with TestClient(app) as c:
        _login(c, "admin@example.edu")

    con = connect()
    try:
        tokens = {r["token_hash"] for r in con.execute(
            "SELECT token_hash FROM login_tokens WHERE token_hash LIKE 'sweep-%'")}
        sess = {r["token_hash"] for r in con.execute(
            "SELECT token_hash FROM sessions WHERE token_hash LIKE 'sweep-sess-%'")}
    finally:
        con.close()
    assert tokens == {"sweep-live"}, f"expected only the live token to survive, got {tokens}"
    assert sess == {"sweep-sess-live"}, f"expected only the live session to survive, got {sess}"

    # The sweep must not have taken the session the sign-in just created, either.
    with TestClient(app) as c:
        _login(c, "admin@example.edu")
        assert c.get("/api/auth/me").status_code == 200, "sign-in session was swept"
        con = connect()
        try:
            assert con.execute(
                "SELECT 1 FROM sessions WHERE token_hash=?",
                (hash_token(c.cookies[get_settings().cookie_name]),)).fetchone(), \
                "live session row missing"
        finally:
            con.close()


def test_schema_family_with_quote_is_handled() -> None:
    from app.tools import schema as sch
    # A family argument containing a quote must not raise or produce a broken
    # (unterminated-string) SQL error; it should behave like "unknown family".
    result = sch.get_columns("c_a' OR '1'='1")
    assert isinstance(result, str), f"expected a string result, got {type(result)}"
    assert "traceback" not in result.lower(), f"leaked a traceback: {result!r}"


def test_resolve_tz_sanitizes_control_chars_before_logging():
    # Log injection (CodeQL py/log-injection, config.py): the /usage `tz` request
    # param is user-controlled and, when it's not a real IANA zone, gets logged.
    # A CR/LF in it must not survive into the log message and forge a second line.
    from app import config as cfg
    evil = "Fake/Zone\r\nCRITICAL forged admin line"
    # The sanitizer strips every control char (CR/LF included) to a space.
    safe = cfg._log_safe(evil)
    assert "\n" not in safe and "\r" not in safe, repr(safe)
    assert "forged admin line" in safe, "content kept; only the newline is neutralized"
    # ...and the real call path (invalid zone) degrades to the default without
    # raising and without carrying the raw newline anywhere.
    assert cfg.resolve_tz(evil).key == "America/New_York"


def test_magic_link_uses_configured_origin_not_request_host() -> None:
    # SEC-1: the sign-in link must be built from app_public_url, NEVER the
    # attacker-controllable Host header. A request-derived base (request.base_url)
    # would let an attacker make the server email a victim a genuine signed link
    # pointing at an attacker domain -> token harvest -> account takeover.
    from app.config import get_settings
    captured.pop("link", None)
    with TestClient(app) as c:
        r = c.post("/api/auth/request", json={"email": "admin@example.edu"},
                   headers={"Host": "evil.example.com"})
        assert r.status_code == 200, r.text
    link = captured.get("link", "")
    assert link, "an allowlisted request must mint a sign-in link"
    origin = get_settings().app_public_url.rstrip("/")
    assert link.startswith(origin), f"link not built from app_public_url: {link!r}"
    assert "evil.example.com" not in link, f"link followed the Host header: {link!r}"


def test_insecure_cookie_posture_is_flagged_at_boot() -> None:
    # SEC-2: an https public URL served with an insecure cookie (which also relaxes
    # the CSRF loopback carve-out) is flagged CRITICAL at boot. Logged, not raised —
    # so dev/tests aren't broken, but a real prod misconfig screams in the Logs tab.
    import types

    from app.main import _insecure_cookie_warning
    danger = _insecure_cookie_warning(types.SimpleNamespace(
        app_public_url="https://oracle.example.edu", cookie_secure=False))
    assert danger and "COOKIE_SECURE" in danger, danger
    # dev (http) and the secure prod posture (https + Secure cookie) are both silent.
    assert _insecure_cookie_warning(types.SimpleNamespace(
        app_public_url="http://localhost:8000", cookie_secure=False)) is None
    assert _insecure_cookie_warning(types.SimpleNamespace(
        app_public_url="https://oracle.example.edu", cookie_secure=True)) is None


def test_email_button_escapes_the_href_attribute() -> None:
    # SEC-4: a quote in an href must be escaped so it can't break out of the
    # attribute and inject markup into the outgoing email.
    from app.mailer import _button
    html = _button('https://x/verify?token=a"onmouseover="alert(1)', "Sign in")
    assert '"onmouseover=' not in html, "a raw quote escaped the href attribute"
    assert "&quot;onmouseover" in html or "&#34;onmouseover" in html, html


def run() -> None:
    print("Security/correctness contract tests (TDD — RED expected pre-fix)\n")

    print("1. path traversal in SPA catch-all")
    check("blocks encoded ../ escapes from frontend/dist", test_path_traversal_blocked)

    print("\n2. de-authorization revokes access")
    check("removing from allowlist kills session + clears is_admin",
         test_deauthorization_revokes_session_and_admin)

    print("\n3. malformed tool-call JSON")
    check("dispatch() returns an error string instead of raising",
         test_malformed_tool_json_does_not_raise)

    print("\n4. skill promotion admin-gating")
    check("setup: record_lesson_from_critic creates a skill row", _setup_promoted_skill)
    check("promoted skill starts verified=0", test_promoted_skill_starts_unverified)
    check("unverified skill excluded from retrieval", test_unverified_skill_excluded_from_retrieval)
    check("admin-verified skill becomes retrievable", test_admin_verified_skill_becomes_retrievable)

    print("\n5. semantic cache vs. conversation history")
    check("cache not served for a history-bearing follow-up",
         test_cache_not_served_when_history_present)

    print("\n6. import upload size cap")
    check("oversized import upload is rejected with 413 (no lock leak)",
         test_import_rejects_oversized_upload)
    check("signing in purges dead tokens/sessions, keeps live ones",
          test_signing_in_purges_dead_auth_rows_only)
    check("schema.get_columns tolerates a quote in family",
          test_schema_family_with_quote_is_handled)

    print("\n7. magic-link verify: GET is non-consuming, POST consumes")
    check("GET verify never burns the token; POST signs in once",
          test_get_verify_does_not_consume_token)

    print("\n8. log injection: user-controlled tz param is scrubbed before logging")
    check("resolve_tz sanitizes control chars before the warning log",
          test_resolve_tz_sanitizes_control_chars_before_logging)

    print("\n9. auth origin-trust hardening (SEC-1/2/4)")
    check("magic link uses app_public_url, not the request Host (SEC-1)",
          test_magic_link_uses_configured_origin_not_request_host)
    check("insecure cookie posture is flagged CRITICAL at boot (SEC-2)",
          test_insecure_cookie_posture_is_flagged_at_boot)
    check("email button escapes the href attribute (SEC-4)",
          test_email_button_escapes_the_href_attribute)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED (expected RED pre-fix): {FAILURES}")
        sys.exit(1)
    print("ALL SECURITY TESTS PASSED")


if __name__ == "__main__":
    run()
