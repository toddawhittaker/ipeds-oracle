"""Admin router contract (backend/app/routers/admin.py): the import pipeline's HTTP
surface (bad extension, single-import lock conflict, a mocked success run,
job listing/detail), the allowlist approval-email failure branch, the usage
dashboard's since>until swap, skills GET/PATCH/DELETE (incl. the `headline`
field and PATCH re-embedding when headline/lesson change), and the
server-logs endpoint.

The heavy importer.run_import is mocked (a fast fake that just marks the job
row 'swapped') and threading.Thread is replaced with a synchronous stand-in so
the "background" job finishes before the request handler returns — no real
loader, mdbtools, or sleep/poll needed. Allowlist add/remove and the
oversized-upload 413 path are already covered by backend/tests/test_backend.py and
backend/tests/test_security.py.
"""
import contextlib
import hashlib
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"
os.environ["COOKIE_SECURE"] = "false"
os.environ["LLM_API_KEY"] = ""
os.environ["RESEND_API_KEY"] = ""
# Uploads must never land in the real repo's data/uploads/ directory.
os.environ["UPLOAD_DIR"] = str(Path(tmp) / "uploads")
# This suite logs in as admin many times; keep the auth rate limiter out of
# the way so it never masks a real assertion.
os.environ["AUTH_RATE_MAX_PER_EMAIL"] = "1000"
os.environ["AUTH_RATE_MAX_PER_IP"] = "1000"

import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import mailer  # noqa: E402

captured = {}
mailer.send_magic_link = lambda to, link: captured.__setitem__("link", link) or True
mailer.send_access_request = lambda *a, **k: True
mailer.send_access_approved = (
    lambda to, link: captured.__setitem__("approved_link", link) or True)

from app import auth as auth_mod  # noqa: E402
from app import skills  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import admin as admin_router  # noqa: E402


def _set_email_domain(domain):
    """Explicit, never ambient: EMAIL_DOMAIN="" (accept-any-domain) is the
    DEFAULT several tests below depend on. Popping the OS var would just
    fall through to whatever a real developer .env sets, which is exactly
    the bleed run_ci_local.sh/ci_env.sh exist to prevent -- see CLAUDE.md's
    "Test-env gotcha". An explicit "" always wins."""
    os.environ["EMAIL_DOMAIN"] = domain or ""
    get_settings.cache_clear()


def _fake_embed(text):
    """Deterministic bag-of-words vector (8 dims, L2-normalized) — mirrors
    backend/tests/test_skills.py's helper, kept local since each backend/tests/ suite is a
    self-contained, dependency-light script."""
    v = np.zeros(8, dtype=np.float32)
    for w in text.lower().split():
        b = int(hashlib.md5(w.encode()).hexdigest(), 16) % 8
        v[b] += 1.0
    n = np.linalg.norm(v)
    return (v / n) if n else v

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _login(c, email="admin@example.edu"):
    c.post("/api/auth/request", json={"email": email})
    token = captured["link"].split("token=")[1]
    assert c.post("/api/auth/verify", json={"token": token}).status_code == 200


def _seed_access_request(email, status="pending", created_at=None):
    """Insert one access_requests row directly, bypassing request_login (which
    has its own dedicated suite in backend/tests/test_access_gate.py). Returns the new
    row's id."""
    con = connect()
    try:
        con.execute(
            "INSERT INTO access_requests(email, status, created_at) VALUES (?,?,?)",
            (email, status, created_at if created_at is not None else time.time()))
        con.commit()
        row_id = con.execute(
            "SELECT id FROM access_requests WHERE email=? ORDER BY id DESC LIMIT 1",
            (email,)).fetchone()[0]
    finally:
        con.close()
    return row_id


# Unmistakable sentinel for the usage-dashboard privacy contract tests below
# (see test_usage_dashboard_never_leaks_question_text). Distinctive enough
# that it could never appear in any real column value by accident.
USAGE_SENTINEL = "SENTINEL_SECRET_QUESTION_TEXT_DO_NOT_LEAK"


def _get_or_create_user(email):
    """Look up (or create) a users row for email, returning its id. Usage_log
    rows FK-reference users(id) only loosely (no FK declared on user_id, but
    the /usage endpoint's top_users JOIN requires a matching users row to
    show up), so seeding a real user keeps this realistic."""
    con = connect()
    try:
        row = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if row:
            return row["id"]
        cur = con.execute(
            "INSERT INTO users(email, is_admin, created_at) VALUES (?,0,?)",
            (email, time.time()))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _seed_usage_log(email, question, created_at=None, model_used="deepseek-v4-flash",
                     ok=1, cached=0, cost=0.01, prompt_tokens=10, completion_tokens=20,
                     escalated=0):
    """Insert one usage_log row directly (mirroring the exact column set
    backend/app/routers/chat.py:_persist's own INSERT uses), bypassing the full
    chat-turn/streaming path -- the same direct-seed convention this file
    already uses for access_requests (_seed_access_request above). Returns
    the seeded user's id."""
    user_id = _get_or_create_user(email)
    con = connect()
    try:
        con.execute(
            "INSERT INTO usage_log(user_id, question, model_used, escalated, "
            "prompt_tokens, completion_tokens, ok, cached, cost, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user_id, question, model_used, escalated, prompt_tokens,
             completion_tokens, ok, cached, cost,
             created_at if created_at is not None else time.time()))
        con.commit()
    finally:
        con.close()
    return user_id


