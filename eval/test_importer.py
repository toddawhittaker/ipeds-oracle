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
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@franklin.edu"
os.environ["COOKIE_SECURE"] = "false"
os.environ["OPENROUTER_API_KEY"] = ""
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
    _years,
    build_check_swap,
    create_job,
    integrity_checks,
    preflight,
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
    jid = create_job("IPEDS202526.accdb", "admin@franklin.edu")
    row = _job_row(jid)
    assert row["filename"] == "IPEDS202526.accdb", row
    assert row["status"] == "pending", row
    assert row["created_by"] == "admin@franklin.edu", row
    assert row["created_at"] > 0, row


def test_log_appends_lines_in_order():
    jid = create_job("IPEDS202526.accdb", "admin@franklin.edu")
    _log(jid, "line one")
    _log(jid, "line two")
    row = _job_row(jid)
    assert row["log"] == "line one\nline two\n", repr(row["log"])


def test_set_status_without_report_leaves_report_untouched():
    jid = create_job("IPEDS202526.accdb", "admin@franklin.edu")
    _set_status(jid, "running", "initial report")
    _set_status(jid, "checks")  # no report arg
    row = _job_row(jid)
    assert row["status"] == "checks", row
    assert row["report"] == "initial report", row


def test_set_status_with_report_overwrites():
    jid = create_job("IPEDS202526.accdb", "admin@franklin.edu")
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
    assert "Preflight OK" in msg and "5 tables" in msg, msg


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


def _fake_settings(ipeds_db_path, data_dir, *, nces_total_max_mb=51200):
    """Stand-in for app.config.Settings used across the importer tests.

    Mirrors every nces_* field the real Settings defines (see app/config.py)
    so run_integrate can read them directly, with no getattr/hasattr
    fallback. nces_work_dir is pinned under data_dir's parent — same tmp
    root the test already controls — so run_integrate's temp work dir lands
    (and gets cleaned up) inside the test's own tmpdir, just like the old
    fallback did. nces_total_max_mb is overridable so a test can force the
    union size-cap enforcement path."""
    return types.SimpleNamespace(
        ipeds_db_path=ipeds_db_path,
        data_dir=data_dir,
        nces_work_dir=Path(data_dir).parent / "work",
        nces_http_timeout_seconds=60.0,
        nces_zip_max_mb=512,
        nces_accdb_max_mb=3072,
        nces_total_max_mb=nces_total_max_mb,
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
        jid = create_job(upload.name, "admin@franklin.edu")
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
        jid = create_job(upload.name, "admin@franklin.edu")
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
        jid = create_job(upload.name, "admin@franklin.edu")
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
        jid = create_job(upload.name, "admin@franklin.edu")
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
        jid = create_job(upload.name, "admin@franklin.edu")
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
        jid = create_job(upload.name, "admin@franklin.edu")
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
# build_check_swap — the extracted loader->checks->swap core (used by both
# run_import, above, and run_integrate, below). run_import's existing tests
# already exercise its behavior end-to-end through this seam (they monkeypatch
# importer.subprocess.Popen / importer.integrity_checks as bare module
# globals, which build_check_swap must call the same way for those mocks to
# still take effect) — this just pins that the extracted function exists and
# is directly callable, i.e. the refactor actually happened.
# ---------------------------------------------------------------------------

def test_build_check_swap_is_a_standalone_callable():
    assert callable(build_check_swap), "importer.build_check_swap must exist post-refactor"


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

    def fake_fetch_year(start_year, work_dir):
        fetched.append(start_year)
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"fake")
        return p

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
        jid = create_job("integrate", "admin@franklin.edu")
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

    def fake_fetch_year(start_year, work_dir):
        work_dir_holder["path"] = Path(work_dir)
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"fake")
        return p

    orig_settings = importer.get_settings
    orig_fetch = importer.nces.fetch_year
    orig_swap = importer.build_check_swap
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.nces.fetch_year = fake_fetch_year
    importer.build_check_swap = lambda jid, ddir: None
    try:
        jid = create_job("integrate", "admin@franklin.edu")
        run_integrate(jid, [2025])
    finally:
        importer.get_settings = orig_settings
        importer.nces.fetch_year = orig_fetch
        importer.build_check_swap = orig_swap

    assert "path" in work_dir_holder, "fetch_year was never called"
    assert not work_dir_holder["path"].exists(), \
        "the temp work dir must be removed after a successful build_check_swap"


def test_run_integrate_enforces_total_size_cap():
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _live_with_years(live, [2025])  # already-integrated start year -> {2024}

    fetched = []
    work_dir_holder = {}

    def fake_fetch_year(start_year, work_dir):
        fetched.append(start_year)
        work_dir_holder["path"] = Path(work_dir)
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"fake-bytes-bigger-than-the-cap")
        return p

    swap_called = {"hit": False}

    def fake_build_check_swap(jid, ddir):
        swap_called["hit"] = True

    orig_settings = importer.get_settings
    orig_fetch = importer.nces.fetch_year
    orig_swap = importer.build_check_swap
    # A 0 MB cap means the very first fetched file already exceeds it,
    # regardless of exactly how many bytes the fake file contains.
    importer.get_settings = lambda: _fake_settings(live, data_dir, nces_total_max_mb=0)
    importer.nces.fetch_year = fake_fetch_year
    importer.build_check_swap = fake_build_check_swap
    try:
        jid = create_job("integrate", "admin@franklin.edu")
        # union = sorted({2024} | {2026}) = [2024, 2026]; must abort after
        # the first fetch, before ever fetching the second year.
        run_integrate(jid, [2026])
    finally:
        importer.get_settings = orig_settings
        importer.nces.fetch_year = orig_fetch
        importer.build_check_swap = orig_swap

    assert len(fetched) == 1, \
        f"must abort after the first fetch once the cap is exceeded, got {fetched}"
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

    def fake_fetch_year(start_year, work_dir):
        work_dir_holder["path"] = Path(work_dir)
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        p = Path(work_dir) / f"IPEDS{start_year}{str(start_year + 1)[-2:]}.accdb"
        p.write_bytes(b"fake")
        return p

    def _boom(jid, ddir):
        raise RuntimeError("integrity checks blew up")

    orig_settings = importer.get_settings
    orig_fetch = importer.nces.fetch_year
    orig_swap = importer.build_check_swap
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.nces.fetch_year = fake_fetch_year
    importer.build_check_swap = _boom
    try:
        jid = create_job("integrate", "admin@franklin.edu")
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
    check("build_check_swap exists as a standalone callable (extracted core)",
          test_build_check_swap_is_a_standalone_callable)
    check("run_integrate: union is correct, idempotent, fetches once per year",
          test_run_integrate_union_is_correct_and_idempotent_and_fetches_once_per_year)
    check("run_integrate: cleans up the temp work dir on success",
          test_run_integrate_cleans_up_temp_dir_on_success)
    check("run_integrate: enforces the union total size cap and cleans up",
          test_run_integrate_enforces_total_size_cap)
    check("run_integrate: cleans up the temp work dir when build_check_swap raises",
          test_run_integrate_cleans_up_temp_dir_when_build_check_swap_raises)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL IMPORTER TESTS PASSED")


if __name__ == "__main__":
    run()
