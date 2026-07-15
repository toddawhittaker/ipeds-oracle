"""Admin router contract (app/routers/admin.py): the import pipeline's HTTP
surface (bad extension, single-import lock conflict, a mocked success run,
job listing/detail), the allowlist approval-email failure branch, the usage
dashboard's since>until swap, skills PATCH/DELETE, and the server-logs
endpoint.

The heavy importer.run_import is mocked (a fast fake that just marks the job
row 'swapped') and threading.Thread is replaced with a synchronous stand-in so
the "background" job finishes before the request handler returns — no real
loader, mdbtools, or sleep/poll needed. Allowlist add/remove and the
oversized-upload 413 path are already covered by eval/test_backend.py and
eval/test_security.py.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@franklin.edu"
os.environ["COOKIE_SECURE"] = "false"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["RESEND_API_KEY"] = ""
# Uploads must never land in the real repo's data/uploads/ directory.
os.environ["UPLOAD_DIR"] = str(Path(tmp) / "uploads")
# This suite logs in as admin many times; keep the auth rate limiter out of
# the way so it never masks a real assertion.
os.environ["AUTH_RATE_MAX_PER_EMAIL"] = "1000"
os.environ["AUTH_RATE_MAX_PER_IP"] = "1000"

from fastapi.testclient import TestClient  # noqa: E402

from app import mailer  # noqa: E402

captured = {}
mailer.send_magic_link = lambda to, link: captured.__setitem__("link", link) or True
mailer.send_access_request = lambda *a, **k: True
mailer.send_access_approved = (
    lambda to, link: captured.__setitem__("approved_link", link) or True)

from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import admin as admin_router  # noqa: E402

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _login(c, email="admin@franklin.edu"):
    c.post("/api/auth/request", json={"email": email})
    token = captured["link"].split("token=")[1]
    assert c.post("/api/auth/verify", json={"token": token}).status_code == 200


class _SyncThread:
    """Runs the target immediately (synchronously) instead of on a real
    background thread, so a mocked run_import completes before .start()
    returns and the test can assert on the job row deterministically."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


def _run_import_success(job_id, upload_path):
    con = connect()
    try:
        con.execute("UPDATE import_jobs SET status='swapped', report=? WHERE id=?",
                    ("mock import ok", job_id))
        con.commit()
    finally:
        con.close()


def test_import_rejects_non_accdb_extension():
    with TestClient(app) as c:
        _login(c)
        r = c.post("/api/admin/import",
                   files={"file": ("notes.txt", b"hello", "text/plain")})
        assert r.status_code == 400, r.text


def test_import_conflicts_while_one_already_running():
    with TestClient(app) as c:
        _login(c)
        assert admin_router._import_lock.acquire(blocking=False)
        try:
            r = c.post("/api/admin/import",
                       files={"file": ("IPEDS202526.accdb", b"data",
                                       "application/octet-stream")})
            assert r.status_code == 409, r.text
        finally:
            admin_router._import_lock.release()


