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
import hashlib
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
        assert r.json()["invited"] is False, r.text


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
