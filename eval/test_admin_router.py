"""Admin router contract (app/routers/admin.py): the import pipeline's HTTP
surface (bad extension, single-import lock conflict, a mocked success run,
job listing/detail), the allowlist approval-email failure branch, the usage
dashboard's since>until swap, skills GET/PATCH/DELETE (incl. the `headline`
field and PATCH re-embedding when headline/lesson change), and the
server-logs endpoint.

The heavy importer.run_import is mocked (a fast fake that just marks the job
row 'swapped') and threading.Thread is replaced with a synchronous stand-in so
the "background" job finishes before the request handler returns — no real
loader, mdbtools, or sleep/poll needed. Allowlist add/remove and the
oversized-upload 413 path are already covered by eval/test_backend.py and
eval/test_security.py.
"""
import contextlib
import hashlib
import logging
import os
import sys
import tempfile
from pathlib import Path

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

from app import skills  # noqa: E402
from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import admin as admin_router  # noqa: E402


def _fake_embed(text):
    """Deterministic bag-of-words vector (8 dims, L2-normalized) — mirrors
    eval/test_skills.py's helper, kept local since each eval/ suite is a
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
        assert by_year[2022]["year"] == 2023, by_year[2022]
        assert by_year[2022]["year_label"] == "2022-23", by_year[2022]
        assert by_year[2022]["zip_bytes"] == 100_000_000, by_year[2022]

        assert "disk" in body, body
        disk = body["disk"]
        assert disk["free_bytes"] == 60_000_000_000, disk
        assert disk["total_bytes"] == 100_000_000_000, disk
        assert disk["used_bytes"] == 40_000_000_000, disk

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


def test_allowlist_add_mail_configured_false_when_no_key():
    # This suite's env has RESEND_API_KEY="" -- the baseline "no key" case.
    with TestClient(app) as c:
        _login(c)
        r = c.post("/api/admin/allowlist", json={"email": "nokey@example.edu"})
        assert r.status_code == 200, r.text
        assert r.json()["mail_configured"] is False, r.text


def test_allowlist_add_mail_configured_true_when_key_set():
    with TestClient(app) as c:
        _login(c)
        restore = _patch_resend_key("re_test_key_1234")
        orig_send = admin_router.send_access_approved
        admin_router.send_access_approved = lambda email, link: True
        try:
            r = c.post("/api/admin/allowlist", json={"email": "haskey@example.edu"})
        finally:
            admin_router.send_access_approved = orig_send
            restore()
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mail_configured"] is True, body
        assert body["invited"] is True, body


def test_allowlist_add_invited_false_mail_configured_true_is_reachable():
    """The whole reason mail_configured exists: with a key CONFIGURED, a send
    failure (invited=False) means the link was minted but never printed
    anywhere -- unlike the no-key dev case, where the console has it. Assert
    this exact (invited=False, mail_configured=True) combination is reachable,
    since that's the case the admin UI must react to differently."""
    with TestClient(app) as c:
        _login(c)
        restore = _patch_resend_key("re_test_key_5678")
        orig_send = admin_router.send_access_approved

        def _boom(email, link):
            raise RuntimeError("smtp is down")
        admin_router.send_access_approved = _boom
        try:
            r = c.post("/api/admin/allowlist", json={"email": "keyfails@example.edu"})
        finally:
            admin_router.send_access_approved = orig_send
            restore()
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["invited"] is False, body
        assert body["mail_configured"] is True, body


@contextlib.contextmanager
def _attached_probe_handler():
    """A REAL SqliteLogHandler (same class the app installs on the root
    logger at startup — see app/logbuffer.py:install) attached to the root
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


def test_usage_since_after_until_is_swapped():
    with TestClient(app) as c:
        _login(c)
        r = c.get("/api/admin/usage", params={"since": 2_000_000_000, "until": 1_000_000_000})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["since"] <= body["until"], body


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


def run():
    print("admin router contract:")
    check("import rejects a non-.accdb upload", test_import_rejects_non_accdb_extension)
    check("import conflicts (409) while one is already running",
          test_import_conflicts_while_one_already_running)
    check("import success creates a job and the job appears in listing/detail",
          test_import_success_creates_and_completes_a_job)
    check("import job detail 404s for an unknown job id",
          test_import_job_not_found_404)
    check("import catalog marks integrated/selectable years + zip_bytes/disk/calibration",
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
    check("allowlist add: mail_configured is False when no resend key is set",
          test_allowlist_add_mail_configured_false_when_no_key)
    check("allowlist add: mail_configured is True when a resend key is set",
          test_allowlist_add_mail_configured_true_when_key_set)
    check("allowlist add: invited=False + mail_configured=True is reachable "
          "(send failed WITH a key configured)",
          test_allowlist_add_invited_false_mail_configured_true_is_reachable)
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
    check("usage dashboard swaps since/until when reversed",
          test_usage_since_after_until_is_swapped)
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
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL ADMIN-ROUTER TESTS PASSED")


if __name__ == "__main__":
    run()