def test_import_success_creates_and_completes_a_job():
    with TestClient(app) as c:
        _login(c)
        orig_thread = admin_router.threading.Thread
        orig_run_import = admin_router.importer.run_import
        admin_router.threading.Thread = _SyncThread
        admin_router.importer.run_import = _run_import_success
        try:
            r = c.post("/api/admin/import",
                       files={"file": ("IPEDS202526.accdb", b"fake accdb bytes",
                                       "application/octet-stream")})
        finally:
            admin_router.threading.Thread = orig_thread
            admin_router.importer.run_import = orig_run_import

        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]
        assert r.json()["status"] == "pending", r.text

        jobs = c.get("/api/admin/import/jobs").json()
        assert any(j["id"] == job_id for j in jobs), jobs

        detail = c.get(f"/api/admin/import/jobs/{job_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["status"] == "swapped", detail.text


def test_import_job_not_found_404():
    with TestClient(app) as c:
        _login(c)
        r = c.get("/api/admin/import/jobs/999999")
        assert r.status_code == 404, r.text


def test_allowlist_add_approval_email_failure_is_logged_not_raised():
    with TestClient(app) as c:
        _login(c)
        orig_send = admin_router.send_access_approved

        def _boom(email, link):
            raise RuntimeError("smtp is down")
        admin_router.send_access_approved = _boom
        try:
            r = c.post("/api/admin/allowlist",
                       json={"email": "newperson@franklin.edu"})
        finally:
            admin_router.send_access_approved = orig_send
        assert r.status_code == 200, r.text
        assert r.json()["invited"] is False, r.text


def _is_admin(c, email):
    row = next((x for x in c.get("/api/admin/allowlist").json()
                if x["email"] == email), None)
    return bool(row and row["is_admin"])


def test_promote_makes_user_admin_immediately_on_live_session():
    with TestClient(app) as c:
        _login(c)
        c.post("/api/admin/allowlist", json={"email": "prof@franklin.edu"})
        # prof signs in as a normal (non-admin) user
        prof = TestClient(app)
        ptok = captured["approved_link"].split("token=")[1]
        assert prof.post("/api/auth/verify", json={"token": ptok}).status_code == 200
        assert prof.get("/api/auth/me").json()["is_admin"] is False
        assert prof.get("/api/admin/allowlist").status_code == 403  # not admin yet

        r = c.patch("/api/admin/allowlist/prof@franklin.edu", json={"is_admin": True})
        assert r.status_code == 200 and r.json()["is_admin"] is True, r.text
        # is_admin is read live, so prof's EXISTING session is now admin
        assert prof.get("/api/auth/me").json()["is_admin"] is True
        assert prof.get("/api/admin/allowlist").status_code == 200


def test_demote_admin_when_another_exists():
    with TestClient(app) as c:
        _login(c)
        c.post("/api/admin/allowlist", json={"email": "prof2@franklin.edu"})
        c.patch("/api/admin/allowlist/prof2@franklin.edu", json={"is_admin": True})
        assert _is_admin(c, "prof2@franklin.edu") is True
        r = c.patch("/api/admin/allowlist/prof2@franklin.edu", json={"is_admin": False})
        assert r.status_code == 200 and r.json()["is_admin"] is False, r.text
        assert _is_admin(c, "prof2@franklin.edu") is False


def test_patch_admin_404_for_non_allowlisted():
    with TestClient(app) as c:
        _login(c)
        r = c.patch("/api/admin/allowlist/nobody@nowhere.test", json={"is_admin": True})
        assert r.status_code == 404, r.text


def test_cannot_demote_self():
    with TestClient(app) as c:
        _login(c)  # signed in as admin@franklin.edu
        r = c.patch("/api/admin/allowlist/admin@franklin.edu", json={"is_admin": False})
        assert r.status_code == 400, r.text
        assert _is_admin(c, "admin@franklin.edu") is True  # guard left them admin


def test_usage_since_after_until_is_swapped():
    with TestClient(app) as c:
        _login(c)
        r = c.get("/api/admin/usage", params={"since": 2_000_000_000, "until": 1_000_000_000})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["since"] <= body["until"], body


def test_skills_patch_updates_fields_and_noop_with_empty_body():
    with TestClient(app) as c:
        _login(c)
        before = c.get("/api/admin/skills").json()
        skill_id = before[0]["id"]

        r = c.patch(f"/api/admin/skills/{skill_id}",
                   json={"verified": True, "notes": "reviewed by test",
                         "canonical_sql": "SELECT 1"})
        assert r.status_code == 200 and r.json()["ok"] is True, r.text

        after = next(s for s in c.get("/api/admin/skills").json() if s["id"] == skill_id)
        assert after["verified"] == 1, after
        assert after["notes"] == "reviewed by test", after
        assert after["canonical_sql"] == "SELECT 1", after

        noop = c.patch(f"/api/admin/skills/{skill_id}", json={})
        assert noop.status_code == 200 and noop.json()["ok"] is True, noop.text


def test_skills_delete_removes_the_row():
    with TestClient(app) as c:
        _login(c)
        before = c.get("/api/admin/skills").json()
        skill_id = before[0]["id"]

        r = c.delete(f"/api/admin/skills/{skill_id}")
        assert r.status_code == 200 and r.json()["ok"] is True, r.text

        after = c.get("/api/admin/skills").json()
        assert not any(s["id"] == skill_id for s in after), after


def test_server_logs_returns_records():
    with TestClient(app) as c:
        _login(c)
        r = c.get("/api/admin/logs")
        assert r.status_code == 200, r.text
        assert "records" in r.json(), r.text


def test_server_logs_with_no_handler_returns_empty():
    import app.logbuffer as logbuffer_mod
    with TestClient(app) as c:
        _login(c)
        orig_get_handler = logbuffer_mod.get_handler
        logbuffer_mod.get_handler = lambda: None
        try:
            r = c.get("/api/admin/logs")
        finally:
            logbuffer_mod.get_handler = orig_get_handler
        assert r.status_code == 200, r.text
        assert r.json() == {"records": []}, r.text


def run():
    print("admin router contract:")
    check("import rejects a non-.accdb upload", test_import_rejects_non_accdb_extension)
    check("import conflicts (409) while one is already running",
          test_import_conflicts_while_one_already_running)
    check("import success creates a job and the job appears in listing/detail",
          test_import_success_creates_and_completes_a_job)
    check("import job detail 404s for an unknown job id",
          test_import_job_not_found_404)
    check("allowlist add logs (not raises) an approval-email failure",
          test_allowlist_add_approval_email_failure_is_logged_not_raised)
    check("promote makes a user admin immediately on their live session",
          test_promote_makes_user_admin_immediately_on_live_session)
    check("demote an admin when another admin exists",
          test_demote_admin_when_another_exists)
    check("PATCH admin 404s for a non-allowlisted email",
          test_patch_admin_404_for_non_allowlisted)
    check("cannot demote yourself (prevents self-lockout + keeps an admin)",
          test_cannot_demote_self)
    check("usage dashboard swaps since/until when reversed",
          test_usage_since_after_until_is_swapped)
    check("skills PATCH updates fields; empty body is a no-op",
          test_skills_patch_updates_fields_and_noop_with_empty_body)
    check("skills DELETE removes the row", test_skills_delete_removes_the_row)
    check("server logs endpoint returns records", test_server_logs_returns_records)
    check("server logs endpoint handles no handler installed",
          test_server_logs_with_no_handler_returns_empty)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL ADMIN-ROUTER TESTS PASSED")


if __name__ == "__main__":
    run()
