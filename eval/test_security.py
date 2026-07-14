"""Security/correctness regression tests for a batch of confirmed review findings.

These are written FIRST, TDD-style, and are EXPECTED TO FAIL against current
code. Each test function encodes one contract; the implementer's fix should
turn it green. Do not weaken these to force a pass.

Runs against a throwaway app.db (same bootstrap as eval/test_backend.py):
isolates app.db via APP_DB_PATH + a tempdir BEFORE importing app, sets
ADMIN_EMAILS, and patches app.mailer so magic links are captured instead of
emailed. No API key required.

Run:
    /home/todd/projects/ipeds/.venv/bin/python eval/test_security.py
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
os.environ["ADMIN_EMAILS"] = "admin@franklin.edu"

from fastapi.testclient import TestClient  # noqa: E402
from app import mailer  # noqa: E402

captured = {}
mailer.send_magic_link = lambda to, link: captured.__setitem__("link", link) or True
mailer.send_access_request = lambda *a, **k: True

from app.main import app  # noqa: E402
from app import skills  # noqa: E402
from app.db import connect  # noqa: E402

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
    link = captured["link"]
    token = link.split("token=")[1]
    v = client.get(f"/api/auth/verify?token={token}", follow_redirects=False)
    assert v.status_code == 303, f"verify failed: {v.status_code} {v.text}"


# ---------------------------------------------------------------------------
# 1. CRITICAL — path traversal in the SPA catch-all (app/main.py:56-64)
# ---------------------------------------------------------------------------
def test_path_traversal_blocked() -> None:
    sentinel = "fastapi"  # a known line from requirements.txt at repo root
    paths = [
        "/%2e%2e/%2e%2e/requirements.txt",
        "/..%2f..%2frequirements.txt",
        "/%2e%2e%2f%2e%2e%2frequirements.txt",
    ]
    with TestClient(app) as c:
        for p in paths:
            r = c.get(p, follow_redirects=False)
            leaked = r.status_code == 200 and sentinel in r.text
            assert not leaked, (
                f"{p} returned 200 and leaked requirements.txt contents "
                f"(body starts: {r.text[:60]!r})")


# ---------------------------------------------------------------------------
# 2. HIGH — de-authorization must revoke access
#    (app/routers/admin.py remove_allowlist + app/auth.py current_user)
# ---------------------------------------------------------------------------
def test_deauthorization_revokes_session_and_admin() -> None:
    with TestClient(app) as admin_c, TestClient(app) as prof_c:
        _login(admin_c, "admin@franklin.edu")

        # admin adds prof, granting admin too (so we can also check is_admin clears)
        r = admin_c.post("/api/admin/allowlist",
                         json={"email": "prof@franklin.edu", "note": "colleague",
                               "is_admin": True})
        assert r.status_code == 200, r.text

        # prof logs in via the real magic-link flow
        _login(prof_c, "prof@franklin.edu")
        me = prof_c.get("/api/auth/me")
        assert me.status_code == 200 and me.json().get("is_admin") is True, me.text

        # admin revokes prof's access
        d = admin_c.delete("/api/admin/allowlist/prof@franklin.edu")
        assert d.status_code == 200, d.text

        # prof's EXISTING session must now be rejected
        me2 = prof_c.get("/api/auth/me")
        assert me2.status_code == 401, (
            f"expected 401 for a de-authorized user's existing session, "
            f"got {me2.status_code}: {me2.text}")

    con = connect()
    try:
        row = con.execute("SELECT is_admin FROM users WHERE email=?",
                          ("prof@franklin.edu",)).fetchone()
    finally:
        con.close()
    assert row is not None and row["is_admin"] == 0, (
        "is_admin must be cleared once a user is removed from the allowlist "
        f"(got is_admin={row['is_admin'] if row else None!r})")


# ---------------------------------------------------------------------------
# 3. HIGH — malformed tool-call JSON must not raise (app/tools/registry.py dispatch)
# ---------------------------------------------------------------------------
def test_malformed_tool_json_does_not_raise() -> None:
    from app.tools.registry import dispatch
    result = dispatch("run_sql", '{"sql": "SELECT 1')  # truncated/invalid JSON
    assert isinstance(result, str), f"dispatch must return a string, got {type(result)}"
    assert "ERROR" in result.upper(), (
        f"dispatch should signal an error string instead of raising, got: {result!r}")


# ---------------------------------------------------------------------------
# 4. HIGH — 👍-promoted skills must be admin-gated before retrieval
#    (app/skills.py promote_from_message + retrieve_skills_block)
# ---------------------------------------------------------------------------
_SKILL4_Q = "how many underwater basket-weaving certificates were awarded in Guam last year"
_SKILL4_SQL = "SELECT 1 AS placeholder_contract_4"
_skill4_state: dict = {}


def _setup_promoted_skill() -> None:
    """Runs once, before the two check()s below, so both assertions are
    independently visible even if the first one fails."""
    skills.promote_from_message(_SKILL4_Q, _SKILL4_SQL)
    con = connect()
    try:
        row = con.execute(
            "SELECT id, verified FROM skills WHERE question=? AND canonical_sql=?",
            (_SKILL4_Q, _SKILL4_SQL)).fetchone()
    finally:
        con.close()
    if row is None:
        raise AssertionError("promote_from_message did not create a skill row")
    _skill4_state["id"] = row["id"]
    _skill4_state["verified"] = row["verified"]


def test_promoted_skill_starts_unverified() -> None:
    assert _skill4_state.get("verified") == 0, (
        "a feedback-promoted skill must start UNVERIFIED (verified=0) pending "
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
#    (app/skills.py cache_lookup/cache_store; used in app/routers/chat.py)
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
# in app/routers/chat.py on `not history`.
# ---------------------------------------------------------------------------
_CACHE_Q = "zzz-quux distinctive cache contract question about enrollment trends"
_CACHE_SQL = "SELECT 1 AS x"
_CACHE_ANSWER_MARKER = "CACHED-ANSWER-MARKER-MUST-NOT-LEAK-INTO-FOLLOWUP"


def test_cache_not_served_when_history_present() -> None:
    if skills._embedder() is None:
        print("    ⚠ fastembed not installed — semantic cache test skipped")
        return

    with TestClient(app) as c:
        _login(c, "admin@franklin.edu")
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
        # conversation. Without an OPENROUTER_API_KEY configured, the real
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
    # TODO(implementer): app/routers/admin.py start_import has no upload size
    # cap today (it streams straight to disk via shutil.copyfileobj). There is
    # no env var to configure a cap yet, so this cannot be encoded as a clean
    # failing assertion without guessing the implementer's seam (a settings
    # field name, a Content-Length check, a chunked read-and-count loop, etc).
    # Once a cap + config knob exists (e.g. settings.max_upload_mb), add a test
    # here that POSTs a file larger than the cap to /api/admin/import (as an
    # admin) and asserts a 4xx, not a 200 that silently truncates or a crash.
    print("    ⚠ TODO: no upload-size cap/config knob exists yet to test against "
         "(see app/routers/admin.py start_import) — left as a documented gap")


def test_schema_family_with_quote_is_handled() -> None:
    from app.tools import schema as sch
    # A family argument containing a quote must not raise or produce a broken
    # (unterminated-string) SQL error; it should behave like "unknown family".
    result = sch.get_columns("c_a' OR '1'='1")
    assert isinstance(result, str), f"expected a string result, got {type(result)}"
    assert "traceback" not in result.lower(), f"leaked a traceback: {result!r}"


def run() -> None:
    print("Security/correctness contract tests (TDD — RED expected pre-fix)\n")

    print("1. path traversal in SPA catch-all")
    check("blocks encoded ../ escapes from web/dist", test_path_traversal_blocked)

    print("\n2. de-authorization revokes access")
    check("removing from allowlist kills session + clears is_admin",
         test_deauthorization_revokes_session_and_admin)

    print("\n3. malformed tool-call JSON")
    check("dispatch() returns an error string instead of raising",
         test_malformed_tool_json_does_not_raise)

    print("\n4. skill promotion admin-gating")
    check("setup: promote_from_message creates a skill row", _setup_promoted_skill)
    check("promoted skill starts verified=0", test_promoted_skill_starts_unverified)
    check("unverified skill excluded from retrieval", test_unverified_skill_excluded_from_retrieval)
    check("admin-verified skill becomes retrievable", test_admin_verified_skill_becomes_retrievable)

    print("\n5. semantic cache vs. conversation history")
    check("cache not served for a history-bearing follow-up",
         test_cache_not_served_when_history_present)

    print("\n6. lower priority / TODO")
    check("oversized import upload is rejected (TODO — no seam yet)",
         test_import_rejects_oversized_upload)
    check("schema.get_columns tolerates a quote in family", test_schema_family_with_quote_is_handled)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED (expected RED pre-fix): {FAILURES}")
        sys.exit(1)
    print("ALL SECURITY TESTS PASSED")


if __name__ == "__main__":
    run()