class _SyncThread:
    """Runs the target immediately (synchronously) instead of on a real
    background thread, so a mocked run_import completes before .start()
    returns and the test can assert on the job row deterministically."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


def _run_import_success(job_id, upload_paths):
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
                   files={"files": ("notes.txt", b"hello", "text/plain")})
        assert r.status_code == 400, r.text


def test_import_conflicts_while_one_already_running():
    with TestClient(app) as c:
        _login(c)
        assert admin_router._import_lock.acquire(blocking=False)
        try:
            r = c.post("/api/admin/import",
                       files={"files": ("IPEDS202526.accdb", b"data",
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
            # A multi-file batch (the drag-drop shape) — every part is named "files".
            r = c.post("/api/admin/import",
                       files=[("files", ("IPEDS202425.accdb", b"a", "application/octet-stream")),
                              ("files", ("IPEDS202526.accdb", b"b", "application/octet-stream"))])
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


# ---------------------------------------------------------------------------
# NCES year catalog + batch integrate
#
# admin_router.nces.probe_catalog and admin_router.importer._years are
# monkeypatched as bare module-level names (mirrors the existing
# importer.subprocess.Popen / importer.preflight convention above) so the
# router must call `nces.probe_catalog(...)` / `importer._years(...)`
# (through the module, not a captured `from ... import` binding) for these
# patches to take effect.
# ---------------------------------------------------------------------------

_FAKE_CATALOG = [
    {"start_year": 2022, "year_label": "2022-23", "year": 2023,
     "available": True, "release": "Final", "zip_bytes": 100_000_000},
    {"start_year": 2023, "year_label": "2023-24", "year": 2024,
     "available": True, "release": "Final", "zip_bytes": 110_000_000},
    {"start_year": 2024, "year_label": "2024-25", "year": 2025,
     "available": True, "release": "Provisional", "zip_bytes": 120_000_000},
    {"start_year": 2025, "year_label": "2025-26", "year": 2026,
     "available": False, "release": None, "zip_bytes": None},
]


def _fake_disk_usage(total=100_000_000_000, used=40_000_000_000, free=60_000_000_000):
    import types as _types
    return lambda path: _types.SimpleNamespace(total=total, used=used, free=free)


def _seed_provenance(rows):
    """rows: list of (start_year, end_year, release, source). Returns a
    restore callable that deletes exactly these rows afterward."""
    con = connect()
    try:
        for start_year, end_year, release, source in rows:
            con.execute(
                "INSERT OR REPLACE INTO year_provenance"
                "(start_year, end_year, release, source, updated_at) "
                "VALUES (?,?,?,?,0)", (start_year, end_year, release, source))
        con.commit()
    finally:
        con.close()

    def _restore():
        con2 = connect()
        try:
            for start_year, *_ in rows:
                con2.execute("DELETE FROM year_provenance WHERE start_year=?", (start_year,))
            con2.commit()
        finally:
            con2.close()
    return _restore


def _patch_catalog(catalog=_FAKE_CATALOG, integrated_years=(2024, 2025), disk_usage=None):
    """integrated_years mirrors importer._years()'s return (ending years);
    already-integrated start_years = {y-1 for y in integrated_years}.
    disk_usage, if given, monkeypatches admin_router.shutil.disk_usage for a
    deterministic "disk" block in the /import/catalog response."""
    orig_probe = admin_router.nces.probe_catalog
    orig_years = admin_router.importer._years
    orig_disk = admin_router.shutil.disk_usage
    admin_router.nces.probe_catalog = lambda refresh=False: catalog
    admin_router.importer._years = lambda path: list(integrated_years)
    admin_router.shutil.disk_usage = disk_usage or _fake_disk_usage()

    def _restore():
        admin_router.nces.probe_catalog = orig_probe
        admin_router.importer._years = orig_years
        admin_router.shutil.disk_usage = orig_disk
    return _restore


def test_import_catalog_marks_integrated_vs_selectable():
    with TestClient(app) as c:
        _login(c)
        restore = _patch_catalog()
        try:
            r = c.get("/api/admin/import/catalog")
        finally:
            restore()

        assert r.status_code == 200, r.text
        body = r.json()
        assert "probed_at" in body, body
        assert "partial" in body and isinstance(body["partial"], bool), body
        assert "years" in body, body

        by_year = {e["start_year"]: e for e in body["years"]}

        assert by_year[2022]["integrated"] is False, by_year[2022]
        assert by_year[2022]["available"] is True, by_year[2022]
        assert by_year[2022]["release"] == "Final", by_year[2022]
        assert by_year[2022]["selectable"] is True, by_year[2022]
        assert by_year[2022]["status"] == "final", by_year[2022]

        # The endpoint must carry a disk section (the UI's headroom gauge reads
        # it); the concrete byte values here are just _fake_disk_usage echoed
        # back, so only the section's presence is asserted.
        assert "disk" in body, body

        assert "calibration" in body, body
        calib = body["calibration"]
        for key in ("expand_factor", "default_per_year_db_mb", "bandwidth_mbps",
                   "build_seconds_per_year", "safety_factor", "per_year_db_bytes",
                   "live_db_bytes", "already_integrated_count"):
            assert key in calib, f"calibration missing {key!r}: {calib}"
        assert calib["already_integrated_count"] == 2, calib  # {2023, 2024}

        # 2023, 2024 are already integrated (importer._years -> [2024, 2025]).
        assert by_year[2023]["integrated"] is True, by_year[2023]
        assert by_year[2023]["selectable"] is False, by_year[2023]
        assert by_year[2023]["status"] == "integrated", by_year[2023]

        assert by_year[2024]["integrated"] is True, by_year[2024]
        assert by_year[2024]["selectable"] is False, by_year[2024]
        assert by_year[2024]["status"] == "integrated", by_year[2024]

        # 2025 is not integrated and NCES doesn't have it yet either.
        assert by_year[2025]["integrated"] is False, by_year[2025]
        assert by_year[2025]["available"] is False, by_year[2025]
        assert by_year[2025]["selectable"] is False, by_year[2025]
        assert by_year[2025]["status"] == "unknown", by_year[2025]


def test_import_catalog_marks_provisional_integrated_as_update_when_final_now_available():
    # 2023 was integrated as Provisional (per year_provenance); the catalog
    # now shows it available as Final -> status="update", selectable=True
    # (re-integrable to pick up the Final release), even though `integrated`
    # stays True.
    restore_prov = _seed_provenance([(2023, 2024, "Provisional", "nces")])
    with TestClient(app) as c:
        _login(c)
        restore_cat = _patch_catalog()  # 2023's catalog entry has release="Final"
        try:
            r = c.get("/api/admin/import/catalog")
        finally:
            restore_cat()
            restore_prov()

        assert r.status_code == 200, r.text
        by_year = {e["start_year"]: e for e in r.json()["years"]}
        assert by_year[2023]["integrated"] is True, by_year[2023]
        assert by_year[2023]["status"] == "update", by_year[2023]
        assert by_year[2023]["selectable"] is True, by_year[2023]


def test_import_catalog_final_integrated_as_final_is_not_an_update():
    # 2024 was integrated already as Final (per year_provenance) and the
    # catalog's current release is ALSO Final (_FAKE_CATALOG) — no newer
    # release exists, so this must stay a plain "integrated", not "update".
    restore_prov = _seed_provenance([(2023, 2024, "Final", "nces")])
    with TestClient(app) as c:
        _login(c)
        restore_cat = _patch_catalog()
        try:
            r = c.get("/api/admin/import/catalog")
        finally:
            restore_cat()
            restore_prov()

        by_year = {e["start_year"]: e for e in r.json()["years"]}
        assert by_year[2023]["status"] == "integrated", by_year[2023]
        assert by_year[2023]["selectable"] is False, by_year[2023]


def test_import_catalog_unknown_provenance_integrated_year_stays_integrated():
    # An integrated year with NO year_provenance row at all (e.g. imported
    # before this feature existed, or a manual upload) must never crash and
    # must never be reported as "update" — provenance is simply unknown.
    with TestClient(app) as c:
        _login(c)
        restore = _patch_catalog()  # no _seed_provenance call at all
        try:
            r = c.get("/api/admin/import/catalog")
        finally:
            restore()
        assert r.status_code == 200, r.text
        by_year = {e["start_year"]: e for e in r.json()["years"]}
        assert by_year[2023]["status"] == "integrated", by_year[2023]
        assert by_year[2023]["selectable"] is False, by_year[2023]
        assert by_year[2024]["status"] == "integrated", by_year[2024]
        assert by_year[2024]["selectable"] is False, by_year[2024]


def test_import_catalog_null_release_provenance_stays_integrated_never_crashes():
    # A NULL release in year_provenance (manual import, source='manual') must
    # be treated the same as "unknown provenance" — never "update", never a 500.
    restore_prov = _seed_provenance([(2023, 2024, None, "manual")])
    with TestClient(app) as c:
        _login(c)
        restore_cat = _patch_catalog()
        try:
            r = c.get("/api/admin/import/catalog")
        finally:
            restore_cat()
            restore_prov()
        assert r.status_code == 200, r.text
        by_year = {e["start_year"]: e for e in r.json()["years"]}
        assert by_year[2023]["status"] == "integrated", by_year[2023]
        assert by_year[2023]["selectable"] is False, by_year[2023]


def test_import_catalog_requires_admin():
    with TestClient(app) as c:  # never logged in
        r = c.get("/api/admin/import/catalog")
        assert r.status_code == 401, r.text


def test_integrate_requires_admin():
    with TestClient(app) as c:  # never logged in
        r = c.post("/api/admin/import/integrate", json={"years": [2022]})
        assert r.status_code == 401, r.text


def _run_integrate_success(job_id, start_years):
    con = connect()
    try:
        con.execute("UPDATE import_jobs SET status='swapped', report=? WHERE id=?",
                    ("mock integrate ok", job_id))
        con.commit()
    finally:
        con.close()


def test_integrate_success_creates_a_job():
    with TestClient(app) as c:
        _login(c)
        restore = _patch_catalog()
        orig_thread = admin_router.threading.Thread
        orig_run_integrate = admin_router.importer.run_integrate
        admin_router.threading.Thread = _SyncThread
        admin_router.importer.run_integrate = _run_integrate_success
        try:
            r = c.post("/api/admin/import/integrate", json={"years": [2022]})
        finally:
            restore()
            admin_router.threading.Thread = orig_thread
            admin_router.importer.run_integrate = orig_run_integrate

        assert r.status_code == 200, r.text
        body = r.json()
        assert "job_id" in body, body
        assert body["status"] == "pending", body

        detail = c.get(f"/api/admin/import/jobs/{body['job_id']}")
        assert detail.status_code == 200 and detail.json()["status"] == "swapped", detail.text


def test_integrate_rejects_out_of_range_year():
    with TestClient(app) as c:
        _login(c)
        restore = _patch_catalog()
        try:
            r = c.post("/api/admin/import/integrate", json={"years": [1900]})
        finally:
            restore()
        assert r.status_code == 400, r.text


def test_integrate_rejects_already_integrated_year():
    with TestClient(app) as c:
        _login(c)
        restore = _patch_catalog()  # 2023 is already integrated
        try:
            r = c.post("/api/admin/import/integrate", json={"years": [2023]})
        finally:
            restore()
        assert r.status_code == 400, r.text


def test_integrate_accepts_reselecting_an_update_year():
    # 2023 was integrated as Provisional; the catalog now offers Final ->
    # status="update", and POST /import/integrate must ACCEPT re-selecting it
    # (unlike a plain already-integrated year, which stays rejected — see
    # test_integrate_rejects_already_integrated_year above).
    restore_prov = _seed_provenance([(2023, 2024, "Provisional", "nces")])
    with TestClient(app) as c:
        _login(c)
        restore_cat = _patch_catalog()
        orig_thread = admin_router.threading.Thread
        orig_run_integrate = admin_router.importer.run_integrate
        admin_router.threading.Thread = _SyncThread
        admin_router.importer.run_integrate = _run_integrate_success
        try:
            r = c.post("/api/admin/import/integrate", json={"years": [2023]})
        finally:
            restore_cat()
            restore_prov()
            admin_router.threading.Thread = orig_thread
            admin_router.importer.run_integrate = orig_run_integrate
        assert r.status_code == 200, r.text


def test_integrate_rejects_unavailable_year():
    with TestClient(app) as c:
        _login(c)
        restore = _patch_catalog()  # 2025 is not available from NCES
        try:
            r = c.post("/api/admin/import/integrate", json={"years": [2025]})
        finally:
            restore()
        assert r.status_code == 400, r.text


def test_integrate_conflicts_while_one_already_running():
    with TestClient(app) as c:
        _login(c)
        restore = _patch_catalog()
        assert admin_router._import_lock.acquire(blocking=False)
        try:
            r = c.post("/api/admin/import/integrate", json={"years": [2022]})
            assert r.status_code == 409, r.text
        finally:
            admin_router._import_lock.release()
            restore()


# ---------------------------------------------------------------------------
# DELETE /import/year/{start_year} — remove an integrated year ("trashcan").
#
# admin_router.importer.run_deintegrate does not exist yet (FEATURE A is not
# implemented) — _patch_attr below is a TDD-safe monkeypatch: it never
# AttributeErrors reading a not-yet-existing module attribute, and correctly
# removes the attribute again on restore if it wasn't there to begin with.
# ---------------------------------------------------------------------------

def _patch_attr(module, name, value):
    had = hasattr(module, name)
    orig = getattr(module, name, None)
    setattr(module, name, value)

    def _restore():
        if had:
            setattr(module, name, orig)
        else:
            delattr(module, name)
    return _restore


def _patch_years(integrated_years):
    """Monkeypatch admin_router.importer._years (a bare module attribute,
    same convention as _patch_catalog above) so _integrated_starts() ->
    {y - 1 for y in integrated_years} without touching a real ipeds.db."""
    orig = admin_router.importer._years
    admin_router.importer._years = lambda path: list(integrated_years)

    def _restore():
        admin_router.importer._years = orig
    return _restore


def test_deintegrate_requires_admin():
    with TestClient(app) as c:  # never logged in
        r = c.delete("/api/admin/import/year/2023")
        assert r.status_code == 401, r.text


def test_deintegrate_conflicts_while_one_already_running():
    with TestClient(app) as c:
        _login(c)
        restore = _patch_years([2024, 2025])  # integrated starts {2023, 2024}
        assert admin_router._import_lock.acquire(blocking=False)
        try:
            r = c.delete("/api/admin/import/year/2023")
            assert r.status_code == 409, r.text
        finally:
            admin_router._import_lock.release()
            restore()


def test_deintegrate_rejects_a_non_integrated_year():
    with TestClient(app) as c:
        _login(c)
        restore = _patch_years([2024, 2025])  # integrated starts {2023, 2024}
        try:
            r = c.delete("/api/admin/import/year/1999")  # never integrated
            assert r.status_code == 400, r.text
        finally:
            restore()


def test_deintegrate_rejects_removing_the_only_integrated_year():
    with TestClient(app) as c:
        _login(c)
        restore = _patch_years([2025])  # only one integrated start: {2024}
        try:
            r = c.delete("/api/admin/import/year/2024")
            assert r.status_code == 400, r.text
        finally:
            restore()


def _run_deintegrate_success(job_id, start_year):
    con = connect()
    try:
        con.execute("UPDATE import_jobs SET status='swapped', report=? WHERE id=?",
                    ("mock deintegrate ok", job_id))
        con.commit()
    finally:
        con.close()


def test_deintegrate_success_creates_a_job():
    with TestClient(app) as c:
        _login(c)
        restore_years = _patch_years([2024, 2025])  # integrated starts {2023, 2024}
        orig_thread = admin_router.threading.Thread
        admin_router.threading.Thread = _SyncThread
        restore_run = _patch_attr(admin_router.importer, "run_deintegrate",
                                  _run_deintegrate_success)
        try:
            r = c.delete("/api/admin/import/year/2023")
        finally:
            restore_years()
            admin_router.threading.Thread = orig_thread
            restore_run()

        assert r.status_code == 200, r.text
        body = r.json()
        assert "job_id" in body, body
        assert body["status"] == "pending", body

        detail = c.get(f"/api/admin/import/jobs/{body['job_id']}")
        assert detail.status_code == 200 and detail.json()["status"] == "swapped", detail.text


def test_allowlist_add_approval_email_failure_is_logged_not_raised():
    with TestClient(app) as c:
        _login(c)
        orig_send = admin_router.send_access_approved

        def _boom(email, link):
            raise RuntimeError("smtp is down")
        admin_router.send_access_approved = _boom
        try:
            r = c.post("/api/admin/allowlist",
                       json={"email": "newperson@example.edu"})
        finally:
            admin_router.send_access_approved = orig_send
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["invited"] is False, r.text
        # No RESEND_API_KEY is set in this suite's env (the dev/no-key world):
        # the mailer would have logged the whole email -- link included -- to
        # the console, so mail_configured must be False here, distinguishing
        # this "recoverable" failure from the key-configured one below.
        assert body["mail_configured"] is False, body


def _patch_resend_key(key):
    """Monkeypatch admin_router.get_settings (imported as a bare name via
    `from app.config import get_settings`, same convention as
    admin_router.send_access_approved above) to return a copy of the real,
    cached Settings with only resend_api_key overridden -- so every other
    field (app_public_url, etc.) that add_allowlist/mint_login_link also
    reads stays real and valid."""
    orig_get_settings = admin_router.get_settings
    real = orig_get_settings()
    fake = real.model_copy(update={"resend_api_key": key})
    admin_router.get_settings = lambda: fake

    def _restore():
        admin_router.get_settings = orig_get_settings
    return _restore


@contextlib.contextmanager
def _attached_probe_handler():
    """A REAL SqliteLogHandler (same class the app installs on the root
    logger at startup — see backend/app/logbuffer.py:install) attached to the root
    logger for the duration of the `with` block, backed by its own throwaway
    logs.db so this test's assertions are never polluted by unrelated log
    traffic from the rest of the suite.

    This is the whole point of the regression test below: a bare `caplog`-style
    capture of "was log.warning() called" would pass even if the call target
    were the excluded `ipeds.mail` logger and the record got silently dropped
    — which is exactly the bug this fix addresses (see
    app.logbuffer._EXCLUDED_LOGGERS). Routing through a real handler exercises
    that exclusion filter for real.
    """
    from app.logbuffer import SqliteLogHandler
    tmp_log_db = Path(tempfile.mkdtemp()) / "probe-logs.db"
    handler = SqliteLogHandler(str(tmp_log_db), retention_days=30)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)


def test_allowlist_add_invite_failure_with_mail_configured_survives_logbuffer():
    """The whole point of the fix: when mail IS configured and the send fails
    (the realistic path — mailer.send_email() catches the provider error
    internally and returns False; it never raises, so the existing `except`
    branch is not what surfaces this), add_allowlist must emit a WARNING that
    the persistent log store actually RETAINS, on a logger admins can read in
    the Logs tab. Before this fix, nothing was stored at all: the only log
    line lived on `ipeds.mail`, which app.logbuffer drops wholesale."""
    with _attached_probe_handler() as handler:
        with TestClient(app) as c:
            _login(c)
            restore = _patch_resend_key("re_test_key_survive")
            orig_send = admin_router.send_access_approved
            captured_link = {}

            def _fails_without_raising(email, link):
                captured_link["link"] = link
                return False  # mirrors send_email()'s real swallow-and-False path
            admin_router.send_access_approved = _fails_without_raising
            try:
                r = c.post("/api/admin/allowlist",
                           json={"email": "logbuffer-probe@example.edu"})
            finally:
                admin_router.send_access_approved = orig_send
                restore()

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["invited"] is False, body
        assert body["mail_configured"] is True, body

        recs = handler.records(limit=2000, q="logbuffer-probe@example.edu")
        assert len(recs) == 1, \
            f"expected exactly one STORED record about the failed invite, got {recs}"
        rec = recs[0]
        assert rec["name"] == "ipeds.admin", rec
        assert rec["level"] == "WARNING", rec
        assert "not delivered" in rec["msg"].lower(), rec


def test_allowlist_add_invite_failure_warning_never_leaks_link_or_token():
    """Security regression pin: ipeds.mail is excluded from the log store
    specifically because dev-mode mail logging includes the raw magic-link
    token, and the Logs view is readable by any admin. A "fix" that solved
    the visibility bug by putting the link into the (retained) ipeds.admin
    logger instead would be a genuine regression — assert that never
    happens, independent of whether the invisibility bug itself is fixed."""
    with _attached_probe_handler() as handler:
        with TestClient(app) as c:
            _login(c)
            restore = _patch_resend_key("re_test_key_leak_check")
            orig_send = admin_router.send_access_approved
            captured_link = {}

            def _fails_without_raising(email, link):
                captured_link["link"] = link
                return False
            admin_router.send_access_approved = _fails_without_raising
            try:
                r = c.post("/api/admin/allowlist",
                           json={"email": "leak-check@example.edu"})
            finally:
                admin_router.send_access_approved = orig_send
                restore()
        assert r.status_code == 200, r.text

        link = captured_link.get("link")
        assert link, "test setup bug: mock never received the minted link"
        assert "token=" in link, \
            f"test setup bug: mint_login_link didn't produce a token= link: {link}"

        blob = str(handler.records(limit=2000))
        assert link not in blob, blob
        assert "token=" not in blob, blob


def test_allowlist_add_no_stored_warning_when_send_succeeds():
    with _attached_probe_handler() as handler:
        with TestClient(app) as c:
            _login(c)
            restore = _patch_resend_key("re_test_key_success")
            orig_send = admin_router.send_access_approved
            admin_router.send_access_approved = lambda email, link: True
            try:
                r = c.post("/api/admin/allowlist",
                           json={"email": "send-ok@example.edu"})
            finally:
                admin_router.send_access_approved = orig_send
                restore()
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["invited"] is True, body
        assert body["mail_configured"] is True, body

        recs = handler.records(limit=2000, q="send-ok@example.edu")
        assert recs == [], \
            f"a successful send must not emit the invite-failure warning: {recs}"


def test_allowlist_add_no_stored_warning_in_dev_no_key_case():
    # No RESEND_API_KEY override here -- this suite's baseline env has
    # RESEND_API_KEY="" (the dev/no-key world), where the mailer legitimately
    # logs the whole email to the console instead: nothing failed, so no
    # "was NOT delivered" warning should ever be stored.
    with _attached_probe_handler() as handler:
        with TestClient(app) as c:
            _login(c)
            orig_send = admin_router.send_access_approved

            def _boom(email, link):
                raise RuntimeError("smtp is down")
            admin_router.send_access_approved = _boom
            try:
                r = c.post("/api/admin/allowlist",
                           json={"email": "dev-no-key@example.edu"})
            finally:
                admin_router.send_access_approved = orig_send
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["invited"] is False, body
        assert body["mail_configured"] is False, body

        # A DIFFERENT warning legitimately fires here (the pre-existing
        # `except Exception as e: log.warning("approval email to %s failed: "
        # "%s", ...)` branch, since the mock raises) -- that's fine and
        # expected. What must NOT appear is this fix's "was NOT delivered"
        # message, since mail_configured is False here: nothing was
        # genuinely lost, the console already has the full email.
        recs = handler.records(limit=2000, q="dev-no-key@example.edu")
        assert not any("not delivered" in r["msg"].lower() for r in recs), \
            f"the no-key dev case must not emit the invite-failure-with-mail-" \
            f"configured warning: {recs}"


def test_allowlist_add_response_includes_delivery_key():
    """Pin the real response shape: {ok, email, invited, mail_configured,
    delivery}. If this drifts, the e2e mocks (which hand-construct this same
    body) have nothing to drift against.

    Note: this suite globally replaces app.mailer.send_access_approved with a
    lambda that always returns True (see the module-level `mailer.` patches
    near the top of this file) before admin.py's `from app.mailer import
    send_access_approved` binds it -- so, unlike production, admin_router's
    binding does NOT already mirror the real no-key-returns-False behavior.
    Override it here to mirror the real send_email() no-key path so
    `delivery` comes out the way it would for an actual dev-mode server."""
    with TestClient(app) as c:
        _login(c)
        orig_send = admin_router.send_access_approved
        admin_router.send_access_approved = lambda email, link: False
        try:
            r = c.post("/api/admin/allowlist", json={"email": "shape-check@example.edu"})
        finally:
            admin_router.send_access_approved = orig_send
        assert r.status_code == 200, r.text
        body = r.json()
        for key in ("ok", "email", "invited", "mail_configured", "delivery"):
            assert key in body, f"{key!r} missing from add_allowlist response: {body}"
        # This suite's baseline env has no RESEND_API_KEY -- the no-key/dev path.
        assert body["delivery"] == "logged_to_console", body


def test_allowlist_add_delivery_emailed_when_send_succeeds():
    with TestClient(app) as c:
        _login(c)
        restore = _patch_resend_key("re_test_key_delivery_emailed")
        orig_send = admin_router.send_access_approved
        admin_router.send_access_approved = lambda email, link: True
        try:
            r = c.post("/api/admin/allowlist",
                       json={"email": "delivery-emailed@example.edu"})
        finally:
            admin_router.send_access_approved = orig_send
            restore()
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["delivery"] == "emailed", body
        assert body["invited"] is True, body
        assert body["mail_configured"] is True, body


def test_allowlist_add_delivery_failed_when_key_configured_and_send_fails():
    with TestClient(app) as c:
        _login(c)
        restore = _patch_resend_key("re_test_key_delivery_failed")
        orig_send = admin_router.send_access_approved
        admin_router.send_access_approved = lambda email, link: False
        try:
            r = c.post("/api/admin/allowlist",
                       json={"email": "delivery-failed@example.edu"})
        finally:
            admin_router.send_access_approved = orig_send
            restore()
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["delivery"] == "failed", body
        assert body["invited"] is False, body
        assert body["mail_configured"] is True, body


def test_allowlist_add_delivery_logged_to_console_when_no_key():
    # This suite's baseline env has RESEND_API_KEY="" -- mail_configured comes
    # out False for free. invited must still be forced False here: the
    # module-level mock (see file header) makes send_access_approved always
    # succeed, unlike the real send_email(), which returns False with no key.
    with TestClient(app) as c:
        _login(c)
        orig_send = admin_router.send_access_approved
        admin_router.send_access_approved = lambda email, link: False
        try:
            r = c.post("/api/admin/allowlist",
                       json={"email": "delivery-console@example.edu"})
        finally:
            admin_router.send_access_approved = orig_send
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["delivery"] == "logged_to_console", body
        assert body["invited"] is False, body
        assert body["mail_configured"] is False, body


def test_allowlist_add_delivery_already_allowlisted_on_reaadd_no_send_attempted():
    """The #57 bug this whole change fixes: re-adding an ALREADY-allowlisted
    address must never mint a link or attempt a send -- delivery must be
    "already_allowlisted", not something that reads as a mail failure."""
    with TestClient(app) as c:
        _login(c)
        restore = _patch_resend_key("re_test_key_already_allowlisted")
        orig_send = admin_router.send_access_approved
        send_calls = []

        def _track(email, link):
            send_calls.append((email, link))
            return True
        admin_router.send_access_approved = _track
        try:
            first = c.post("/api/admin/allowlist",
                           json={"email": "already-on@example.edu"})
            assert first.status_code == 200, first.text
            assert first.json()["delivery"] == "emailed", first.text
            assert len(send_calls) == 1, send_calls

            second = c.post("/api/admin/allowlist",
                            json={"email": "already-on@example.edu",
                                  "note": "updated note"})
        finally:
            admin_router.send_access_approved = orig_send
            restore()

        assert second.status_code == 200, second.text
        body = second.json()
        assert body["delivery"] == "already_allowlisted", body
        assert body["invited"] is False, body
        assert body["mail_configured"] is True, body
        # No second send was even attempted for the re-add.
        assert len(send_calls) == 1, send_calls

        # The note WAS updated (re-add still does its one real job).
        row = next(x for x in c.get("/api/admin/allowlist").json()
                  if x["email"] == "already-on@example.edu")
        assert row["note"] == "updated note", row


def test_allowlist_add_delivery_already_allowlisted_never_emits_failure_warning():
    """The exact regression being fixed: before this change, `invited=False`
    for an already-allowlisted re-add was indistinguishable from a genuine
    send failure, and admin.py's #59 'was NOT delivered' warning is gated on
    `if invite_link:` -- so it must NEVER fire for an already-allowlisted
    re-add, where invite_link is None. Routed through the REAL logbuffer
    handler (see _attached_probe_handler's docstring) so this actually
    exercises the ipeds.mail exclusion filter, not just a bare call-was-made
    check."""
    with _attached_probe_handler() as handler:
        with TestClient(app) as c:
            _login(c)
            restore = _patch_resend_key("re_test_key_already_allowlisted_2")
            orig_send = admin_router.send_access_approved
            admin_router.send_access_approved = lambda email, link: True
            try:
                first = c.post("/api/admin/allowlist",
                               json={"email": "already-on-2@example.edu"})
                assert first.status_code == 200, first.text
                assert first.json()["delivery"] == "emailed", first.text

                second = c.post("/api/admin/allowlist",
                                json={"email": "already-on-2@example.edu"})
            finally:
                admin_router.send_access_approved = orig_send
                restore()

        assert second.status_code == 200, second.text
        assert second.json()["delivery"] == "already_allowlisted", second.text

        recs = handler.records(limit=2000, q="already-on-2@example.edu")
        assert not any("not delivered" in r["msg"].lower() for r in recs), \
            f"re-adding an already-allowlisted address must never emit the " \
            f"invite-failure-with-mail-configured warning -- nothing failed: {recs}"


def _is_admin(c, email):
    row = next((x for x in c.get("/api/admin/allowlist").json()
                if x["email"] == email), None)
    return bool(row and row["is_admin"])


def test_promote_makes_user_admin_immediately_on_live_session():
    with TestClient(app) as c:
        _login(c)
        c.post("/api/admin/allowlist", json={"email": "prof@example.edu"})
        # prof signs in as a normal (non-admin) user
        prof = TestClient(app)
        ptok = captured["approved_link"].split("token=")[1]
        assert prof.post("/api/auth/verify", json={"token": ptok}).status_code == 200
        assert prof.get("/api/auth/me").json()["is_admin"] is False
        assert prof.get("/api/admin/allowlist").status_code == 403  # not admin yet

        r = c.patch("/api/admin/allowlist/prof@example.edu", json={"is_admin": True})
        assert r.status_code == 200 and r.json()["is_admin"] is True, r.text
        # is_admin is read live, so prof's EXISTING session is now admin
        assert prof.get("/api/auth/me").json()["is_admin"] is True
        assert prof.get("/api/admin/allowlist").status_code == 200


def test_demote_admin_when_another_exists():
    with TestClient(app) as c:
        _login(c)
        c.post("/api/admin/allowlist", json={"email": "prof2@example.edu"})
        c.patch("/api/admin/allowlist/prof2@example.edu", json={"is_admin": True})
        assert _is_admin(c, "prof2@example.edu") is True
        r = c.patch("/api/admin/allowlist/prof2@example.edu", json={"is_admin": False})
        assert r.status_code == 200 and r.json()["is_admin"] is False, r.text
        assert _is_admin(c, "prof2@example.edu") is False


def test_patch_admin_404_for_non_allowlisted():
    with TestClient(app) as c:
        _login(c)
        r = c.patch("/api/admin/allowlist/nobody@nowhere.test", json={"is_admin": True})
        assert r.status_code == 404, r.text


def test_cannot_demote_self():
    with TestClient(app) as c:
        _login(c)  # signed in as admin@example.edu
        r = c.patch("/api/admin/allowlist/admin@example.edu", json={"is_admin": False})
        assert r.status_code == 400, r.text
        assert _is_admin(c, "admin@example.edu") is True  # guard left them admin


def test_cannot_remove_self_from_allowlist():
    # Removing your own allowlist row would drop your admin AND kill your own
    # sessions — a self-lockout footgun that (unlike self-demote) had no guard.
    with TestClient(app) as c:
        _login(c)  # signed in as admin@example.edu
        r = c.delete("/api/admin/allowlist/admin@example.edu")
        assert r.status_code == 400, r.text
        # still on the allowlist and still admin afterward
        assert any(x["email"] == "admin@example.edu"
                   for x in c.get("/api/admin/allowlist").json())
        assert _is_admin(c, "admin@example.edu") is True


def test_can_remove_another_user():
    with TestClient(app) as c:
        _login(c)
        c.post("/api/admin/allowlist", json={"email": "leaver@example.edu"})
        assert any(x["email"] == "leaver@example.edu"
                   for x in c.get("/api/admin/allowlist").json())
        r = c.delete("/api/admin/allowlist/leaver@example.edu")
        assert r.status_code == 200, r.text
        assert not any(x["email"] == "leaver@example.edu"
                       for x in c.get("/api/admin/allowlist").json())


def _emails(c):
    return {x["email"] for x in c.get("/api/admin/allowlist").json()}


def test_bulk_add_creates_multiple_users():
    with TestClient(app) as c:
        _login(c)
        r = c.post("/api/admin/allowlist/bulk", json={"users": [
            {"email": "b1@example.edu", "note": "one"},
            {"email": "B2@Example.edu", "note": "two"},
        ]})
        assert r.status_code == 200, r.text
        assert r.json()["added"] == 2, r.json()
        emails = _emails(c)
        # both present, email normalized to lowercase
        assert {"b1@example.edu", "b2@example.edu"} <= emails, emails


def test_bulk_add_grants_admin_and_counts():
    with TestClient(app) as c:
        _login(c)
        r = c.post("/api/admin/allowlist/bulk", json={"users": [
            {"email": "boss@example.edu", "is_admin": True},
            {"email": "worker@example.edu", "is_admin": False},
        ]})
        assert r.status_code == 200, r.text
        assert r.json()["admins_granted"] == 1, r.json()
        assert _is_admin(c, "boss@example.edu") is True
        assert _is_admin(c, "worker@example.edu") is False


def test_bulk_add_skips_existing_and_in_batch_duplicates():
    with TestClient(app) as c:
        _login(c)
        c.post("/api/admin/allowlist", json={"email": "already@example.edu"})
        r = c.post("/api/admin/allowlist/bulk", json={"users": [
            {"email": "already@example.edu"},            # existing -> skip
            {"email": "fresh@example.edu"},               # add
            {"email": "Fresh@example.edu"},               # dup of the above -> skip
        ]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["added"] == 1, body
        reasons = {(s["email"], s["reason"]) for s in body["skipped"]}
        assert ("already@example.edu", "already a user") in reasons, body
        assert ("fresh@example.edu", "duplicate in file") in reasons, body
        # existing row was NOT duplicated
        assert sum(1 for e in _emails(c) if e == "already@example.edu") == 1


def test_bulk_add_reports_invalid_email_and_keeps_going():
    with TestClient(app) as c:
        _login(c)
        r = c.post("/api/admin/allowlist/bulk", json={"users": [
            {"email": "not-an-email"},                    # invalid -> skip, not fatal
            {"email": "valid@example.edu"},               # still added
        ]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["added"] == 1, body
        assert ("not-an-email", "invalid email") in {
            (s["email"], s["reason"]) for s in body["skipped"]}, body
        assert "valid@example.edu" in _emails(c)


def test_bulk_added_rows_list_in_stable_email_order():
    # Bulk-imported rows all share one added_at, so ORDER BY added_at alone is
    # arbitrary (and shuffles between requests, which reshuffles the paged UI on a
    # delete). The email tiebreak must return them consistently email-ascending.
    with TestClient(app) as c:
        _login(c)
        c.post("/api/admin/allowlist/bulk", json={"users": [
            {"email": "zed@ex.edu"}, {"email": "alpha@ex.edu"}, {"email": "mid@ex.edu"},
        ]})
        listed = [x["email"] for x in c.get("/api/admin/allowlist").json()]
        imported = [e for e in listed if e in {"zed@ex.edu", "alpha@ex.edu", "mid@ex.edu"}]
        assert imported == ["alpha@ex.edu", "mid@ex.edu", "zed@ex.edu"], imported


def test_bulk_add_sends_no_email_and_mints_no_token():
    # The defining contract of the bulk path (vs single-add): NO invite email and
    # NO sign-in token are produced. If this regresses, a CSV import silently
    # blasts the mail provider and mints a token per row.
    with TestClient(app) as c:
        _login(c)
        captured.pop("approved_link", None)
        c.post("/api/admin/allowlist/bulk", json={"users": [
            {"email": "silent@example.edu"},
        ]})
        assert "approved_link" not in captured, \
            "bulk add must not send an approval email"
        con = connect()
        try:
            n = con.execute("SELECT COUNT(*) FROM login_tokens WHERE email=?",
                            ("silent@example.edu",)).fetchone()[0]
        finally:
            con.close()
        assert n == 0, f"bulk add must not mint a sign-in token (found {n})"


def test_usage_since_after_until_is_swapped():
    with TestClient(app) as c:
        _login(c)
        r = c.get("/api/admin/usage", params={"since": 2_000_000_000, "until": 1_000_000_000})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["since"] <= body["until"], body


# ---------------------------------------------------------------------------
# GET /api/admin/usage -- privacy contract.
#
# usage_log.question KEEPS being written (Todd's explicit decision, no schema
# change) -- the fix is narrower: the admin-facing dashboard must stop
# echoing verbatim question text back out. The old "recent" key returned the
# last 20 raw question strings across ALL users, unfiltered by the request's
# own since/until window's narrowness -- and since since/until are
# caller-controlled and top_users names the active users in that window, an
# admin could trivially narrow the window to a single user's session and read
# their literal questions. That's a real content leak, not a cosmetic one.
#
# These tests are the actual regression gate: they don't just check that the
# key "recent" is gone (a rename would slip past a narrower test) -- they
# seed a distinctive sentinel as the question text and assert it never
# appears ANYWHERE in the serialized response body, under any key, in any
# nesting. Do NOT "fix" a failure here by re-adding question text to the
# response in any form; that is precisely the leak this PR removes.
# ---------------------------------------------------------------------------

def test_usage_response_has_no_recent_key():
    with TestClient(app) as c:
        _login(c)
        r = c.get("/api/admin/usage")
        assert r.status_code == 200, r.text
        assert "recent" not in r.json(), \
            "the usage dashboard must not return a 'recent' key of verbatim " \
            "question text (privacy leak; deliberately removed -- see CLAUDE.md)"


def test_usage_dashboard_never_leaks_question_text():
    """The core privacy-contract test. Seed usage_log rows whose `question`
    is an unmistakable sentinel, hit the real endpoint, and grep the ENTIRE
    raw response body (not a parsed/re-serialized dict -- the actual bytes
    the client would receive) for the sentinel. This survives a future
    "recent" being renamed to something else, or a question smuggled into
    top_users/series/totals, since it doesn't care what key it might hide
    under."""
    with TestClient(app) as c:
        _login(c)
        now = time.time()
        _seed_usage_log("leaker1@example.edu", USAGE_SENTINEL, created_at=now)
        _seed_usage_log("leaker2@example.edu", USAGE_SENTINEL + "_TWO", created_at=now)

        r = c.get("/api/admin/usage", params={"since": now - 60, "until": now + 60})
        assert r.status_code == 200, r.text
        raw = r.text  # exact wire body
        assert USAGE_SENTINEL not in raw, \
            f"question text leaked into the usage dashboard response: {raw}"
        assert (USAGE_SENTINEL + "_TWO") not in raw, \
            f"question text leaked into the usage dashboard response: {raw}"


def test_usage_dashboard_narrow_window_plus_top_users_still_no_question_text():
    """Covers the attributability angle specifically: narrowing since/until
    to exactly one user's activity window, combined with top_users naming
    that user, used to be exactly how an admin could de-anonymize the old
    'recent' list down to a single person's literal question. Confirm that
    narrowing the window to name a user via top_users still yields zero
    question text -- not just that the 'recent' key is absent."""
    with TestClient(app) as c:
        _login(c)
        t = time.time()
        email = "narrowuser@example.edu"
        _seed_usage_log(email, USAGE_SENTINEL, created_at=t)

        r = c.get("/api/admin/usage", params={"since": t - 1, "until": t + 1})
        assert r.status_code == 200, r.text
        body = r.json()

        # top_users legitimately still names the user in this narrow window --
        # that's unchanged and correct. The point is that naming them buys
        # an admin no question text whatsoever.
        assert any(u["email"] == email for u in body["top_users"]), body["top_users"]

        raw = r.text
        assert USAGE_SENTINEL not in raw, \
            f"narrow since/until + top_users must not expose question text: {raw}"


def test_usage_totals_series_top_users_unaffected_by_recent_removal():
    """MUST-STILL-WORK pin: totals/series/top_users are untouched by removing
    'recent' -- assert their presence and correctness against known seeded
    rows, so a future edit can't quietly break them while trimming the
    response dict."""
    with TestClient(app) as c:
        _login(c)
        # A fixed, arbitrary past timestamp -- NOT time.time() -- so this
        # test's tight since/until window can't accidentally sweep in rows
        # seeded (at real "now") by the sentinel/leak tests above, which
        # would inflate `queries`/`spend` and make this test flaky depending
        # on run timing/order.
        t = 1_700_000_000.0
        email = "counts@example.edu"
        _seed_usage_log(email, "q1 (not a sentinel, just filler text)",
                        created_at=t, cost=0.10)
        _seed_usage_log(email, "q2 (not a sentinel, just filler text)",
                        created_at=t, cost=0.20)

        r = c.get("/api/admin/usage", params={"since": t - 5, "until": t + 5})
        assert r.status_code == 200, r.text
        body = r.json()

        assert "totals" in body, body
        assert "series" in body, body
        assert "top_users" in body, body

        assert body["totals"]["queries"] == 2, body["totals"]
        assert abs(body["totals"]["spend"] - 0.30) < 1e-9, body["totals"]

        top = next((u for u in body["top_users"] if u["email"] == email), None)
        assert top is not None, body["top_users"]
        assert top["queries"] == 2, top


def test_usage_log_question_column_still_written():
    """Deliberate, and the flip side of the privacy fix: usage_log.question
    KEEPS being written to the database -- only the admin-facing /usage
    endpoint stops exposing it. This pins the write path (a direct seed+read
    against the same column set backend/app/routers/chat.py:_persist's INSERT uses --
    see _seed_usage_log's docstring) so nobody "helpfully" also drops the
    column or stops writing to it while fixing the leak."""
    email = "stillwritten@example.edu"
    question = "does usage_log.question still get written after the fix?"
    _seed_usage_log(email, question, created_at=time.time())

    con = connect()
    try:
        row = con.execute(
            "SELECT question FROM usage_log WHERE question=?", (question,)).fetchone()
    finally:
        con.close()
    assert row is not None and row["question"] == question, \
        "usage_log.question must still be written/readable -- this is deliberate, " \
        "not something to remove alongside the /usage endpoint fix"


def test_skills_get_includes_headline_field():
    with TestClient(app) as c:
        _login(c)
        rows = c.get("/api/admin/skills").json()
        assert rows, "expected at least the seed rows"
        assert "headline" in rows[0], f"skills list must expose the headline field: {rows[0]}"


def test_skills_patch_headline_or_lesson_reembeds():
    with TestClient(app) as c:
        _login(c)
        before = c.get("/api/admin/skills").json()
        skill_id = before[0]["id"]

        new_headline = "New generalized headline for the rule."
        new_lesson = "New generalized description explaining the rule in full."
        captured = {}

        def _capturing(text):
            captured["text"] = text
            return _fake_embed(text)

        orig_embed = skills.embed
        skills.embed = _capturing
        try:
            r = c.patch(f"/api/admin/skills/{skill_id}",
                       json={"headline": new_headline, "lesson": new_lesson})
        finally:
            skills.embed = orig_embed
        assert r.status_code == 200 and r.json()["ok"] is True, r.text

        assert captured.get("text") == skills._embed_source(new_headline, new_lesson), captured

        after = next(s for s in c.get("/api/admin/skills").json() if s["id"] == skill_id)
        assert after["headline"] == new_headline, after
        assert after["lesson"] == new_lesson, after

        con = connect()
        emb = con.execute("SELECT embedding FROM skills WHERE id=?", (skill_id,)).fetchone()[0]
        con.close()
        got = skills._from_blob(emb)
        want = _fake_embed(skills._embed_source(new_headline, new_lesson))
        assert np.allclose(got, want), (got, want)


def test_skills_patch_embed_failure_preserves_existing_embedding():
    """Regression (code review on the lesson-editor PR): skills.embed() returns
    None when fastembed didn't load. update_skill() used to write that straight
    through as `embedding=NULL` — permanently unretrievable, since
    reembed_skills_if_needed() bails out for good once the source-version
    marker is set, so nothing ever backfills it. A previously-good lesson,
    edited on a box where the embed model isn't available, must keep its
    stale-but-working vector rather than lose retrieval forever."""
    with TestClient(app) as c:
        _login(c)
        before = c.get("/api/admin/skills").json()
        skill_id = before[0]["id"]
        con = connect()
        emb_before = con.execute(
            "SELECT embedding FROM skills WHERE id=?", (skill_id,)).fetchone()[0]
        con.close()
        assert emb_before is not None, "fixture skill must start with a real embedding"

        orig_embed = skills.embed
        skills.embed = lambda text: None  # simulates fastembed unavailable
        try:
            r = c.patch(f"/api/admin/skills/{skill_id}",
                       json={"headline": "New headline while embed is down.",
                             "lesson": "New description while embed is down."})
        finally:
            skills.embed = orig_embed
        assert r.status_code == 200 and r.json()["ok"] is True, r.text

        # The text fields still update even though the embed call failed...
        after = next(s for s in c.get("/api/admin/skills").json() if s["id"] == skill_id)
        assert after["headline"] == "New headline while embed is down.", after
        assert after["lesson"] == "New description while embed is down.", after

        # ...but the embedding column is left exactly as it was, not NULLed.
        con = connect()
        emb_after = con.execute(
            "SELECT embedding FROM skills WHERE id=?", (skill_id,)).fetchone()[0]
        con.close()
        assert emb_after is not None, \
            "a failed embed() must never NULL out an existing embedding"
        assert emb_after == emb_before, \
            "embedding must be left byte-for-byte untouched when embed() fails"


def test_skills_patch_verify_only_does_not_reembed():
    with TestClient(app) as c:
        _login(c)
        before = c.get("/api/admin/skills").json()
        skill_id = before[0]["id"]
        con = connect()
        emb_before = con.execute(
            "SELECT embedding FROM skills WHERE id=?", (skill_id,)).fetchone()[0]
        con.close()

        called = {"n": 0}

        def _tracking(text):
            called["n"] += 1
            return _fake_embed(text)

        orig_embed = skills.embed
        skills.embed = _tracking
        try:
            r = c.patch(f"/api/admin/skills/{skill_id}", json={"verified": True})
        finally:
            skills.embed = orig_embed
        assert r.status_code == 200, r.text
        assert called["n"] == 0, "a verify-only PATCH must not recompute the embedding"

        con = connect()
        emb_after = con.execute(
            "SELECT embedding FROM skills WHERE id=?", (skill_id,)).fetchone()[0]
        con.close()
        assert emb_after == emb_before, "embedding must be untouched by a verify-only PATCH"


def test_skills_patch_updates_fields_and_noop_with_empty_body():
    with TestClient(app) as c:
        _login(c)
        before = c.get("/api/admin/skills").json()
        skill_id = before[0]["id"]

        assert "lesson" in before[0], "skills list must expose the lesson field"

        r = c.patch(f"/api/admin/skills/{skill_id}",
                   json={"verified": True, "lesson": "edited rule",
                         "notes": "reviewed by test", "canonical_sql": "SELECT 1"})
        assert r.status_code == 200 and r.json()["ok"] is True, r.text

        after = next(s for s in c.get("/api/admin/skills").json() if s["id"] == skill_id)
        assert after["verified"] == 1, after
        assert after["lesson"] == "edited rule", after
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


# ---------------------------------------------------------------------------
# POST /api/admin/access-requests/{email}/deny -- not implemented yet (no such
# route registered), so every test below is expected to fail red until the
# implementer ships it. A request to an unregistered route 404s automatically
# (FastAPI), which is itself the expected "not built yet" signal for most of
# these -- the assertions below are still checking real behavior (status code,
# rows written), not merely presence of a symbol.
# ---------------------------------------------------------------------------

def test_deny_access_request_marks_denied_and_clears_pending():
    with TestClient(app) as c:
        _login(c)
        email = "denyme@example.edu"
        _seed_access_request(email, status="pending")

        r = c.post(f"/api/admin/access-requests/{email}/deny")
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True, r.text

        con = connect()
        row = con.execute(
            "SELECT status FROM access_requests WHERE email=?", (email,)).fetchone()
        con.close()
        assert row is not None and row["status"] == "denied", row

        reqs = c.get("/api/admin/access-requests").json()
        assert not any(x["email"] == email for x in reqs), \
            f"a denied address must be gone from the pending list, got {reqs}"


def test_deny_keys_on_address_not_row_id():
    with TestClient(app) as c:
        _login(c)
        email = "threerows@example.edu"
        for _ in range(3):
            _seed_access_request(email, status="pending")

        r = c.post(f"/api/admin/access-requests/{email}/deny")
        assert r.status_code == 200, r.text

        con = connect()
        rows = con.execute(
            "SELECT status FROM access_requests WHERE email=?", (email,)).fetchall()
        con.close()
        assert len(rows) == 3, rows
        assert all(row["status"] == "denied" for row in rows), \
            f"a single deny call must flip ALL pending rows for the address, got {rows}"


def test_deny_unknown_or_already_handled_address_404s():
    with TestClient(app) as c:
        _login(c)
        email = "nosuchrequest@example.edu"
        con = connect()
        before = con.execute(
            "SELECT COUNT(*) FROM access_requests WHERE email=?", (email,)).fetchone()[0]
        con.close()

        r = c.post(f"/api/admin/access-requests/{email}/deny")
        assert r.status_code == 404, r.text

        con = connect()
        after = con.execute(
            "SELECT COUNT(*) FROM access_requests WHERE email=?", (email,)).fetchone()[0]
        con.close()
        assert after == before, "a 404 deny (nothing pending) must write nothing"


def test_deny_requires_admin():
    with TestClient(app) as c:  # never logged in
        r = c.post("/api/admin/access-requests/someone@example.edu/deny")
        assert r.status_code == 401, r.text


def test_deny_does_not_touch_an_approved_row():
    with TestClient(app) as c:
        _login(c)
        email = "approved-then-reapply@example.edu"
        _seed_access_request(email, status="approved")
        _seed_access_request(email, status="pending")

        r = c.post(f"/api/admin/access-requests/{email}/deny")
        assert r.status_code == 200, r.text

        con = connect()
        rows = con.execute(
            "SELECT status FROM access_requests WHERE email=? ORDER BY id",
            (email,)).fetchall()
        con.close()
        statuses = [row["status"] for row in rows]
        assert statuses.count("approved") == 1, \
            f"an existing approved row must never be touched by deny, got {statuses}"
        assert statuses.count("denied") == 1, \
            f"only the pending row should have flipped to denied, got {statuses}"


def test_allowlisting_a_denied_address_converts_the_denied_row():
    with TestClient(app) as c:
        _login(c)
        email = "denied-then-allow@example.edu"
        _seed_access_request(email, status="pending")
        deny_r = c.post(f"/api/admin/access-requests/{email}/deny")
        assert deny_r.status_code == 200, deny_r.text

        r = c.post("/api/admin/allowlist", json={"email": email})
        assert r.status_code == 200, r.text

        con = connect()
        row = con.execute(
            "SELECT status FROM access_requests WHERE email=?", (email,)).fetchone()
        con.close()
        assert row is not None and row["status"] == "approved", \
            (f"allowlisting a denied address must convert its denied row to "
             f"'approved' (the widened UPDATE), got {row['status'] if row else row}")


def test_removing_from_allowlist_does_not_resurrect_a_denial():
    """The entire justification for widening add_allowlist's UPDATE to
    `status IN ('pending','denied')`: without it, "blocked" becomes a
    two-place invariant (a denied row exists AND the address isn't currently
    allowlisted), so routine offboarding (deny -> allowlist -> later remove
    from the allowlist) silently re-blocks someone no admin meant to re-deny.
    """
    with TestClient(app) as c:
        _login(c)
        email = "offboarded@example.edu"
        _seed_access_request(email, status="pending")
        deny_r = c.post(f"/api/admin/access-requests/{email}/deny")
        assert deny_r.status_code == 200, deny_r.text
        assert c.post("/api/admin/allowlist", json={"email": email}).status_code == 200
        assert c.delete(f"/api/admin/allowlist/{email}").status_code == 200

        con = connect()
        try:
            try:
                still_denied = auth_mod.is_denied(con, email)
            except AttributeError as e:
                raise AssertionError(
                    "app.auth.is_denied does not exist yet -- this test can only "
                    f"run once the feature adds it: {e}") from e
        finally:
            con.close()
        assert still_denied is False, \
            "removing a converted address from the allowlist must not resurrect its old denial"

        # A fresh request from this now-unblocked address must file a new row.
        con = connect()
        before = con.execute(
            "SELECT COUNT(*) FROM access_requests WHERE email=?", (email,)).fetchone()[0]
        con.close()
        with TestClient(app) as c2:
            r = c2.post("/api/auth/request", json={"email": email})
            assert r.status_code == 200, r.text
        con = connect()
        after = con.execute(
            "SELECT COUNT(*) FROM access_requests WHERE email=?", (email,)).fetchone()[0]
        con.close()
        assert after == before + 1, \
            (f"a fresh request from the un-blocked address must file a new "
             f"row, before={before} after={after}")


def test_access_requests_list_collapses_duplicates_per_address():
    with TestClient(app) as c:
        _login(c)
        email_a = "dup-a@example.edu"
        email_b = "dup-b@example.edu"
        t0 = time.time()
        _seed_access_request(email_a, status="pending", created_at=t0)
        _seed_access_request(email_a, status="pending", created_at=t0 + 10)
        _seed_access_request(email_a, status="pending", created_at=t0 + 20)  # most recent
        _seed_access_request(email_b, status="pending", created_at=t0 + 5)

        reqs = c.get("/api/admin/access-requests").json()
        a_rows = [r for r in reqs if r["email"] == email_a]
        b_rows = [r for r in reqs if r["email"] == email_b]
        assert len(a_rows) == 1, \
            f"expected exactly ONE collapsed row for {email_a}, got {a_rows}"
        assert len(b_rows) == 1, \
            f"expected exactly ONE collapsed row for {email_b}, got {b_rows}"
        assert set(a_rows[0].keys()) == {"id", "email", "reason", "status", "created_at"}, \
            a_rows[0]
        assert abs(a_rows[0]["created_at"] - (t0 + 20)) < 1, \
            f"collapsed row's created_at must be the MOST RECENT pending request, got {a_rows[0]}"


# ---------------------------------------------------------------------------
# FIX ROUND -- Defect 2 (HIGH, security review, CONFIRMED): plus-addressing
# bypasses a denial. Exact-string matching is fail-CLOSED for an allowlist but
# fail-OPEN for a denylist. Denying mallory@example.edu did NOT block
# mallory+1@example.edu or MALLORY+X@example.edu, so the admin's "block this
# address" action was silently bypassable by anyone who controls the mailbox
# (Gmail/Workspace/M365 all deliver user+tag@domain to user@domain -- this is
# RFC-valid sub-addressing, not a hypothetical). The fix: match on a
# CANONICAL form (lowercase + `+tag` local-part suffix stripped) via a new
# `canon_email` column (migration 9), in BOTH directions.
#
# Every row below is created through the REAL public HTTP surface
# (POST /api/auth/request, POST .../deny) rather than a raw SQL seed, so
# canon_email -- however the implementation chooses to populate it (computed
# at insert time, a DB trigger/generated column, whatever) -- gets populated
# exactly the way it would in production. That also means these tests are
# implementation-agnostic about the *mechanism* and only pin the observable
# behavior, which is what actually matters here.
#
# Explicitly NOT covered / NOT wanted: gmail-style DOT-STRIPPING. Dots ARE
# significant on many domains (john.smith@ and johnsmith@ can be two
# different real people), so collapsing them risks blocking an innocent third
# party. test_dots_in_local_part_are_not_canonicalized pins that this is
# never "improved" into a false-positive generator.
# ---------------------------------------------------------------------------

def test_deny_canonicalizes_plus_tag_and_case_variants():
    _set_email_domain("")
    with TestClient(app) as c:
        _login(c)
        bare = "mallory@example.edu"
        assert c.post("/api/auth/request", json={"email": bare}).status_code == 200

        deny_r = c.post(f"/api/admin/access-requests/{bare}/deny")
        assert deny_r.status_code == 200, deny_r.text

        orig_send = auth_mod.send_access_request
        spy_calls = []
        auth_mod.send_access_request = lambda *a, **k: spy_calls.append(a) or True
        try:
            for variant in ("mallory+1@example.edu", "mallory+anything@example.edu",
                           "MALLORY+X@example.edu"):
                r = c.post("/api/auth/request", json={"email": variant})
                assert r.status_code == 200, r.text
        finally:
            auth_mod.send_access_request = orig_send

        con = connect()
        try:
            for variant in ("mallory+1@example.edu", "mallory+anything@example.edu",
                           "mallory+x@example.edu"):  # request_login lowercases
                rows = con.execute(
                    "SELECT * FROM access_requests WHERE email=?", (variant,)).fetchall()
                assert rows == [], (
                    f"{variant} must be blocked by the bare-address denial "
                    f"(canonical match required), got a new row: {rows}")
        finally:
            con.close()
        assert spy_calls == [], (
            f"a canonically-denied +tag/case variant must trigger NO admin "
            f"notification email, got {spy_calls}")


def test_deny_of_plus_tag_variant_also_blocks_the_bare_address():
    """Canonicalization must work in BOTH directions: denying a +tag variant
    must also block the bare address, not just the reverse (the case above)."""
    _set_email_domain("")
    with TestClient(app) as c:
        _login(c)
        variant = "bobdeny+work@example.edu"
        bare = "bobdeny@example.edu"
        assert c.post("/api/auth/request", json={"email": variant}).status_code == 200

        deny_r = c.post(f"/api/admin/access-requests/{variant}/deny")
        assert deny_r.status_code == 200, deny_r.text

        r = c.post("/api/auth/request", json={"email": bare})
        assert r.status_code == 200, r.text

        con = connect()
        try:
            rows = con.execute(
                "SELECT * FROM access_requests WHERE email=?", (bare,)).fetchall()
        finally:
            con.close()
        assert rows == [], (
            f"the bare address must be blocked after denying its +tag "
            f"variant (canonical match must work in BOTH directions), got {rows}")


def test_deny_clears_all_pending_rows_sharing_canonical_address():
    """Deny is keyed on the CANONICAL address: pending rows for the bare
    address and its +tag variants must ALL flip to denied together when any
    one of them is denied."""
    _set_email_domain("")
    with TestClient(app) as c:
        _login(c)
        addrs = ["caroldeny@example.edu", "caroldeny+1@example.edu",
                "caroldeny+2@example.edu"]
        for a in addrs:
            assert c.post("/api/auth/request", json={"email": a}).status_code == 200

        deny_r = c.post("/api/admin/access-requests/caroldeny@example.edu/deny")
        assert deny_r.status_code == 200, deny_r.text

        con = connect()
        try:
            for a in addrs:
                row = con.execute(
                    "SELECT status FROM access_requests WHERE email=?", (a,)).fetchone()
                assert row is not None and row["status"] == "denied", \
                    f"expected {a} to be denied (shared canonical group), got {row}"
        finally:
            con.close()


def test_dots_in_local_part_are_not_canonicalized():
    """PIN this: dot-stripping is explicitly NOT performed. john.smith@ and
    johnsmith@ can be two different real people on many mail systems (unlike
    RFC-valid +tag sub-addressing, which really does deliver to the same
    mailbox on Gmail/Workspace/M365). Denying the dotted address must leave
    the un-dotted one completely untouched."""
    _set_email_domain("")
    with TestClient(app) as c:
        _login(c)
        dotted = "john.smithdeny@example.edu"
        undotted = "johnsmithdeny@example.edu"
        assert c.post("/api/auth/request", json={"email": dotted}).status_code == 200
        assert c.post("/api/auth/request", json={"email": undotted}).status_code == 200

        deny_r = c.post(f"/api/admin/access-requests/{dotted}/deny")
        assert deny_r.status_code == 200, deny_r.text

        con = connect()
        try:
            row = con.execute(
                "SELECT status FROM access_requests WHERE email=?", (undotted,)).fetchone()
        finally:
            con.close()
        assert row is not None and row["status"] == "pending", (
            f"denying {dotted} must NOT block the un-dotted {undotted} -- "
            f"dots are significant and must never be collapsed, got {row}")


# ---------------------------------------------------------------------------
# FIX ROUND -- Defect 3 (LOW, security review): test_deny_requires_admin above
# only covers the UNAUTHENTICATED (401) case. Add the authenticated
# NON-ADMIN -> 403 case, mirroring
# test_promote_makes_user_admin_immediately_on_live_session's pattern
# (allowlist a plain user, sign them in, hit an admin-only route).
# ---------------------------------------------------------------------------

def test_deny_requires_admin_403_for_authenticated_non_admin():
    with TestClient(app) as c:
        _login(c)
        plain_email = "not-an-admin-deny-check@example.edu"
        assert c.post("/api/admin/allowlist", json={"email": plain_email}).status_code == 200
        target_email = "target-of-deny-403-check@example.edu"
        _seed_access_request(target_email, status="pending")

        non_admin = TestClient(app)
        tok = captured["approved_link"].split("token=")[1]
        assert non_admin.post("/api/auth/verify", json={"token": tok}).status_code == 200
        assert non_admin.get("/api/auth/me").json()["is_admin"] is False

        r = non_admin.post(f"/api/admin/access-requests/{target_email}/deny")
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# ROUND 3 (.plan-undeny.md) -- see a denied-addresses list, and undo a
# denial without granting access. GET /api/admin/access-requests/denied and
# DELETE /api/admin/access-requests/{email}/denial do not exist yet (no such
# routes registered), so every test below is expected to fail red until the
# implementer ships them.
#
# THE ACCEPTANCE CRITERION: un-deny writes NO allowlist row, mints NO
# login_tokens row, creates NO users row, and sends NO email. The only prior
# way to un-block a denied address was allowlisting -- which does all four of
# those. The absence of each is the requirement; see
# test_undo_denial_grants_no_access_and_sends_no_email below, and do not
# "tidy away" any of its negative assertions as vacuous.
# ---------------------------------------------------------------------------

def test_undo_denial_grants_no_access_and_sends_no_email():
    """THE acceptance criterion. Patches app.routers.admin's OWN
    send_access_approved name (not app.mailer's) -- admin.py imports the
    symbol with `from app.mailer import send_access_approved` at module load,
    so admin_router.send_access_approved is a name bound at import time;
    patching app.mailer.send_access_approved afterward would not be observed
    by the handler. This mirrors why this file's own module-level mailer
    patches (top of file) are applied BEFORE `from app.routers import admin`
    is imported."""
    with TestClient(app) as c:
        _login(c)
        email = "victim-noaccess@example.edu"
        _seed_access_request(email, status="denied")

        spy_calls = []
        restore = _patch_attr(admin_router, "send_access_approved",
                              lambda *a, **k: spy_calls.append(a) or True)
        try:
            r = c.delete(f"/api/admin/access-requests/{email}/denial")
        finally:
            restore()
        assert r.status_code == 200, r.text

        con = connect()
        try:
            allow_n = con.execute(
                "SELECT COUNT(*) FROM allowlist WHERE email=?", (email,)).fetchone()[0]
            token_n = con.execute(
                "SELECT COUNT(*) FROM login_tokens WHERE email=?", (email,)).fetchone()[0]
            user_n = con.execute(
                "SELECT COUNT(*) FROM users WHERE email=?", (email,)).fetchone()[0]
        finally:
            con.close()
        assert allow_n == 0, \
            f"undo must NOT add {email} to the allowlist, found {allow_n} row(s)"
        assert token_n == 0, (
            f"undo must NOT mint a login token (allowlisting does this, see "
            f"admin.py:73 mint_login_link) -- this pins that undo did not "
            f"silently fall back to the allowlist path, found {token_n} row(s)")
        assert user_n == 0, \
            f"undo must NOT create a users row, found {user_n} row(s)"
        # The ABSENCE of this call IS the requirement -- do not remove or
        # weaken this assertion as "vacuous". Granting access + emailing a
        # welcome link was the ONLY prior way to un-block a denied address
        # (via allowlisting), and reproducing that here would be the exact
        # bug this endpoint exists to fix.
        assert spy_calls == [], (
            f"undo must send NO email whatsoever -- got {spy_calls}")


def test_undo_denial_clears_the_whole_canonical_group():
    """Undo via a VARIANT url (not the bare address that was originally
    denied) -- proves canonicalization runs on the DELETE's input, not just
    on the rows it matches. Without this, victim+1@ would stay blocked while
    victim@ (and every other variant) worked."""
    _set_email_domain("")
    with TestClient(app) as c:
        _login(c)
        addrs = ["cangroup@example.edu", "cangroup+1@example.edu",
                "Cangroup+X@example.edu"]
        for a in addrs:
            assert c.post("/api/auth/request", json={"email": a}).status_code == 200
        deny_r = c.post("/api/admin/access-requests/cangroup@example.edu/deny")
        assert deny_r.status_code == 200, deny_r.text

        con = connect()
        try:
            assert auth_mod.is_denied(con, "cangroup@example.edu") is True  # sanity
        finally:
            con.close()

        undo_url = ("/api/admin/access-requests/"
                   f"{quote('cangroup+1@example.edu', safe='')}/denial")
        r = c.delete(undo_url)
        assert r.status_code == 200, r.text

        con = connect()
        try:
            still_bare = auth_mod.is_denied(con, "cangroup@example.edu")
            still_new_variant = auth_mod.is_denied(con, "cangroup+9@example.edu")
        finally:
            con.close()
        assert still_bare is False, (
            "undoing via a +tag variant must clear the WHOLE canonical "
            "group, including the bare address")
        assert still_new_variant is False, (
            "undoing via one variant must also un-block a DIFFERENT, "
            "never-seen-before variant of the same mailbox")


def test_undo_denial_deletes_the_rows_not_restatuses_them():
    """Pins the resolution against a future 'safer' restatus: the canonical
    group must have ZERO rows after undo, not a row with some other status."""
    with TestClient(app) as c:
        _login(c)
        email = "gonecompletely@example.edu"
        _seed_access_request(email, status="denied")

        r = c.delete(f"/api/admin/access-requests/{email}/denial")
        assert r.status_code == 200, r.text

        con = connect()
        try:
            count = con.execute(
                "SELECT COUNT(*) FROM access_requests "
                "WHERE COALESCE(canon_email, LOWER(email))=?",
                (auth_mod.canon_email(email),)).fetchone()[0]
        finally:
            con.close()
        assert count == 0, (
            f"expected ZERO rows for the canonical group after undo (rows "
            f"are DELETEd, not re-statused -- 'never requested' is the "
            f"intended terminal state), got {count}")


def test_undo_denial_does_not_touch_approved_or_pending_rows():
    """Pins the status='denied' guard in the DELETE against ever being
    widened -- an approved row is inert history and a pending row is a live
    queue item; neither should be touched by clearing a denial."""
    with TestClient(app) as c:
        _login(c)
        email = "mixed-status-undo@example.edu"
        _seed_access_request(email, status="denied")
        _seed_access_request(email, status="approved")
        _seed_access_request(email, status="pending")

        r = c.delete(f"/api/admin/access-requests/{email}/denial")
        assert r.status_code == 200, r.text

        con = connect()
        try:
            rows = con.execute(
                "SELECT status FROM access_requests WHERE email=? ORDER BY id",
                (email,)).fetchall()
        finally:
            con.close()
        statuses = [row["status"] for row in rows]
        assert statuses.count("denied") == 0, \
            f"the denied row must be gone after undo, got {statuses}"
        assert statuses.count("approved") == 1, \
            f"an approved row must survive undo untouched, got {statuses}"
        assert statuses.count("pending") == 1, \
            f"a pending row must survive undo untouched, got {statuses}"


def test_undo_denial_is_idempotent_and_does_not_404():
    """Deliberately asymmetric with /deny's 404-on-nothing-to-do: DELETE is
    idempotent by contract, so a second undo on an already-cleared denial is
    still a 200 with cleared=0, not a 404."""
    with TestClient(app) as c:
        _login(c)
        email = "idempotent-undo@example.edu"
        _seed_access_request(email, status="denied")

        r1 = c.delete(f"/api/admin/access-requests/{email}/denial")
        assert r1.status_code == 200, r1.text
        assert r1.json().get("cleared") == 1, r1.text

        r2 = c.delete(f"/api/admin/access-requests/{email}/denial")
        assert r2.status_code == 200, (
            f"a second undo on an already-cleared denial must stay 200 "
            f"(idempotent), not 404 like /deny -- got {r2.status_code}")
        assert r2.json().get("cleared") == 0, r2.text


def test_undo_denial_requires_admin():
    with TestClient(app) as c:  # never logged in
        r = c.delete("/api/admin/access-requests/someone@example.edu/denial")
        assert r.status_code == 401, r.text


def test_denied_list_groups_canonically_and_shows_original_addresses():
    """The API-contract non-swap test: canon_email is the canonical
    (ACTUALLY BLOCKED) form -- what Undo's DELETE keys on -- while emails
    carries the raw ORIGINAL addresses that were actually filed. (Which of
    the two the UI renders, and when, is the UI's call -- see the SEC #1
    security-review test below and frontend/e2e/undo-denial.spec.js; the
    canon_email is NOT always hidden any more, since a denied group can have
    NO original equal to it at all -- see that test.) This one only pins the
    API payload itself: if a future edit swaps `canon_email` and `emails`,
    or drops an original, this goes red."""
    _set_email_domain("")
    with TestClient(app) as c:
        _login(c)
        addrs = ["listvictim@example.edu", "Listvictim+1@example.edu"]
        for a in addrs:
            assert c.post("/api/auth/request", json={"email": a}).status_code == 200
        deny_r = c.post("/api/admin/access-requests/listvictim@example.edu/deny")
        assert deny_r.status_code == 200, deny_r.text

        r = c.get("/api/admin/access-requests/denied")
        assert r.status_code == 200, r.text
        items = r.json()
        matches = [x for x in items if x.get("canon_email") == "listvictim@example.edu"]
        assert len(matches) == 1, (
            f"expected exactly ONE canonical group for listvictim@example.edu, "
            f"got {items}")
        item = matches[0]
        # request_login lowercases on insert, so both originals are stored
        # lowercase even though "Listvictim+1@example.edu" was sent mixed-case.
        assert sorted(item["emails"]) == [
            "listvictim+1@example.edu", "listvictim@example.edu"], (
            f"expected BOTH original addresses to survive in `emails`, got "
            f"{item['emails']}")


# ---------------------------------------------------------------------------
# SEC #1 (HIGH, security review of round 3) -- the API-side ground truth for
# the tagged-only griefing bypass. An attacker files ONLY a +tag variant
# (never the base address), the admin Rejects it, and the REAL victim
# (the base address, which never filed anything) is the one actually
# blocked. The backend already gets this right -- canon_email is the base,
# emails contains only the variant that was actually requested -- this test
# pins that API contract as ground truth so nobody "fixes" it into hiding
# the mismatch. The bug this enables is a FRONTEND rendering bug (the UI
# only ever showed `emails`, never `canon_email`) -- see
# frontend/e2e/undo-denial.spec.js for the display-side RED tests.
# ---------------------------------------------------------------------------

def test_denied_list_surfaces_the_canonical_address_even_when_no_original_matches_it():
    """Ground truth, reproduced directly: deny a +tag-only request and
    confirm the API response's canon_email is the BASE address (the one
    actually blocked) while emails contains ONLY the tagged variant that was
    actually filed -- canon_email is not itself among emails. This is
    already true today (no API change needed for SEC #1); it's pinned here
    so the eventual UI fix has a stable contract to build on."""
    _set_email_domain("")
    with TestClient(app) as c:
        _login(c)
        tagged_only = "onlytagged+newsletter@example.edu"
        base = "onlytagged@example.edu"
        assert c.post("/api/auth/request", json={"email": tagged_only}).status_code == 200
        deny_r = c.post(f"/api/admin/access-requests/{tagged_only}/deny")
        assert deny_r.status_code == 200, deny_r.text

        con = connect()
        try:
            assert auth_mod.is_denied(con, base) is True, (
                "sanity: denying only a +tag variant must still block the base "
                "address -- this is the actual griefing vector")
        finally:
            con.close()

        r = c.get("/api/admin/access-requests/denied")
        assert r.status_code == 200, r.text
        matches = [x for x in r.json() if x.get("canon_email") == base]
        assert len(matches) == 1, (
            f"expected one canonical group keyed on the BASE address {base}, "
            f"got {r.json()}")
        item = matches[0]
        assert item["emails"] == [tagged_only], (
            f"expected emails to contain ONLY the tagged variant that was "
            f"actually filed, got {item['emails']}")
        assert base not in item["emails"], (
            f"the base address was never itself filed, so it must not appear "
            f"in `emails` (that would be a false claim) -- it belongs in "
            f"canon_email, which it already does: {item}")


def test_deny_records_a_separate_denied_at_timestamp():
    """Deny stamps denied_at (when the request was REJECTED) without touching
    created_at (when it was REQUESTED); the denied list returns both, separately.
    Guards the spec's "store Requested and Denied separately; don't replace the
    original request timestamp with the denial timestamp."""
    _set_email_domain("")
    with TestClient(app) as c:
        _login(c)
        addr = "stamp-victim@example.edu"
        before = time.time()
        assert c.post("/api/auth/request", json={"email": addr}).status_code == 200
        con = connect()
        requested_at = con.execute(
            "SELECT created_at FROM access_requests WHERE email=?", (addr,)).fetchone()[0]
        con.close()
        assert c.post(f"/api/admin/access-requests/{addr}/deny").status_code == 200
        after = time.time()

        item = next(x for x in c.get("/api/admin/access-requests/denied").json()
                    if x["canon_email"] == addr)
        assert item["denied_at"] is not None, f"deny must record a denial time: {item}"
        assert before <= item["denied_at"] <= after + 1, item
        # The request time is preserved separately, NOT overwritten by the denial.
        assert item["created_at"] == requested_at, (
            f"deny must not rewrite created_at (requested time): "
            f"{item['created_at']} != {requested_at}")
        assert item["denied_at"] >= item["created_at"], item


def test_denied_list_tolerates_a_legacy_denial_with_no_denied_at():
    """A row denied before migration 11 has denied_at NULL; it must still list,
    with denied_at None (the Blocked table renders "—")."""
    con = connect()
    con.execute("DELETE FROM access_requests")
    con.commit()
    con.close()
    with TestClient(app) as c:
        _login(c)
        _seed_access_request("legacy-denied@example.edu", status="denied")
        item = next(x for x in c.get("/api/admin/access-requests/denied").json()
                    if x["canon_email"] == "legacy-denied@example.edu")
        assert item["denied_at"] is None, f"a pre-migration denial has no denied_at: {item}"


def test_denied_list_excludes_pending_and_approved():
    with TestClient(app) as c:
        _login(c)
        denied_email = "excl-denied@example.edu"
        pending_email = "excl-pending@example.edu"
        _seed_access_request(denied_email, status="denied")
        _seed_access_request(pending_email, status="pending")

        denied_resp = c.get("/api/admin/access-requests/denied")
        # Check the status code BEFORE treating the body as a list -- pre-
        # implementation this 404s with {"detail": "Not found"}, and
        # iterating that dict yields its keys (strings), which would crash
        # with an unhelpful AttributeError instead of a clean assertion.
        assert denied_resp.status_code == 200, denied_resp.text
        denied_list = denied_resp.json()
        assert any(x.get("canon_email") == denied_email for x in denied_list), denied_list
        assert not any(pending_email in x.get("emails", []) for x in denied_list), \
            f"a pending address must not appear in the denied list, got {denied_list}"

        pending_list = c.get("/api/admin/access-requests").json()
        assert any(x["email"] == pending_email for x in pending_list), pending_list


def test_denied_list_is_empty_when_nothing_is_denied():
    con = connect()
    con.execute("DELETE FROM access_requests")
    con.commit()
    con.close()
    with TestClient(app) as c:
        _login(c)
        r = c.get("/api/admin/access-requests/denied")
        assert r.status_code == 200, r.text
        assert r.json() == [], r.json()


def test_denied_list_requires_admin():
    with TestClient(app) as c:  # never logged in
        r = c.get("/api/admin/access-requests/denied")
        assert r.status_code == 401, r.text


def test_pending_list_still_groups_by_raw_address_not_canonically():
    """Pins the deliberate grouping asymmetry: Approve is EXACT (the
    allowlist insert is a literal address), so the pending list must group by
    the RAW address, never canonically -- unlike the denied list. If someone
    "makes them consistent", Approve would silently collapse two real people
    sharing a canonical group into one button."""
    _set_email_domain("")
    with TestClient(app) as c:
        _login(c)
        addrs = ["pendbob@example.edu", "pendbob+1@example.edu"]
        for a in addrs:
            assert c.post("/api/auth/request", json={"email": a}).status_code == 200

        reqs = c.get("/api/admin/access-requests").json()
        matches = [x for x in reqs if x["email"] in addrs]
        assert len(matches) == 2, (
            f"the pending list must show TWO separate rows for {addrs} "
            f"(grouped by raw address, not canonically), got {matches}")
        assert {x["email"] for x in matches} == set(addrs), matches


def test_allowlisting_clears_a_denial_filed_under_a_variant():
    """FOLDED-IN FIX 1 (round 3): add_allowlist's denial-clearing UPDATE
    (admin.py:68) is EXACT ('WHERE email=?'), but a denial is CANONICAL
    (spans +tag/case variants) -- so allowlisting the bare address converts
    ONLY the bare row, leaving a variant's denied row behind. Offboarding
    later (remove from allowlist) then finds that surviving variant row and
    is_denied() resurrects the block: the EXACT scenario round 2's widened
    UPDATE (status IN ('pending','denied')) was written to prevent,
    reintroduced through the variant it forgot.

    Without this test, making add_allowlist's UPDATE canonical looks like an
    unmotivated behavior change to shipped code and would get reverted. RED
    on today's code (the UPDATE is still exact)."""
    _set_email_domain("")
    with TestClient(app) as c:
        _login(c)
        bare = "resurrect@example.edu"
        variant = "resurrect+work@example.edu"
        assert c.post("/api/auth/request", json={"email": bare}).status_code == 200
        assert c.post("/api/auth/request", json={"email": variant}).status_code == 200

        deny_r = c.post(f"/api/admin/access-requests/{bare}/deny")
        assert deny_r.status_code == 200, deny_r.text  # flips BOTH bare + variant

        assert c.post("/api/admin/allowlist", json={"email": bare}).status_code == 200
        assert c.delete(f"/api/admin/allowlist/{bare}").status_code == 200

        con = connect()
        try:
            still_denied = auth_mod.is_denied(con, bare)
        finally:
            con.close()
        assert still_denied is False, (
            f"{bare} must NOT be re-blocked after offboarding -- a surviving "
            f"denied row for {variant} (not converted by an EXACT allowlist "
            f"UPDATE) is resurrecting the block")

        con = connect()
        before = con.execute(
            "SELECT COUNT(*) FROM access_requests WHERE email=?", (bare,)).fetchone()[0]
        con.close()
        r = c.post("/api/auth/request", json={"email": bare})
        assert r.status_code == 200, r.text
        con = connect()
        after = con.execute(
            "SELECT COUNT(*) FROM access_requests WHERE email=?", (bare,)).fetchone()[0]
        con.close()
        assert after == before + 1, (
            f"a fresh request from the un-blocked bare address must file a "
            f"new row, before={before} after={after}")


def run():
    print("admin router contract:")
    check("import rejects a non-.accdb upload", test_import_rejects_non_accdb_extension)
    check("import conflicts (409) while one is already running",
          test_import_conflicts_while_one_already_running)
    check("import success creates a job and the job appears in listing/detail",
          test_import_success_creates_and_completes_a_job)
    check("import job detail 404s for an unknown job id",
          test_import_job_not_found_404)
    check("import catalog marks integrated/selectable years (+ disk/calibration shape)",
          test_import_catalog_marks_integrated_vs_selectable)
    check("import catalog marks a Provisional-integrated year as 'update' when Final is now out",
          test_import_catalog_marks_provisional_integrated_as_update_when_final_now_available)
    check("import catalog: Final-integrated-as-Final is not an 'update'",
          test_import_catalog_final_integrated_as_final_is_not_an_update)
    check("import catalog: unknown provenance on an integrated year stays 'integrated'",
          test_import_catalog_unknown_provenance_integrated_year_stays_integrated)
    check("import catalog: NULL-release provenance stays 'integrated', never crashes",
          test_import_catalog_null_release_provenance_stays_integrated_never_crashes)
    check("import catalog requires admin",
          test_import_catalog_requires_admin)
    check("integrate requires admin",
          test_integrate_requires_admin)
    check("integrate success creates a job",
          test_integrate_success_creates_a_job)
    check("integrate accepts re-selecting an 'update' year",
          test_integrate_accepts_reselecting_an_update_year)
    check("integrate rejects an out-of-range year",
          test_integrate_rejects_out_of_range_year)
    check("integrate rejects an already-integrated year",
          test_integrate_rejects_already_integrated_year)
    check("integrate rejects an unavailable year",
          test_integrate_rejects_unavailable_year)
    check("integrate conflicts (409) while one is already running",
          test_integrate_conflicts_while_one_already_running)
    check("deintegrate requires admin", test_deintegrate_requires_admin)
    check("deintegrate conflicts (409) while one is already running",
          test_deintegrate_conflicts_while_one_already_running)
    check("deintegrate rejects a non-integrated year",
          test_deintegrate_rejects_a_non_integrated_year)
    check("deintegrate rejects removing the only integrated year",
          test_deintegrate_rejects_removing_the_only_integrated_year)
    check("deintegrate success creates a job", test_deintegrate_success_creates_a_job)
    check("allowlist add logs (not raises) an approval-email failure",
          test_allowlist_add_approval_email_failure_is_logged_not_raised)
    check("allowlist add: a mail-configured send failure emits a WARNING that "
          "survives the real logbuffer (not on the excluded ipeds.mail logger)",
          test_allowlist_add_invite_failure_with_mail_configured_survives_logbuffer)
    check("allowlist add: the stored invite-failure warning never leaks the "
          "magic link or a token= value",
          test_allowlist_add_invite_failure_warning_never_leaks_link_or_token)
    check("allowlist add: no stored warning when the send succeeds",
          test_allowlist_add_no_stored_warning_when_send_succeeds)
    check("allowlist add: no stored warning in the dev/no-key case",
          test_allowlist_add_no_stored_warning_in_dev_no_key_case)
    check("allowlist add: response includes the delivery key (pins the shape "
          "e2e mocks must match)",
          test_allowlist_add_response_includes_delivery_key)
    check("allowlist add: delivery='emailed' when the send succeeds",
          test_allowlist_add_delivery_emailed_when_send_succeeds)
    check("allowlist add: delivery='failed' when a key is configured and the "
          "send fails",
          test_allowlist_add_delivery_failed_when_key_configured_and_send_fails)
    check("allowlist add: delivery='logged_to_console' when no key is configured",
          test_allowlist_add_delivery_logged_to_console_when_no_key)
    check("allowlist add: delivery='already_allowlisted' on a re-add, no send "
          "attempted",
          test_allowlist_add_delivery_already_allowlisted_on_reaadd_no_send_attempted)
    check("allowlist add: an already-allowlisted re-add never emits the "
          "invite-failure warning (the #57/#59 regression this fixes)",
          test_allowlist_add_delivery_already_allowlisted_never_emits_failure_warning)
    check("promote makes a user admin immediately on their live session",
          test_promote_makes_user_admin_immediately_on_live_session)
    check("demote an admin when another admin exists",
          test_demote_admin_when_another_exists)
    check("PATCH admin 404s for a non-allowlisted email",
          test_patch_admin_404_for_non_allowlisted)
    check("cannot demote yourself (prevents self-lockout + keeps an admin)",
          test_cannot_demote_self)
    check("cannot remove yourself from the allowlist (self-lockout guard)",
          test_cannot_remove_self_from_allowlist)
    check("can remove another user from the allowlist",
          test_can_remove_another_user)
    check("bulk add creates multiple users (email normalized)",
          test_bulk_add_creates_multiple_users)
    check("bulk add grants admin and counts admins_granted",
          test_bulk_add_grants_admin_and_counts)
    check("bulk add skips existing + in-batch duplicate rows",
          test_bulk_add_skips_existing_and_in_batch_duplicates)
    check("bulk add reports an invalid email and keeps going",
          test_bulk_add_reports_invalid_email_and_keeps_going)
    check("bulk-added rows (tied added_at) list in stable email order",
          test_bulk_added_rows_list_in_stable_email_order)
    check("bulk add CONTRACT: sends no email and mints no token",
          test_bulk_add_sends_no_email_and_mints_no_token)
    check("usage dashboard swaps since/until when reversed",
          test_usage_since_after_until_is_swapped)
    check("usage dashboard response has no 'recent' key",
          test_usage_response_has_no_recent_key)
    check("usage dashboard PRIVACY CONTRACT: never leaks question text (sentinel)",
          test_usage_dashboard_never_leaks_question_text)
    check("usage dashboard: narrow since/until + top_users still leaks no question text",
          test_usage_dashboard_narrow_window_plus_top_users_still_no_question_text)
    check("usage dashboard: totals/series/top_users unaffected by 'recent' removal",
          test_usage_totals_series_top_users_unaffected_by_recent_removal)
    check("usage_log.question is still written to the DB (deliberate, not dropped)",
          test_usage_log_question_column_still_written)
    check("skills GET includes the headline field", test_skills_get_includes_headline_field)
    check("skills PATCH headline/lesson re-embeds", test_skills_patch_headline_or_lesson_reembeds)
    check("skills PATCH preserves an existing embedding when embed() fails (returns None)",
          test_skills_patch_embed_failure_preserves_existing_embedding)
    check("skills PATCH verify-only does not re-embed",
          test_skills_patch_verify_only_does_not_reembed)
    check("skills PATCH updates fields; empty body is a no-op",
          test_skills_patch_updates_fields_and_noop_with_empty_body)
    check("skills DELETE removes the row", test_skills_delete_removes_the_row)
    check("server logs endpoint returns records", test_server_logs_returns_records)
    check("server logs endpoint handles no handler installed",
          test_server_logs_with_no_handler_returns_empty)
    check("deny access request marks denied and clears the pending list",
          test_deny_access_request_marks_denied_and_clears_pending)
    check("deny keys on the address, not a single row id",
          test_deny_keys_on_address_not_row_id)
    check("deny of an unknown/already-handled address 404s and writes nothing",
          test_deny_unknown_or_already_handled_address_404s)
    check("deny requires admin", test_deny_requires_admin)
    check("deny never touches an already-approved row",
          test_deny_does_not_touch_an_approved_row)
    check("allowlisting a denied address converts the denied row to approved",
          test_allowlisting_a_denied_address_converts_the_denied_row)
    check("removing from the allowlist does not resurrect a prior denial",
          test_removing_from_allowlist_does_not_resurrect_a_denial)
    check("access-requests list collapses duplicate pending rows per address",
          test_access_requests_list_collapses_duplicates_per_address)
    check("deny canonicalizes +tag/case variants (bare denial blocks variants) (defect 2)",
          test_deny_canonicalizes_plus_tag_and_case_variants)
    check("deny of a +tag variant also blocks the bare address (defect 2, both directions)",
          test_deny_of_plus_tag_variant_also_blocks_the_bare_address)
    check("deny clears all pending rows sharing a canonical address (defect 2)",
          test_deny_clears_all_pending_rows_sharing_canonical_address)
    check("dots in the local part are NOT canonicalized (defect 2, pinned)",
          test_dots_in_local_part_are_not_canonicalized)
    check("deny requires admin -- authenticated non-admin gets 403 (defect 3)",
          test_deny_requires_admin_403_for_authenticated_non_admin)
    check("undo denial grants NO access and sends NO email (round 3 acceptance criterion)",
          test_undo_denial_grants_no_access_and_sends_no_email)
    check("undo denial clears the whole canonical group, undone via a variant url",
          test_undo_denial_clears_the_whole_canonical_group)
    check("undo denial DELETEs the rows, does not re-status them",
          test_undo_denial_deletes_the_rows_not_restatuses_them)
    check("undo denial does not touch approved/pending rows",
          test_undo_denial_does_not_touch_approved_or_pending_rows)
    check("undo denial is idempotent and does not 404 on a second call",
          test_undo_denial_is_idempotent_and_does_not_404)
    check("undo denial requires admin", test_undo_denial_requires_admin)
    check("denied list groups canonically, shows original addresses (display/match non-swap)",
          test_denied_list_groups_canonically_and_shows_original_addresses)
    check("denied list surfaces the canonical (actually-blocked) address even when "
          "no original matches it (SEC #1 ground truth)",
          test_denied_list_surfaces_the_canonical_address_even_when_no_original_matches_it)
    check("denied list excludes pending/approved addresses",
          test_denied_list_excludes_pending_and_approved)
    check("denied list is empty when nothing is denied",
          test_denied_list_is_empty_when_nothing_is_denied)
    check("denied list requires admin", test_denied_list_requires_admin)
    check("pending list still groups by the raw address, not canonically",
          test_pending_list_still_groups_by_raw_address_not_canonically)
    check("allowlisting clears a denial filed under a variant (fold-in fix 1)",
          test_allowlisting_clears_a_denial_filed_under_a_variant)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL ADMIN-ROUTER TESTS PASSED")


if __name__ == "__main__":
    run()
