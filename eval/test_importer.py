"""Admin data-import pipeline contract (app/importer.py).

Exercises the pure/DB-testable seams without the real 1.9GB ipeds.db or
mdbtools: job row CRUD, the preflight filename/table-probe gate, the
family/year/associate's-total readers against tiny fixture DBs, the
integrity-check report across pass/fail scenarios, the data_dir
restore-on-failure helper, and the full run_import pipeline with
preflight/subprocess/integrity_checks monkeypatched so every branch
(preflight failure, loader failure, checks failure, unexpected exception,
and full success+swap) runs deterministically and fast.
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"
os.environ["COOKIE_SECURE"] = "false"
os.environ["LLM_API_KEY"] = ""
os.environ["RESEND_API_KEY"] = ""

from app import importer  # noqa: E402
from app.db import connect as db_connect  # noqa: E402
from app.db import init_db  # noqa: E402
from app.importer import (  # noqa: E402
    FILENAME_RE,
    _associates_latest,
    _family_counts,
    _log,
    _restore_data_dir,
    _set_status,
    _update_rebuild_progress,
    _years,
    build_check_swap,
    create_job,
    deintegrate_checks,
    integrity_checks,
    preflight,
    run_deintegrate,
    run_import,
    run_integrate,
)

init_db()

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _job_row(job_id):
    con = db_connect()
    try:
        return dict(con.execute("SELECT * FROM import_jobs WHERE id=?",
                                (job_id,)).fetchone())
    finally:
        con.close()


# ---------------------------------------------------------------------------
# create_job / _log / _set_status — plain DB row ops
# ---------------------------------------------------------------------------

def test_create_job_row():
    jid = create_job("IPEDS202526.accdb", "admin@example.edu")
    row = _job_row(jid)
    assert row["filename"] == "IPEDS202526.accdb", row
    assert row["status"] == "pending", row
    assert row["created_by"] == "admin@example.edu", row
    assert row["created_at"] > 0, row


def test_log_appends_lines_in_order():
    jid = create_job("IPEDS202526.accdb", "admin@example.edu")
    _log(jid, "line one")
    _log(jid, "line two")
    row = _job_row(jid)
    assert row["log"] == "line one\nline two\n", repr(row["log"])


def test_set_status_without_report_leaves_report_untouched():
    jid = create_job("IPEDS202526.accdb", "admin@example.edu")
    _set_status(jid, "running", "initial report")
    _set_status(jid, "checks")  # no report arg
    row = _job_row(jid)
    assert row["status"] == "checks", row
    assert row["report"] == "initial report", row


def test_set_status_with_report_overwrites():
    jid = create_job("IPEDS202526.accdb", "admin@example.edu")
    _set_status(jid, "failed", "boom")
    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert row["report"] == "boom", row


# ---------------------------------------------------------------------------
# preflight — filename regex + mocked mdb-tables probe
# ---------------------------------------------------------------------------

def test_filename_regex_accepts_expected_and_rejects_others():
    assert FILENAME_RE.match("IPEDS202526.accdb")
    assert FILENAME_RE.match("ipeds202526.accdb")  # case-insensitive
    assert not FILENAME_RE.match("IPEDS2025.accdb")
    assert not FILENAME_RE.match("data.accdb")
    assert not FILENAME_RE.match("IPEDS202526.mdb")


def test_preflight_rejects_bad_filename_without_touching_subprocess():
    called = {"hit": False}
    orig = importer.subprocess.run
    importer.subprocess.run = lambda *a, **k: called.__setitem__("hit", True)
    try:
        ok, msg = preflight(Path("/tmp/some_other_name.accdb"))
    finally:
        importer.subprocess.run = orig
    assert ok is False, msg
    assert "must match IPEDS" in msg, msg
    assert called["hit"] is False, "subprocess.run must not run for a bad filename"


def test_preflight_no_mdb_tools_installed():
    orig = importer.subprocess.run

    def _raise(*a, **k):
        raise FileNotFoundError("mdb-tables not found")
    importer.subprocess.run = _raise
    try:
        ok, msg = preflight(Path("IPEDS202526.accdb"))
    finally:
        importer.subprocess.run = orig
    assert ok is False, msg
    assert "Could not read the Access file" in msg, msg


def test_preflight_called_process_error():
    import subprocess as sp
    orig = importer.subprocess.run

    def _raise(*a, **k):
        raise sp.CalledProcessError(1, ["mdb-tables"])
    importer.subprocess.run = _raise
    try:
        ok, msg = preflight(Path("IPEDS202526.accdb"))
    finally:
        importer.subprocess.run = orig
    assert ok is False, msg
    assert "Could not read the Access file" in msg, msg


def _fake_run(stdout):
    def _run(*a, **k):
        return types.SimpleNamespace(stdout=stdout, returncode=0)
    return _run


def test_preflight_missing_completions_table():
    orig = importer.subprocess.run
    importer.subprocess.run = _fake_run("HD2024 valueSets vartable")
    try:
        ok, msg = preflight(Path("IPEDS202526.accdb"))
    finally:
        importer.subprocess.run = orig
    assert ok is False, msg
    assert "No Completions" in msg, msg


def test_preflight_missing_hd_table():
    orig = importer.subprocess.run
    importer.subprocess.run = _fake_run("C2024_A valueSets vartable")
    try:
        ok, msg = preflight(Path("IPEDS202526.accdb"))
    finally:
        importer.subprocess.run = orig
    assert ok is False, msg
    assert "No HD" in msg, msg


def test_preflight_success():
    orig = importer.subprocess.run
    importer.subprocess.run = _fake_run("HD2024 C2024_A valueSets vartable EFFY2024")
    try:
        ok, msg = preflight(Path("IPEDS202526.accdb"))
    finally:
        importer.subprocess.run = orig
    assert ok is True, msg
    assert "Preflight OK" in msg, msg


# ---------------------------------------------------------------------------
# _family_counts / _years / _associates_latest — tiny fixture DBs
# ---------------------------------------------------------------------------

def _build_fixture(path, *, family_rows, years, c_a_rows):
    """family_rows: list of (family, n_rows) — may repeat a family (summed).
    years: list of int years for _years.
    c_a_rows: list of (year, ctotalt, awlevel, majornum, cipcode)."""
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE _family_map (src_table TEXT, family TEXT, "
                "survey_year TEXT, year INTEGER, n_rows INTEGER)")
    for fam, n in family_rows:
        con.execute("INSERT INTO _family_map VALUES (?,?,?,?,?)",
                    (fam.upper() + "2024", fam, "2023-24",
                     max(years) if years else 2024, n))
    con.execute("CREATE TABLE _years (survey_year TEXT, year INTEGER PRIMARY KEY)")
    for y in years:
        con.execute("INSERT INTO _years VALUES (?,?)", (f"{y - 1}-{str(y)[2:]}", y))
    con.execute("CREATE TABLE c_a (year INTEGER, ctotalt INTEGER, awlevel INTEGER, "
                "majornum INTEGER, cipcode TEXT)")
    con.executemany("INSERT INTO c_a VALUES (?,?,?,?,?)", c_a_rows)
    con.commit()
    con.close()


def _healthy_db(path, assoc=800_000, years=(2024, 2025)):
    _build_fixture(
        path,
        family_rows=[("c_a", 5000), ("hd", 3000), ("valuesets", 1000),
                    ("vartable", 500)],
        years=list(years),
        c_a_rows=[(max(years), assoc, 3, 1, "99")],
    )


def test_family_counts_sums_across_rows_for_same_family():
    d = Path(tempfile.mkdtemp())
    p = d / "fixture.db"
    _build_fixture(p, family_rows=[("c_a", 3000), ("c_a", 2000), ("hd", 500)],
                   years=[2025], c_a_rows=[(2025, 800_000, 3, 1, "99")])
    fams = _family_counts(p)
    assert fams["c_a"] == 5000, fams
    assert fams["hd"] == 500, fams


def test_years_returns_sorted_list():
    d = Path(tempfile.mkdtemp())
    p = d / "fixture.db"
    _build_fixture(p, family_rows=[("c_a", 100)], years=[2023, 2021, 2022],
                   c_a_rows=[])
    assert _years(p) == [2021, 2022, 2023], _years(p)


def test_associates_latest_returns_sum_for_max_year():
    d = Path(tempfile.mkdtemp())
    p = d / "fixture.db"
    _build_fixture(p, family_rows=[("c_a", 100)], years=[2024, 2025],
                   c_a_rows=[(2025, 500_000, 3, 1, "99"),
                            (2025, 300_000, 3, 1, "99"),
                            (2024, 999_999, 3, 1, "99")])  # older year, ignored
    assert _associates_latest(p) == 800_000, _associates_latest(p)


def test_associates_latest_none_when_no_matching_row():
    d = Path(tempfile.mkdtemp())
    p = d / "fixture.db"
    _build_fixture(p, family_rows=[("c_a", 100)], years=[2025],
                   c_a_rows=[(2025, 800_000, 5, 1, "99")])  # wrong awlevel
    assert _associates_latest(p) is None, _associates_latest(p)


# ---------------------------------------------------------------------------
# integrity_checks — pass/fail scenarios
# ---------------------------------------------------------------------------

def test_integrity_checks_first_build_healthy_passes():
    d = Path(tempfile.mkdtemp())
    staging = d / "staging.db"
    _healthy_db(staging)
    ok, report = integrity_checks(staging, None)
    text = "\n".join(report)
    assert ok is True, text
    assert "required families present" in text, text
    assert "national associate's total" in text and "sane" in text, text
    assert "first build" in text, text


def test_integrity_checks_missing_required_family():
    d = Path(tempfile.mkdtemp())
    staging = d / "staging.db"
    _build_fixture(staging, family_rows=[("c_a", 5000), ("hd", 3000)],
                   years=[2025], c_a_rows=[(2025, 800_000, 3, 1, "99")])
    ok, report = integrity_checks(staging, None)
    text = "\n".join(report)
    assert ok is False, text
    assert "required family/object missing" in text and "vartable" in text, text


def test_integrity_checks_no_years_fails():
    d = Path(tempfile.mkdtemp())
    staging = d / "staging.db"
    _build_fixture(staging,
                   family_rows=[("c_a", 5000), ("hd", 3000), ("valuesets", 1000),
                               ("vartable", 500)],
                   years=[], c_a_rows=[])
    ok, report = integrity_checks(staging, None)
    text = "\n".join(report)
    assert ok is False, text
    assert "no years loaded" in text, text


def test_integrity_checks_assoc_too_low_fails():
    d = Path(tempfile.mkdtemp())
    staging = d / "staging.db"
    _healthy_db(staging, assoc=500_000)
    ok, report = integrity_checks(staging, None)
    text = "\n".join(report)
    assert ok is False, text
    assert "outside sane range" in text, text


def test_integrity_checks_assoc_too_high_fails():
    d = Path(tempfile.mkdtemp())
    staging = d / "staging.db"
    _healthy_db(staging, assoc=1_500_000)
    ok, report = integrity_checks(staging, None)
    text = "\n".join(report)
    assert ok is False, text
    assert "outside sane range" in text, text


def test_integrity_checks_assoc_uncomputable_fails():
    d = Path(tempfile.mkdtemp())
    staging = d / "staging.db"
    _build_fixture(staging,
                   family_rows=[("c_a", 5000), ("hd", 3000), ("valuesets", 1000),
                               ("vartable", 500)],
                   years=[2025], c_a_rows=[(2025, 800_000, 5, 1, "99")])
    ok, report = integrity_checks(staging, None)
    text = "\n".join(report)
    assert ok is False, text
    assert "could not compute national associate's total" in text, text


def test_integrity_checks_stale_year_warns_but_does_not_fail():
    d = Path(tempfile.mkdtemp())
    staging = d / "staging.db"
    live = d / "live.db"
    _healthy_db(staging, assoc=800_000, years=(2024, 2025))
    _healthy_db(live, assoc=800_000, years=(2024, 2025))  # same max year
    ok, report = integrity_checks(staging, live)
    text = "\n".join(report)
    assert ok is True, text
    assert "not newer than" in text, text


def test_integrity_checks_family_shrink_fails():
    d = Path(tempfile.mkdtemp())
    staging = d / "staging.db"
    live = d / "live.db"
    _healthy_db(live, assoc=800_000, years=(2024, 2025))
    # Staging has a healthy new year but c_a shrank >20% vs. live.
    _build_fixture(
        staging,
        family_rows=[("c_a", 1500), ("hd", 3000), ("valuesets", 1000),
                    ("vartable", 500)],
        years=[2024, 2025, 2026],
        c_a_rows=[(2026, 800_000, 3, 1, "99")],
    )
    ok, report = integrity_checks(staging, live)
    text = "\n".join(report)
    assert ok is False, text
    assert "family c_a shrank" in text, text


# ---------------------------------------------------------------------------
# _restore_data_dir — both branches
# ---------------------------------------------------------------------------

def test_restore_data_dir_restores_backup():
    d = Path(tempfile.mkdtemp())
    target = d / "IPEDS202526.accdb"
    backup = d / "IPEDS202526.accdb.bak"
    target.write_bytes(b"new-bad-upload")
    backup.write_bytes(b"previous-good-file")
    _restore_data_dir(target, backup)
    assert target.read_bytes() == b"previous-good-file"
    assert not backup.exists()


def test_restore_data_dir_unlinks_when_no_backup():
    d = Path(tempfile.mkdtemp())
    target = d / "IPEDS202526.accdb"
    target.write_bytes(b"new-bad-upload")
    _restore_data_dir(target, None)
    assert not target.exists()


def test_restore_data_dir_noop_when_nothing_to_do():
    d = Path(tempfile.mkdtemp())
    target = d / "IPEDS202526.accdb"  # doesn't exist
    _restore_data_dir(target, None)  # must not raise
    assert not target.exists()


# ---------------------------------------------------------------------------
# run_import — full pipeline, failure + success branches
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, returncode, lines):
        self.returncode = returncode
        self.stdout = iter(lines)

    def wait(self):
        pass


def _fake_settings(ipeds_db_path, data_dir, *, nces_total_max_mb=51200,
                   nces_accdb_expand_factor=3.0, nces_est_bandwidth_mbps=10.0,
                   nces_est_build_seconds_per_year=60.0, nces_default_per_year_db_mb=380,
                   nces_download_deadline_seconds=1800.0, nces_disk_safety_factor=1.2,
                   nces_probe_concurrency=5, nces_download_concurrency=5):
    """Stand-in for app.config.Settings used across the importer tests.

    Mirrors every nces_* field the real Settings defines (see app/config.py)
    so run_integrate can read them directly, with no getattr/hasattr
    fallback. nces_work_dir is pinned under data_dir's parent — same tmp
    root the test already controls — so run_integrate's temp work dir lands
    (and gets cleaned up) inside the test's own tmpdir, just like the old
    fallback did. nces_total_max_mb is overridable so a test can force the
    union size-cap enforcement path. The eight nces_est_*/nces_disk_*/
    nces_*_concurrency knobs back the disk/time estimator (app/estimate.py)
    and the concurrent probe/download pools — every test that exercises
    run_integrate's disk-headroom check or concurrent fetch path needs these
    present with sane defaults, hence they're kwargs here (not hidden extras)
    so a test can override just the one it cares about."""
    return types.SimpleNamespace(
        ipeds_db_path=ipeds_db_path,
        data_dir=data_dir,
        nces_work_dir=Path(data_dir).parent / "work",
        nces_http_timeout_seconds=60.0,
        nces_zip_max_mb=512,
        nces_accdb_max_mb=3072,
        nces_total_max_mb=nces_total_max_mb,
        nces_accdb_expand_factor=nces_accdb_expand_factor,
        nces_est_bandwidth_mbps=nces_est_bandwidth_mbps,
        nces_est_build_seconds_per_year=nces_est_build_seconds_per_year,
        nces_default_per_year_db_mb=nces_default_per_year_db_mb,
        nces_download_deadline_seconds=nces_download_deadline_seconds,
        nces_disk_safety_factor=nces_disk_safety_factor,
        nces_probe_concurrency=nces_probe_concurrency,
        nces_download_concurrency=nces_download_concurrency,
    )


def _new_upload(d, name="IPEDS202526.accdb", content=b"fake accdb bytes"):
    uploads = d / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    p = uploads / name
    p.write_bytes(content)
    return p


def test_run_import_preflight_failure_no_swap():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    upload = _new_upload(d)

    orig_settings, orig_preflight = importer.get_settings, importer.preflight
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (False, "bad file, rejected")
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, upload)
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert "bad file, rejected" in (row["report"] or ""), row
    assert not live.exists(), "live db must not be created on preflight failure"


def test_run_import_loader_failure_restores_data_dir():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    upload = _new_upload(d)
    staging = live.with_name("ipeds_staging.db")

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = lambda *a, **k: _FakeProc(1, ["loader output line"])
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, upload)
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert "Loader exited with code 1" in (row["report"] or ""), row
    assert "loader output line" in (row["log"] or ""), row
    assert not (data_dir / upload.name).exists(), "staged upload must be removed"
    assert not staging.exists()
    assert not live.exists()


def test_run_import_integrity_checks_failure_no_swap():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    upload = _new_upload(d)
    staging = live.with_name("ipeds_staging.db")

    def _fake_popen(*a, **k):
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"staged-build-output")
        return _FakeProc(0, ["build ok"])

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    orig_checks = importer.integrity_checks
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = _fake_popen
    importer.integrity_checks = lambda staging_, live_: (False, ["✗ bad magnitude"])
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, upload)
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen
        importer.integrity_checks = orig_checks

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert "Integrity checks FAILED" in (row["report"] or ""), row
    assert "✗ bad magnitude" in (row["report"] or ""), row
    assert not staging.exists(), "staging db must be removed on checks failure"
    assert not live.exists(), "live db must not be touched on checks failure"


def test_run_import_unexpected_exception_is_caught():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    upload = _new_upload(d)

    def _boom(*a, **k):
        raise RuntimeError("disk exploded")

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = _boom
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, upload)
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert "Unexpected error" in (row["report"] or "") and "disk exploded" in row["report"], row
    assert "ERROR: RuntimeError" in (row["log"] or ""), row


def test_run_import_backs_up_existing_staged_accdb():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    upload = _new_upload(d, content=b"new-upload-bytes")
    # A same-named .accdb already sitting in data_dir from a previous import.
    data_dir.mkdir(parents=True, exist_ok=True)
    existing = data_dir / upload.name
    existing.write_bytes(b"previous-accdb-bytes")

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    # Fail fast right after the staging copy so we don't need a fake loader.
    importer.subprocess.Popen = lambda *a, **k: _FakeProc(1, ["loader output"])
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, upload)
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen

    # On failure, _restore_data_dir puts the previous .accdb back in place.
    assert existing.read_bytes() == b"previous-accdb-bytes", \
        "previous staged .accdb was not restored after failure"
    assert not (data_dir / (upload.name + ".bak")).exists()


def test_run_import_success_swaps_and_bumps_data_version():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    upload = _new_upload(d)
    staging = live.with_name("ipeds_staging.db")
    live.write_bytes(b"old-live-content")  # simulate an existing live db

    def _fake_popen(*a, **k):
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"new-staging-content")
        return _FakeProc(0, ["build line 1", "build line 2"])

    # Seed data_version + a query_cache row so we can prove the bump + the
    # invalidate_cache() call for real (no mocking needed - it's cheap).
    con = db_connect()
    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('data_version','1')")
    con.execute("INSERT INTO query_cache(question, data_version, created_at) "
                "VALUES ('old question', 1, 0)")
    con.commit()
    con.close()

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    orig_checks = importer.integrity_checks
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = _fake_popen
    importer.integrity_checks = lambda staging_, live_: (True, ["✓ all good"])
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, upload)
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen
        importer.integrity_checks = orig_checks

    row = _job_row(jid)
    assert row["status"] == "swapped", row
    assert "✓ all good" in (row["report"] or ""), row
    assert live.read_bytes() == b"new-staging-content", "live db was not swapped"
    prev = live.with_suffix(".db.prev")
    assert prev.read_bytes() == b"old-live-content", "previous live was not backed up"
    assert not staging.exists()

    con = db_connect()
    try:
        dv = con.execute("SELECT value FROM meta WHERE key='data_version'").fetchone()[0]
        assert dv == "2", f"data_version not bumped: {dv}"
        n_cache = con.execute("SELECT COUNT(*) FROM query_cache").fetchone()[0]
        assert n_cache == 0, "semantic cache was not invalidated"
    finally:
        con.close()


# ---------------------------------------------------------------------------
# run_integrate — NCES year-catalog batch integration
# ---------------------------------------------------------------------------

def _live_with_years(path, years):
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE _years (survey_year TEXT, year INTEGER PRIMARY KEY)")
    for y in years:
        con.execute("INSERT INTO _years VALUES (?,?)", (f"{y - 1}-{str(y)[2:]}", y))
    con.commit()
    con.close()


def test_run_integrate_union_is_correct_and_idempotent_and_fetches_once_per_year():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    # live already has years 2024, 2025 -> already-integrated start_years {2023, 2024}.
    _live_with_years(live, [2024, 2025])

    fetched = []
    fetched_lock = threading.Lock()

    def fake_fetch_year(start_year, work_dir, on_progress=None):
        with fetched_lock:
            fetched.append(start_year)
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"fake")
        return p, "Final"

    swap_calls = []

    def fake_build_check_swap(jid, ddir):
        swap_calls.append((jid, str(ddir)))

    orig_settings = importer.get_settings
    orig_fetch = importer.nces.fetch_year
    orig_swap = importer.build_check_swap
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.nces.fetch_year = fake_fetch_year
    importer.build_check_swap = fake_build_check_swap
    try:
        jid = create_job("integrate", "admin@example.edu")
        # Selecting 2024 (already integrated -> must not duplicate) and 2025 (new).
        run_integrate(jid, [2024, 2025])
    finally:
        importer.get_settings = orig_settings
        importer.nces.fetch_year = orig_fetch
        importer.build_check_swap = orig_swap

    assert sorted(fetched) == [2023, 2024, 2025], fetched
    assert len(swap_calls) == 1, swap_calls
    assert swap_calls[0][0] == jid, swap_calls


def test_run_integrate_cleans_up_temp_dir_on_success():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _live_with_years(live, [2025])

    work_dir_holder = {}

    def fake_fetch_year(start_year, work_dir, on_progress=None):
        work_dir_holder["path"] = Path(work_dir)
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"fake")
        return p, "Final"

    orig_settings = importer.get_settings
    orig_fetch = importer.nces.fetch_year
    orig_swap = importer.build_check_swap
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.nces.fetch_year = fake_fetch_year
    importer.build_check_swap = lambda jid, ddir: None
    try:
        jid = create_job("integrate", "admin@example.edu")
        run_integrate(jid, [2025])
    finally:
        importer.get_settings = orig_settings
        importer.nces.fetch_year = orig_fetch
        importer.build_check_swap = orig_swap

    assert "path" in work_dir_holder, "fetch_year was never called"
    assert not work_dir_holder["path"].exists(), \
        "the temp work dir must be removed after a successful build_check_swap"


def test_run_integrate_enforces_total_size_cap():
    # NOTE: fetch_year now runs CONCURRENTLY (a thread pool, width
    # nces_download_concurrency), so this no longer asserts an exact fetch
    # count of 1 — several fetches may legitimately be in flight before the
    # shared running total is noticed to have exceeded the cap. What must
    # still hold, deterministically, regardless of thread scheduling: the
    # cap trips (the fake sizes sum well past it), the job ends 'failed'
    # with a cap-related message, build_check_swap never runs, the live db
    # is untouched, and the temp work dir is still cleaned up.
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _live_with_years(live, [2025])  # already-integrated start year -> {2024}

    fetched = []
    fetched_lock = threading.Lock()
    work_dir_holder = {}

    def fake_fetch_year(start_year, work_dir, on_progress=None):
        with fetched_lock:
            fetched.append(start_year)
        work_dir_holder["path"] = Path(work_dir)
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"fake-bytes-bigger-than-the-cap")
        return p, "Final"

    swap_called = {"hit": False}

    def fake_build_check_swap(jid, ddir):
        swap_called["hit"] = True

    orig_settings = importer.get_settings
    orig_fetch = importer.nces.fetch_year
    orig_swap = importer.build_check_swap
    # A 0 MB cap means every fetched file — individually or summed — already
    # exceeds it, regardless of exactly how many bytes the fake file
    # contains or how many fetches race ahead of the cap check.
    importer.get_settings = lambda: _fake_settings(live, data_dir, nces_total_max_mb=0)
    importer.nces.fetch_year = fake_fetch_year
    importer.build_check_swap = fake_build_check_swap
    try:
        jid = create_job("integrate", "admin@example.edu")
        # union = sorted({2024} | {2026}) = [2024, 2026].
        run_integrate(jid, [2026])
    finally:
        importer.get_settings = orig_settings
        importer.nces.fetch_year = orig_fetch
        importer.build_check_swap = orig_swap

    assert len(fetched) >= 1, "at least one year must have been fetched"
    assert swap_called["hit"] is False, \
        "build_check_swap must never run once the union size cap is exceeded"
    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert "cap" in (row["report"] or "").lower(), row
    assert "path" in work_dir_holder, "fetch_year was never called"
    assert not work_dir_holder["path"].exists(), \
        "the temp work dir must still be cleaned up after a size-cap abort"


def test_run_integrate_cleans_up_temp_dir_when_build_check_swap_raises():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _live_with_years(live, [2025])

    work_dir_holder = {}

    def fake_fetch_year(start_year, work_dir, on_progress=None):
        work_dir_holder["path"] = Path(work_dir)
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"fake")
        return p, "Final"

    def _boom(jid, ddir):
        raise RuntimeError("integrity checks blew up")

    orig_settings = importer.get_settings
    orig_fetch = importer.nces.fetch_year
    orig_swap = importer.build_check_swap
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.nces.fetch_year = fake_fetch_year
    importer.build_check_swap = _boom
    try:
        jid = create_job("integrate", "admin@example.edu")
        run_integrate(jid, [2026])  # must not raise back out to the caller
    finally:
        importer.get_settings = orig_settings
        importer.nces.fetch_year = orig_fetch
        importer.build_check_swap = orig_swap

    assert "path" in work_dir_holder, "fetch_year was never called"
    assert not work_dir_holder["path"].exists(), \
        "the temp work dir must be removed even when build_check_swap raises"
    row = _job_row(jid)
    assert row["status"] == "failed", row


def test_run_integrate_fetch_failure_of_newly_selected_year_preserves_wording():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _live_with_years(live, [2025])  # already-integrated start year -> {2024}

    def fake_fetch_year(start_year, work_dir, on_progress=None):
        if start_year == 2026:  # the newly-selected year
            raise RuntimeError("NCES returned a 500")
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"fake")
        return p, "Final"

    orig_settings = importer.get_settings
    orig_fetch = importer.nces.fetch_year
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.nces.fetch_year = fake_fetch_year
    try:
        jid = create_job("integrate", "admin@example.edu")
        run_integrate(jid, [2026])
    finally:
        importer.get_settings = orig_settings
        importer.nces.fetch_year = orig_fetch

    row = _job_row(jid)
    assert row["status"] == "failed", row
    report = row["report"] or ""
    assert "newly-selected" in report, report
    assert "2026-27" in report, report
    assert "Live database unchanged" in report, report
    assert live.exists(), "live db must survive a fetch failure"


def test_run_integrate_fetch_failure_of_already_integrated_year_preserves_wording():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _live_with_years(live, [2025])  # already-integrated start year -> {2024}

    def fake_fetch_year(start_year, work_dir, on_progress=None):
        if start_year == 2024:  # already integrated
            raise RuntimeError("NCES withdrew the file")
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"fake")
        return p, "Final"

    orig_settings = importer.get_settings
    orig_fetch = importer.nces.fetch_year
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.nces.fetch_year = fake_fetch_year
    try:
        jid = create_job("integrate", "admin@example.edu")
        run_integrate(jid, [2026])
    finally:
        importer.get_settings = orig_settings
        importer.nces.fetch_year = orig_fetch

    row = _job_row(jid)
    assert row["status"] == "failed", row
    report = row["report"] or ""
    assert "already-integrated" in report, report
    assert "2024-25" in report, report


# ---------------------------------------------------------------------------
# Disk-headroom preflight refusal — run_integrate must compute the
# needed-vs-free estimate BEFORE fetching anything, and refuse (fail the job,
# never call fetch_year or build_check_swap, leave the live db + work dir
# untouched) when free space is insufficient. shutil.disk_usage is
# monkeypatched as a bare module attribute on importer.shutil, mirroring the
# subprocess.Popen/preflight convention used throughout this file.
# ---------------------------------------------------------------------------

def test_run_integrate_refuses_when_disk_headroom_insufficient():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _live_with_years(live, [2024, 2025])
    original_live_bytes = live.read_bytes()

    fetch_called = {"hit": False}

    def fake_fetch_year(start_year, work_dir, on_progress=None):
        fetch_called["hit"] = True
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"should never be fetched")
        return p, "Final"

    swap_called = {"hit": False}

    def fake_build_check_swap(jid, ddir):
        swap_called["hit"] = True
        return True

    def fake_disk_usage(path):
        # Effectively no free space at all: whatever the estimator computes
        # as "needed", 1 byte free can never cover it.
        return types.SimpleNamespace(total=1_000_000_000_000,
                                     used=999_999_999_999, free=1)

    orig_settings = importer.get_settings
    orig_disk_usage = importer.shutil.disk_usage
    orig_fetch = importer.nces.fetch_year
    orig_swap = importer.build_check_swap
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.shutil.disk_usage = fake_disk_usage
    importer.nces.fetch_year = fake_fetch_year
    importer.build_check_swap = fake_build_check_swap
    try:
        jid = create_job("integrate", "admin@example.edu")
        run_integrate(jid, [2026])
    finally:
        importer.get_settings = orig_settings
        importer.shutil.disk_usage = orig_disk_usage
        importer.nces.fetch_year = orig_fetch
        importer.build_check_swap = orig_swap

    assert fetch_called["hit"] is False, \
        "fetch_year must never run when the disk-headroom preflight refuses"
    assert swap_called["hit"] is False, \
        "build_check_swap must never run when the disk-headroom preflight refuses"
    row = _job_row(jid)
    assert row["status"] == "failed", row
    report = (row["report"] or "").lower()
    assert "disk" in report or "space" in report, row["report"]
    assert live.read_bytes() == original_live_bytes, "live db must be untouched"
    work_dir = Path(data_dir).parent / "work" / f"integrate_{jid}"
    assert not work_dir.exists(), "the temp work dir must not be left behind"


def test_run_integrate_proceeds_when_disk_headroom_sufficient():
    # The mirror-image check: an ample disk_usage must NOT trip the refusal —
    # otherwise the preflight would be a false-positive block on every run.
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _live_with_years(live, [2025])

    def fake_fetch_year(start_year, work_dir, on_progress=None):
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"fake")
        return p, "Final"

    swap_called = {"hit": False}

    def fake_build_check_swap(jid, ddir):
        swap_called["hit"] = True
        return True

    def fake_disk_usage(path):
        return types.SimpleNamespace(total=10_000_000_000_000,
                                     used=1_000_000_000_000,
                                     free=9_000_000_000_000)  # 9 TB free

    orig_settings = importer.get_settings
    orig_disk_usage = importer.shutil.disk_usage
    orig_fetch = importer.nces.fetch_year
    orig_swap = importer.build_check_swap
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.shutil.disk_usage = fake_disk_usage
    importer.nces.fetch_year = fake_fetch_year
    importer.build_check_swap = fake_build_check_swap
    try:
        jid = create_job("integrate", "admin@example.edu")
        run_integrate(jid, [2026])
    finally:
        importer.get_settings = orig_settings
        importer.shutil.disk_usage = orig_disk_usage
        importer.nces.fetch_year = orig_fetch
        importer.build_check_swap = orig_swap

    assert swap_called["hit"] is True, \
        "build_check_swap must run when there's ample disk headroom"


# ---------------------------------------------------------------------------
# run_integrate — structured per-year JSON progress (import_jobs.progress)
# ---------------------------------------------------------------------------

def _progress(job_id):
    row = _job_row(job_id)
    raw = row["progress"]
    assert raw, "import_jobs.progress must be populated"
    return json.loads(raw)


def test_run_integrate_writes_progress_json_reaching_done_on_success():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _live_with_years(live, [2025])  # already-integrated -> {2024}; select 2026

    def fake_fetch_year(start_year, work_dir, on_progress=None):
        if on_progress is not None:
            on_progress(1000, 2000)
            on_progress(2000, 2000)
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"fake")
        return p, "Final"

    orig_settings = importer.get_settings
    orig_fetch = importer.nces.fetch_year
    orig_swap = importer.build_check_swap
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.nces.fetch_year = fake_fetch_year
    importer.build_check_swap = lambda jid, ddir: True
    try:
        jid = create_job("integrate", "admin@example.edu")
        run_integrate(jid, [2026])
    finally:
        importer.get_settings = orig_settings
        importer.nces.fetch_year = orig_fetch
        importer.build_check_swap = orig_swap

    progress = _progress(jid)
    assert "overall" in progress and "years" in progress, progress
    assert progress["overall"]["phase"] == "done", progress["overall"]
    assert "message" in progress["overall"], progress["overall"]

    years = progress["years"]
    assert set(years.keys()) == {"2024", "2026"}, years
    for sy_str, entry in years.items():
        for key in ("start_year", "year_label", "step",
                   "downloaded_bytes", "total_bytes", "pct"):
            assert key in entry, f"year {sy_str} entry missing {key!r}: {entry}"
        assert entry["step"] in (
            "queued", "downloading", "extracting", "fetched", "failed"), entry
        assert entry["start_year"] == int(sy_str), entry
    # both years succeeded -> both should have reached a post-fetch step.
    assert all(e["step"] in ("fetched", "extracting") for e in years.values()), years


def test_run_integrate_writes_progress_json_reaching_failed_on_error():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _live_with_years(live, [2025])  # already-integrated -> {2024}; select 2026

    def fake_fetch_year(start_year, work_dir, on_progress=None):
        if start_year == 2026:
            raise RuntimeError("boom")
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"fake")
        return p, "Final"

    orig_settings = importer.get_settings
    orig_fetch = importer.nces.fetch_year
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.nces.fetch_year = fake_fetch_year
    try:
        jid = create_job("integrate", "admin@example.edu")
        run_integrate(jid, [2026])
    finally:
        importer.get_settings = orig_settings
        importer.nces.fetch_year = orig_fetch

    progress = _progress(jid)
    assert progress["overall"]["phase"] == "failed", progress["overall"]
    years = progress["years"]
    assert years["2026"]["step"] == "failed", years["2026"]


# ---------------------------------------------------------------------------
# Provenance (app.db year_provenance) — written only after a successful swap.
# run_import: source='manual', release=NULL. run_integrate: source='nces',
# release taken from each fetched year's actual release.
# ---------------------------------------------------------------------------

def _provenance_rows():
    con = db_connect()
    try:
        rows = con.execute(
            "SELECT start_year, end_year, release, source FROM year_provenance "
            "ORDER BY start_year").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def test_run_import_records_manual_provenance_on_success():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    upload = _new_upload(d)  # IPEDS202526.accdb -> start_year 2025, end_year 2026
    staging = live.with_name("ipeds_staging.db")

    def _fake_popen(*a, **k):
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"new-staging-content")
        return _FakeProc(0, ["build ok"])

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    orig_checks = importer.integrity_checks
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = _fake_popen
    importer.integrity_checks = lambda staging_, live_: (True, ["✓ all good"])
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, upload)
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen
        importer.integrity_checks = orig_checks

    assert _job_row(jid)["status"] == "swapped"
    rows = [r for r in _provenance_rows() if r["start_year"] == 2025]
    assert len(rows) == 1, _provenance_rows()
    row = rows[0]
    assert row["end_year"] == 2026, row
    assert row["release"] is None, row
    assert row["source"] == "manual", row


def test_run_import_no_provenance_written_on_preflight_failure():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    upload = _new_upload(d, name="IPEDS209899.accdb")  # a start_year unlikely to collide

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (False, "bad file, rejected")
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, upload)
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight

    assert _job_row(jid)["status"] == "failed"
    assert not any(r["start_year"] == 2098 for r in _provenance_rows()), _provenance_rows()


def test_run_integrate_records_nces_provenance_for_every_union_year_on_success():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _live_with_years(live, [2024, 2025])  # already-integrated -> {2023, 2024}

    releases = {2023: "Final", 2024: "Final", 2020: "Provisional"}

    def fake_fetch_year(start_year, work_dir, on_progress=None):
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"fake")
        return p, releases[start_year]

    orig_settings = importer.get_settings
    orig_fetch = importer.nces.fetch_year
    orig_swap = importer.build_check_swap
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.nces.fetch_year = fake_fetch_year
    importer.build_check_swap = lambda jid, ddir: True
    try:
        jid = create_job("integrate", "admin@example.edu")
        # union = sorted({2023,2024} | {2020}) = [2020, 2023, 2024]
        run_integrate(jid, [2020])
    finally:
        importer.get_settings = orig_settings
        importer.nces.fetch_year = orig_fetch
        importer.build_check_swap = orig_swap

    rows = {r["start_year"]: r for r in _provenance_rows()
           if r["start_year"] in (2020, 2023, 2024)}
    assert set(rows) == {2020, 2023, 2024}, rows
    for sy, expected_release in releases.items():
        assert rows[sy]["release"] == expected_release, rows[sy]
        assert rows[sy]["source"] == "nces", rows[sy]
        assert rows[sy]["end_year"] == sy + 1, rows[sy]


def test_run_integrate_no_provenance_written_when_swap_fails():
    # This suite shares ONE real app.db across the whole process (see
    # scripts/coverage_check.sh / run_ci_local.sh, which run this file's
    # run() as a single process) — other tests in this file legitimately
    # write real year_provenance rows for years 2020-2026 on their own
    # successful swaps. A blanket "no row exists at all for these
    # start_years" assertion is therefore order-dependent and NOT a valid
    # test of THIS test's behavior. Instead: (1) snapshot whatever rows
    # already exist for 2024/2099 before this run, and assert they are
    # BYTE-IDENTICAL after (nothing added or modified for these years), and
    # (2) use a release string no other test ever writes, so even if some
    # future test coincidentally shares these start_years, a regression that
    # writes provenance on a FAILED swap is still caught unambiguously.
    SENTINEL_RELEASE = "SENTINEL-NEVER-RECORDED-ON-FAILED-SWAP"

    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _live_with_years(live, [2025])  # already-integrated -> {2024}

    before = {r["start_year"]: r for r in _provenance_rows() if r["start_year"] in (2024, 2099)}

    def fake_fetch_year(start_year, work_dir, on_progress=None):
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"fake")
        return p, SENTINEL_RELEASE

    orig_settings = importer.get_settings
    orig_fetch = importer.nces.fetch_year
    orig_swap = importer.build_check_swap
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.nces.fetch_year = fake_fetch_year
    importer.build_check_swap = lambda jid, ddir: False  # a handled failure, no swap
    try:
        jid = create_job("integrate", "admin@example.edu")
        run_integrate(jid, [2099])
    finally:
        importer.get_settings = orig_settings
        importer.nces.fetch_year = orig_fetch
        importer.build_check_swap = orig_swap

    after = {r["start_year"]: r for r in _provenance_rows() if r["start_year"] in (2024, 2099)}
    assert after == before, (
        "run_integrate must not add or modify any year_provenance row for "
        f"2024/2099 when build_check_swap fails: before={before}, after={after}")
    assert not any(r["release"] == SENTINEL_RELEASE for r in _provenance_rows()), \
        "the sentinel release must never have been recorded anywhere on a failed swap"


# ---------------------------------------------------------------------------
# run_deintegrate / deintegrate_checks — remove-an-integrated-year ("trashcan")
#
# Fixture covers every table shape run_deintegrate must touch: c_a/hd/
# valuesets/vartable (each carrying a `year` column, enumerated via
# sqlite_master + PRAGMA table_info), the bookkeeping tables _years/
# _family_map (also carry `year`), and the year-LESS _column_presence (whose
# `years` field is a CSV of survey_year tokens, e.g. "2023-24,2024-25" — a
# token is removed from each row, and a row whose CSV becomes empty is
# dropped entirely). `years` passed to the fixture builder are DB `_years.year`
# END years; the corresponding start_year is always year-1, matching the
# loader's own survey_year convention (see scripts/build_ipeds_db.py
# discover_files/derive_family).
# ---------------------------------------------------------------------------

def _deintegrate_fixture(path, *, years, assoc_by_year=None, include_column_presence=True):
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE _years (survey_year TEXT, year INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE _family_map (src_table TEXT, family TEXT, "
                "survey_year TEXT, year INTEGER, n_rows INTEGER)")
    con.execute("CREATE TABLE c_a (year INTEGER, ctotalt INTEGER, awlevel INTEGER, "
                "majornum INTEGER, cipcode TEXT)")
    con.execute("CREATE TABLE hd (unitid INTEGER, year INTEGER)")
    con.execute("CREATE TABLE valuesets (tablename TEXT, varname TEXT, "
                "codevalue TEXT, year INTEGER)")
    con.execute("CREATE TABLE vartable (varname TEXT, datatype TEXT, year INTEGER)")
    if include_column_presence:
        con.execute("CREATE TABLE _column_presence (family TEXT, column_name TEXT, years TEXT)")

    assoc_by_year = assoc_by_year or {}
    survey_years = []
    for y in years:
        sy = y - 1
        token = f"{sy}-{str(y)[2:]}"
        survey_years.append(token)
        con.execute("INSERT INTO _years VALUES (?,?)", (token, y))
        assoc = assoc_by_year.get(y, 800_000)
        con.execute("INSERT INTO c_a VALUES (?,?,?,?,?)", (y, assoc, 3, 1, "99"))
        con.execute("INSERT INTO c_a VALUES (?,?,?,?,?)", (y, 3000, 1, 1, "01.0000"))
        for i in range(3):
            con.execute("INSERT INTO hd VALUES (?,?)", (100_000 + i + y, y))
        con.execute("INSERT INTO valuesets VALUES (?,?,?,?)", ("C_A", "AWLEVEL", "3", y))
        con.execute("INSERT INTO vartable VALUES (?,?,?)", ("awlevel", "N", y))
        for fam, n in (("c_a", 8000), ("hd", 3000), ("valuesets", 1000), ("vartable", 500)):
            con.execute("INSERT INTO _family_map VALUES (?,?,?,?,?)",
                        (fam.upper() + str(y), fam, token, y, n))
    if include_column_presence:
        # A column present in every seeded year, and one present ONLY in the
        # first (soon-to-be-removed, in the tests below) year — the latter
        # row must vanish entirely once its only token is stripped.
        con.execute("INSERT INTO _column_presence VALUES (?,?,?)",
                    ("c_a", "ctotalt", ",".join(survey_years)))
        con.execute("INSERT INTO _column_presence VALUES (?,?,?)",
                    ("c_a", "only_in_first_year", survey_years[0]))
    con.commit()
    con.close()


def _seed_provenance_row(start_year, end_year, release="Final", source="nces"):
    con = db_connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO year_provenance"
            "(start_year, end_year, release, source, updated_at) VALUES (?,?,?,?,0)",
            (start_year, end_year, release, source))
        con.commit()
    finally:
        con.close()


def _provenance_row_exists(start_year):
    con = db_connect()
    try:
        return con.execute("SELECT 1 FROM year_provenance WHERE start_year=?",
                           (start_year,)).fetchone() is not None
    finally:
        con.close()


def _data_version():
    con = db_connect()
    try:
        row = con.execute("SELECT value FROM meta WHERE key='data_version'").fetchone()
        return int(row[0]) if row else 1
    finally:
        con.close()


def _ample_disk_usage(path):
    return types.SimpleNamespace(total=1_000_000_000_000, used=100_000_000_000,
                                 free=900_000_000_000)


def _tiny_disk_usage(path):
    return types.SimpleNamespace(total=1_000_000_000_000, used=999_999_999_999, free=1)


def test_run_deintegrate_happy_path_removes_year_and_swaps():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _deintegrate_fixture(live, years=[2024, 2025])  # remove start 2023, keep 2024
    original_live_bytes = live.read_bytes()
    removed_start_year = 2023
    surviving_end_year = 2025

    _seed_provenance_row(removed_start_year, removed_start_year + 1)
    dv_before = _data_version()

    orig_settings = importer.get_settings
    orig_disk_usage = importer.shutil.disk_usage
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.shutil.disk_usage = _ample_disk_usage
    try:
        jid = create_job(f"deintegrate:{removed_start_year}", "admin@example.edu")
        run_deintegrate(jid, removed_start_year)
    finally:
        importer.get_settings = orig_settings
        importer.shutil.disk_usage = orig_disk_usage

    row = _job_row(jid)
    assert row["status"] == "swapped", row
    assert live.read_bytes() != original_live_bytes, "live db must have been swapped"

    assert _years(live) == [surviving_end_year], _years(live)

    con = sqlite3.connect(live)
    try:
        removed_fam_rows = con.execute(
            "SELECT COUNT(*) FROM _family_map WHERE year=?",
            (removed_start_year + 1,)).fetchone()[0]
        assert removed_fam_rows == 0, "removed year's _family_map rows must be gone"
        surviving_fam_rows = con.execute(
            "SELECT COUNT(*) FROM _family_map WHERE year=?",
            (surviving_end_year,)).fetchone()[0]
        assert surviving_fam_rows == 4, surviving_fam_rows  # c_a/hd/valuesets/vartable

        cp = dict(con.execute(
            "SELECT column_name, years FROM _column_presence").fetchall())
        assert "only_in_first_year" not in cp, \
            "a _column_presence row whose CSV became empty must be deleted"
        assert cp["ctotalt"] == "2024-25", cp  # lost the removed year's token only
    finally:
        con.close()

    assert not _provenance_row_exists(removed_start_year), \
        "year_provenance row for the removed start year must be deleted"
    assert _data_version() == dv_before + 1, (dv_before, _data_version())

    staging = live.with_name("ipeds_staging.db")
    assert not staging.exists(), "staging db must be removed after the swap"
    assert live.with_suffix(".db.prev").exists(), \
        "a .db.prev backup of the pre-removal live db must exist"


def test_run_deintegrate_refuses_removing_the_only_integrated_year():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _deintegrate_fixture(live, years=[2025])
    original_live_bytes = live.read_bytes()

    orig_settings = importer.get_settings
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    try:
        jid = create_job("deintegrate:2024", "admin@example.edu")
        run_deintegrate(jid, 2024)
    finally:
        importer.get_settings = orig_settings

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert "only" in (row["report"] or "").lower(), row
    assert live.read_bytes() == original_live_bytes, "live db must be untouched"
    assert not live.with_name("ipeds_staging.db").exists()


def test_run_deintegrate_refuses_a_non_integrated_year():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _deintegrate_fixture(live, years=[2024, 2025])
    original_live_bytes = live.read_bytes()

    orig_settings = importer.get_settings
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    try:
        jid = create_job("deintegrate:2030", "admin@example.edu")
        run_deintegrate(jid, 2030)  # end_year 2031 was never integrated
    finally:
        importer.get_settings = orig_settings

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert "not integrated" in (row["report"] or "").lower(), row
    assert live.read_bytes() == original_live_bytes, "live db must be untouched"


def test_run_deintegrate_refuses_when_disk_headroom_insufficient():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _deintegrate_fixture(live, years=[2024, 2025])
    original_live_bytes = live.read_bytes()

    orig_settings = importer.get_settings
    orig_disk_usage = importer.shutil.disk_usage
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.shutil.disk_usage = _tiny_disk_usage
    try:
        jid = create_job("deintegrate:2023", "admin@example.edu")
        run_deintegrate(jid, 2023)
    finally:
        importer.get_settings = orig_settings
        importer.shutil.disk_usage = orig_disk_usage

    row = _job_row(jid)
    assert row["status"] == "failed", row
    report = (row["report"] or "").lower()
    assert "disk" in report or "space" in report, row["report"]
    assert live.read_bytes() == original_live_bytes, "live db must be untouched"
    assert not live.with_name("ipeds_staging.db").exists(), \
        "no staging file must be left behind on a disk-headroom refusal"


def test_deintegrate_checks_fails_if_removed_year_still_present():
    d = Path(tempfile.mkdtemp())
    live = d / "live.db"
    staging = d / "staging.db"
    _deintegrate_fixture(live, years=[2024, 2025])
    _deintegrate_fixture(staging, years=[2024, 2025])  # "removal" that removed nothing
    ok, report = deintegrate_checks(staging, live, 2024)
    text = "\n".join(report)
    assert ok is False, text


def test_deintegrate_checks_passes_for_a_healthy_removal():
    d = Path(tempfile.mkdtemp())
    live = d / "live.db"
    staging = d / "staging.db"
    _deintegrate_fixture(live, years=[2024, 2025])
    _deintegrate_fixture(staging, years=[2025])  # 2024 correctly removed
    ok, report = deintegrate_checks(staging, live, 2024)
    text = "\n".join(report)
    assert ok is True, text


# ---------------------------------------------------------------------------
# build_check_swap — ##PROGRESS## marker parsing into progress["rebuild"]
# (the rebuild progress bar). Marker lines must be parsed for
# tables_total=/tables_done= and NEVER written into the human-readable log;
# non-marker lines are logged exactly as before.
# ---------------------------------------------------------------------------

def test_update_rebuild_progress_computes_pct_and_preserves_siblings():
    jid = create_job("integrate", "admin@example.edu")
    importer._set_progress(jid, {
        "overall": {"phase": "downloading", "message": "x"},
        "years": {"2024": {"start_year": 2024}},
    })

    _update_rebuild_progress(jid, tables_total=4, tables_done=0)
    p = json.loads(_job_row(jid)["progress"])
    assert p["rebuild"] == {"tables_total": 4, "tables_done": 0, "pct": 0}, p
    assert p["overall"]["phase"] == "downloading", p  # sibling preserved
    assert p["years"]["2024"]["start_year"] == 2024, p  # sibling preserved

    _update_rebuild_progress(jid, tables_total=4, tables_done=2)
    p = json.loads(_job_row(jid)["progress"])
    assert p["rebuild"] == {"tables_total": 4, "tables_done": 2, "pct": 50}, p

    _update_rebuild_progress(jid, tables_total=3, tables_done=1)
    p = json.loads(_job_row(jid)["progress"])
    assert p["rebuild"] == {"tables_total": 3, "tables_done": 1, "pct": 33}, p


def test_build_check_swap_parses_progress_markers_and_keeps_them_out_of_the_log():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    live.write_bytes(b"old-live-content")
    staging = live.with_name("ipeds_staging.db")

    def _fake_popen(*a, **k):
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"new-staging-content")
        lines = [
            "Found 1 files: 2024-25",
            "##PROGRESS## tables_total=3",
            "  loaded C2024_A                 -> c_a                     5000 rows",
            "##PROGRESS## tables_done=1",
            "  loaded HD2024                  -> hd                      3000 rows",
            "##PROGRESS## tables_done=2",
            "  loaded valueSets2024           -> valuesets                1000 rows",
            "##PROGRESS## tables_done=3",
        ]
        return _FakeProc(0, lines)

    orig_settings = importer.get_settings
    orig_popen = importer.subprocess.Popen
    orig_checks = importer.integrity_checks
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.subprocess.Popen = _fake_popen
    importer.integrity_checks = lambda staging_, live_: (True, ["✓ all good"])
    try:
        jid = create_job("integrate", "admin@example.edu")
        build_check_swap(jid, data_dir)
    finally:
        importer.get_settings = orig_settings
        importer.subprocess.Popen = orig_popen
        importer.integrity_checks = orig_checks

    row = _job_row(jid)
    progress = json.loads(row["progress"])
    assert progress["rebuild"] == {"tables_total": 3, "tables_done": 3, "pct": 100}, progress

    log = row["log"] or ""
    assert "##PROGRESS##" not in log, log
    assert "loaded C2024_A" in log, log
    assert "loaded HD2024" in log, log
    assert "loaded valueSets2024" in log, log


def run():
    print("importer contract:")
    check("create_job writes a pending row", test_create_job_row)
    check("_log appends lines in order", test_log_appends_lines_in_order)
    check("_set_status without report leaves report untouched",
          test_set_status_without_report_leaves_report_untouched)
    check("_set_status with report overwrites it",
          test_set_status_with_report_overwrites)
    check("FILENAME_RE accepts IPEDS{YYYY}{YY}.accdb, rejects others",
          test_filename_regex_accepts_expected_and_rejects_others)
    check("preflight rejects bad filename without calling subprocess",
          test_preflight_rejects_bad_filename_without_touching_subprocess)
    check("preflight handles missing mdb-tools (FileNotFoundError)",
          test_preflight_no_mdb_tools_installed)
    check("preflight handles mdb-tables CalledProcessError",
          test_preflight_called_process_error)
    check("preflight rejects a file with no Completions (C…_A) table",
          test_preflight_missing_completions_table)
    check("preflight rejects a file with no HD table",
          test_preflight_missing_hd_table)
    check("preflight succeeds when both required tables are present",
          test_preflight_success)
    check("_family_counts sums n_rows across rows for the same family",
          test_family_counts_sums_across_rows_for_same_family)
    check("_years returns years sorted ascending", test_years_returns_sorted_list)
    check("_associates_latest sums ctotalt for the max year only",
          test_associates_latest_returns_sum_for_max_year)
    check("_associates_latest is None with no matching grand-total row",
          test_associates_latest_none_when_no_matching_row)
    check("integrity_checks: healthy first build passes",
          test_integrity_checks_first_build_healthy_passes)
    check("integrity_checks: missing required family fails",
          test_integrity_checks_missing_required_family)
    check("integrity_checks: no years loaded fails",
          test_integrity_checks_no_years_fails)
    check("integrity_checks: associate's total too low fails",
          test_integrity_checks_assoc_too_low_fails)
    check("integrity_checks: associate's total too high fails",
          test_integrity_checks_assoc_too_high_fails)
    check("integrity_checks: uncomputable associate's total fails",
          test_integrity_checks_assoc_uncomputable_fails)
    check("integrity_checks: stale year warns but doesn't fail",
          test_integrity_checks_stale_year_warns_but_does_not_fail)
    check("integrity_checks: family shrinking >20% fails",
          test_integrity_checks_family_shrink_fails)
    check("_restore_data_dir restores the backed-up file",
          test_restore_data_dir_restores_backup)
    check("_restore_data_dir unlinks the staged file with no backup",
          test_restore_data_dir_unlinks_when_no_backup)
    check("_restore_data_dir is a no-op when there's nothing to restore",
          test_restore_data_dir_noop_when_nothing_to_do)
    check("run_import: preflight failure fails the job, no swap",
          test_run_import_preflight_failure_no_swap)
    check("run_import: loader failure restores the data dir",
          test_run_import_loader_failure_restores_data_dir)
    check("run_import: integrity-checks failure leaves live db untouched",
          test_run_import_integrity_checks_failure_no_swap)
    check("run_import: unexpected exception is caught and reported",
          test_run_import_unexpected_exception_is_caught)
    check("run_import: backs up a pre-existing staged .accdb of the same name",
          test_run_import_backs_up_existing_staged_accdb)
    check("run_import: success swaps db, bumps data_version, clears cache",
          test_run_import_success_swaps_and_bumps_data_version)
    check("run_integrate: union is correct, idempotent, fetches once per year",
          test_run_integrate_union_is_correct_and_idempotent_and_fetches_once_per_year)
    check("run_integrate: cleans up the temp work dir on success",
          test_run_integrate_cleans_up_temp_dir_on_success)
    check("run_integrate: enforces the union total size cap and cleans up",
          test_run_integrate_enforces_total_size_cap)
    check("run_integrate: cleans up the temp work dir when build_check_swap raises",
          test_run_integrate_cleans_up_temp_dir_when_build_check_swap_raises)
    check("run_integrate: fetch failure of a newly-selected year preserves wording",
          test_run_integrate_fetch_failure_of_newly_selected_year_preserves_wording)
    check("run_integrate: fetch failure of an already-integrated year preserves wording",
          test_run_integrate_fetch_failure_of_already_integrated_year_preserves_wording)
    check("run_integrate: refuses (no fetch/swap) when disk headroom is insufficient",
          test_run_integrate_refuses_when_disk_headroom_insufficient)
    check("run_integrate: proceeds normally when disk headroom is sufficient",
          test_run_integrate_proceeds_when_disk_headroom_sufficient)
    check("run_integrate: writes progress JSON reaching phase=done on success",
          test_run_integrate_writes_progress_json_reaching_done_on_success)
    check("run_integrate: writes progress JSON reaching phase=failed on error",
          test_run_integrate_writes_progress_json_reaching_failed_on_error)
    check("run_import: records manual provenance (source=manual, release=NULL) on success",
          test_run_import_records_manual_provenance_on_success)
    check("run_import: writes no provenance row on preflight failure",
          test_run_import_no_provenance_written_on_preflight_failure)
    check("run_integrate: records nces provenance for every union year on success",
          test_run_integrate_records_nces_provenance_for_every_union_year_on_success)
    check("run_integrate: writes no provenance when the swap fails",
          test_run_integrate_no_provenance_written_when_swap_fails)
    check("run_deintegrate: happy path removes the year and swaps",
          test_run_deintegrate_happy_path_removes_year_and_swaps)
    check("run_deintegrate: refuses removing the only integrated year",
          test_run_deintegrate_refuses_removing_the_only_integrated_year)
    check("run_deintegrate: refuses a non-integrated year",
          test_run_deintegrate_refuses_a_non_integrated_year)
    check("run_deintegrate: refuses when disk headroom is insufficient",
          test_run_deintegrate_refuses_when_disk_headroom_insufficient)
    check("deintegrate_checks: fails if the removed year is still present",
          test_deintegrate_checks_fails_if_removed_year_still_present)
    check("deintegrate_checks: passes for a healthy removal",
          test_deintegrate_checks_passes_for_a_healthy_removal)
    check("_update_rebuild_progress computes pct and preserves sibling progress keys",
          test_update_rebuild_progress_computes_pct_and_preserves_siblings)
    check("build_check_swap parses ##PROGRESS## markers, keeps them out of the log",
          test_build_check_swap_parses_progress_markers_and_keeps_them_out_of_the_log)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL IMPORTER TESTS PASSED")


if __name__ == "__main__":
    run()
