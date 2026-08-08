"""Admin data-import pipeline contract (backend/app/importer.py).

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


def test_restore_data_dir_leaves_an_existing_target_alone_when_no_backup_was_taken():
    """Direct unit test of the new middle branch _restore_data_dir needs:
    backup is None (the move-aside never completed, or was never attempted)
    but existed_before is True (something was already sitting at data_target
    when this job started). Both existing _restore_data_dir tests above keep
    passing with no `existed_before` argument at all (it must be added as a
    KEYWORD-ONLY parameter with a default, never a new positional one) — this
    test is the one that actually exercises the new branch: no restore is
    possible (there's no backup to move back) and no removal is correct
    either (this job's own copy never overwrote it, since the move-aside that
    would have preceded it never completed) — the target must be left
    exactly as it was. Regression for the destructive middle case: without
    `existed_before`, `_restore_data_dir(target, None)` today takes the
    `elif data_target.exists(): unlink` branch and DELETES a file this job
    never touched."""
    d = Path(tempfile.mkdtemp())
    target = d / "IPEDS202526.accdb"
    target.write_bytes(b"untouched-original-bytes")
    try:
        importer._restore_data_dir(target, None, existed_before=True)
    except TypeError as e:
        raise AssertionError(
            "_restore_data_dir must accept an `existed_before` keyword-only "
            "argument distinguishing 'the move-aside never completed, the "
            "original is still sitting under its own name' from 'nothing "
            "was there before, only this job could have put a file there': "
            f"{e}") from e
    assert target.exists(), \
        "an existing target with no backup and existed_before=True must be " \
        "left alone, not deleted"
    assert target.read_bytes() == b"untouched-original-bytes", \
        "the existing target's bytes changed even though nothing should " \
        "have restored or removed it"


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

    Mirrors every nces_* field the real Settings defines (see backend/app/config.py)
    so run_integrate can read them directly, with no getattr/hasattr
    fallback. nces_work_dir is pinned under data_dir's parent — same tmp
    root the test already controls — so run_integrate's temp work dir lands
    (and gets cleaned up) inside the test's own tmpdir, just like the old
    fallback did. nces_total_max_mb is overridable so a test can force the
    union size-cap enforcement path. The eight nces_est_*/nces_disk_*/
    nces_*_concurrency knobs back the disk/time estimator (backend/app/estimate.py)
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
        run_import(jid, [upload])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert "bad file, rejected" in (row["report"] or ""), row
    assert not live.exists(), "live db must not be created on preflight failure"
    # Preflight returns before data_dir is ever created, so the resolve()
    # comparison the upload-discard guard makes (up.resolve() vs
    # data_dir/up.name) is non-strict here — data_dir doesn't exist to
    # resolve a real path under. Pin that the upload is still discarded on
    # this early-exit path regardless.
    assert not upload.exists(), \
        "the uploaded copy was left on disk after a preflight failure"


def test_discard_uploads_does_not_delete_when_identity_cannot_be_proven():
    """Pins the CALLER half of PR #297's fail-closed rule, not just the
    helper. test_same_file_none_for_an_unstattable_path (above) already pins
    that _same_file itself can answer None; nothing pinned that
    _discard_uploads actually branches on that answer rather than treating
    it as "not aliased, safe to delete" — and that's exactly the kind of
    guard that regresses silently, since every OTHER test in this file
    drives _same_file through real filesystem state and never makes it
    return None, so a caller-side regression (e.g. collapsing `if same is
    None: ...continue` into the False branch) would still pass everything
    else here.

    Monkeypatches importer._same_file to unconditionally return None and
    drives the cheapest real path into _discard_uploads (a preflight
    failure, which runs in run_import's `finally` before data_dir even
    exists) — so this exercises exactly the None branch regardless of what
    the real identity check would say for these paths. Asserts on the
    FILESYSTEM (the upload is still there), not the warning _discard_uploads
    also logs — the log line is incidental, non-deletion is the contract.

    VERIFIED BY HAND that this fails if the None branch fell through to the
    unlink: edited a scratch copy of importer.py's _discard_uploads (never
    the repo file) to `same = False` whenever `_same_file(...)` returned
    None instead of `continue`-ing past the unlink, ran this exact scenario
    against that mutated copy via a sys.path swap, and confirmed the upload
    was deleted (`upload.exists()` came back False) — then reran the same
    scenario against the real, unmutated importer.py and confirmed the
    upload survived. So this assertion actually depends on the fail-closed
    branch and isn't vacuously true either way."""
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    upload = _new_upload(d)

    orig_settings, orig_preflight = importer.get_settings, importer.preflight
    orig_same_file = importer._same_file
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (False, "bad file, rejected")
    importer._same_file = lambda a, b: None
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, [upload])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer._same_file = orig_same_file

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert upload.exists(), \
        ("_discard_uploads deleted the uploaded copy even though _same_file "
         "returned None (identity unprovable) — PR #297's fail-closed rule "
         "requires leaving an upload with unprovable identity on disk, not "
         "treating 'can't tell' as 'safe to delete'")


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
        run_import(jid, [upload])
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
        run_import(jid, [upload])
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
        run_import(jid, [upload])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert "Unexpected error" in (row["report"] or "") and "disk exploded" in row["report"], row
    assert "ERROR: RuntimeError" in (row["log"] or ""), row


def test_run_import_refuses_dropping_a_live_year():
    # The superset guard: a manual rebuild uses EXACTLY the .accdb in data_dir,
    # so it must not drop a year the live DB currently has. Regression for the
    # post-online-only footgun (data/ is empty, a single-year upload would
    # otherwise rebuild a 1-year DB). Must fail BEFORE the loader runs, leaving
    # the live DB untouched.
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    _live_with_years(live, [2021, 2022])  # ending years -> 2020-21 + 2021-22 live
    upload = _new_upload(d, name="IPEDS202021.accdb")  # only 2020-21; drops 2021-22

    called = {"popen": False}

    def _spy_popen(*a, **k):
        called["popen"] = True
        return _FakeProc(0, [])

    orig_settings, orig_preflight = importer.get_settings, importer.preflight
    orig_popen = importer.subprocess.Popen
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = _spy_popen
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, [upload])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert "would DROP" in (row["report"] or "") and "2021-22" in row["report"], row
    assert not called["popen"], "the loader must never run once the guard refuses"
    assert live.exists(), "live DB must be untouched when the guard refuses"
    assert not (data_dir / upload.name).exists(), "the staged upload must be rolled back"


def test_run_import_multi_file_success_records_all_provenance():
    # Multiple .accdb dropped at once: all stage, the rebuild runs on the union,
    # and provenance is recorded for EVERY uploaded year. No live DB = a first
    # build, so the superset guard is a no-op.
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    staging = live.with_name("ipeds_staging.db")
    up1 = _new_upload(d, name="IPEDS202021.accdb")  # start 2020
    up2 = _new_upload(d, name="IPEDS202122.accdb")  # start 2021

    def _fake_popen(*a, **k):
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"built")
        return _FakeProc(0, ["build ok"])

    def _fake_activate_staging(job_id, st, on_swapped=None):
        # Models the real swap closely enough for this test: removes the
        # staging file (the real thing moves it into place) AND fires the
        # on_swapped callback the way _activate_staging will once it calls
        # it right after its own shutil.move — a fake that only did the
        # unlink would silently stop exercising the swapped-flag plumbing
        # the moment build_check_swap starts passing it a callback.
        st.unlink(missing_ok=True)
        if on_swapped is not None:
            on_swapped()

    orig = (importer.get_settings, importer.preflight, importer.subprocess.Popen,
            importer.integrity_checks, importer._activate_staging)
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = _fake_popen
    importer.integrity_checks = lambda s_, l_: (True, ["✓ ok"])
    importer._activate_staging = _fake_activate_staging
    try:
        jid = create_job("2 files", "admin@example.edu")
        run_import(jid, [up1, up2])
    finally:
        (importer.get_settings, importer.preflight, importer.subprocess.Popen,
         importer.integrity_checks, importer._activate_staging) = orig

    row = _job_row(jid)
    assert row["status"] == "swapped", row
    start_years = {p["start_year"] for p in _provenance_rows()}
    assert {2020, 2021} <= start_years, start_years
    assert (data_dir / "IPEDS202021.accdb").exists() and (data_dir / "IPEDS202122.accdb").exists()


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
        run_import(jid, [upload])
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
        run_import(jid, [upload])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen
        importer.integrity_checks = orig_checks

    row = _job_row(jid)
    assert row["status"] == "swapped", row
    assert "✓ all good" in (row["report"] or ""), row
    assert live.read_bytes() == b"new-staging-content", "live db was not swapped"
    # The moved-aside copy exists only to make the two-step move recoverable;
    # once staging is in place it is deleted, because ipeds.db is ~2 GB and
    # rebuildable. Keeping it doubled the dataset on disk, forever.
    assert not live.with_suffix(".db.prev").exists(), \
        "the previous live database was left on disk after a successful swap"
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
# run_import — leftover .accdb cleanup (upload_dir copy + data_dir .bak)
#
# A successful import currently leaks TWO full 1-3 GB Access files forever:
# the streamed upload_dir copy (admin.py only unlinks it on an UPLOAD
# failure, never after run_import's own pipeline finishes) and any
# pre-existing same-named data_dir/<name>.accdb.bak backup (run_import moves
# it aside before the rebuild but never removes it on success). The
# data_dir/<name>.accdb file itself is NOT leaked storage — it's the loader's
# live source, re-globbed by scripts/build_ipeds_db.py on every future
# rebuild — so every test here that asserts a deletion also asserts that
# THIS file survives, to pin the fix against over-deleting the dataset.
# ---------------------------------------------------------------------------

def test_run_import_success_removes_the_uploaded_copy_and_the_backup():
    """Regression for the leaked-.accdb bug (admin.py:832/importer.py:683 +
    :679-681): a successful import must not leave the streamed upload_dir
    copy OR the pre-existing data_dir .bak backup on disk forever — each is a
    1-3 GB Access file, and nothing today ever removes either of them on the
    success path. The third assertion (the data_dir/<name>.accdb loader
    source still holds the NEW upload's bytes) is what would catch a fix that
    over-deletes and destroys the live dataset instead of just the leaked
    upload/backup copies."""
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    upload = _new_upload(d, content=b"new-upload-bytes")
    # A same-named .accdb already sitting in data_dir from a previous import
    # -> run_import backs it up to data_dir/<name>.accdb.bak before staging.
    data_dir.mkdir(parents=True, exist_ok=True)
    existing = data_dir / upload.name
    existing.write_bytes(b"previous-accdb-bytes")
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
        run_import(jid, [upload])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen
        importer.integrity_checks = orig_checks

    row = _job_row(jid)
    assert row["status"] == "swapped", row
    assert not upload.exists(), \
        f"the uploaded copy {upload} was left on disk after a successful import"
    assert not (data_dir / (upload.name + ".bak")).exists(), \
        "the pre-existing .bak backup was left on disk after a successful import"
    assert (data_dir / upload.name).read_bytes() == b"new-upload-bytes", \
        ("the loader's data_dir source .accdb must survive a successful import "
         "unchanged (holding the new upload's bytes) — only the leaked "
         "upload/backup copies should ever be removed")


def test_run_import_a_failed_import_still_removes_the_uploaded_copy():
    """Regression: the upload_dir copy must be discarded on EVERY exit path,
    not just success. Before this fix, admin.py:847-849 only unlinked the
    upload inside its OWN except block — i.e. only when the upload/stream
    itself failed — so a later pipeline failure inside run_import (a loader
    crash, here) still left the full uploaded .accdb on disk forever."""
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    upload = _new_upload(d)

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = lambda *a, **k: _FakeProc(1, ["loader output line"])
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, [upload])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert not upload.exists(), \
        f"the uploaded copy {upload} was left on disk after a FAILED import"


def test_run_import_never_deletes_the_upload_when_upload_dir_equals_data_dir():
    """Guard for the discard-uploads fix: config.py places no validator
    between upload_dir and data_dir (config.py:90-91), so a self-hoster CAN
    point UPLOAD_DIR at the same directory as data_dir. In that
    configuration the streamed 'upload' IS the loader's live source file for
    that name, and a naive unconditional discard would delete it — silently
    destroying the dataset scripts/build_ipeds_db.py needs for every future
    rebuild. The fix must skip any upload where up.resolve() equals the
    would-be data_dir/<name>.accdb target."""
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    upload = data_dir / "IPEDS202526.accdb"
    upload.write_bytes(b"fake accdb bytes")
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
        run_import(jid, [upload])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen
        importer.integrity_checks = orig_checks

    row = _job_row(jid)
    assert row["status"] == "swapped", row
    assert upload.exists() and upload.read_bytes() == b"fake accdb bytes", \
        ("the dataset's own .accdb was deleted when upload_dir == data_dir — "
         "the fix must special-case this instead of unconditionally discarding "
         "every uploaded path")


def test_run_import_a_failed_import_does_not_delete_the_dataset_when_upload_dir_equals_data_dir():
    """Regression for the FAILURE-path twin of the aliasing bug above. When
    UPLOAD_DIR == DATA_DIR, the staging loop's existing guards correctly skip
    both the .bak-aside move and the copy2 (up.resolve() == data_target.
    resolve()), so this job changes nothing about the file on disk — but
    staged.append((data_target, None)) ran unconditionally regardless, so
    the aliased file was recorded in `staged` as though the job HAD staged
    it. On a FAILED import, _restore_all() then calls
    _restore_data_dir(target, None), whose `elif data_target.exists()`
    branch unlinks it outright — deleting the admin's own dataset file,
    which this job never created and never modified, on nothing more than a
    loader crash. The next import is then wrongly refused by the superset
    guard, since data_dir now has one fewer year than it should. Existence
    is asserted before the byte comparison so a deleted file fails with a
    legible message instead of read_bytes() raising FileNotFoundError."""
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    upload = data_dir / "IPEDS202526.accdb"
    upload.write_bytes(b"the admins own dataset file")

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = lambda *a, **k: _FakeProc(1, ["loader output line"])
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, [upload])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert upload.exists(), \
        ("the admin's own .accdb (upload_dir == data_dir) was deleted by a "
         "FAILED import's rollback, even though this job never staged or "
         "modified it — staged.append must not record an aliased file as "
         "something this job changed")
    assert upload.read_bytes() == b"the admins own dataset file", \
        "the dataset file's bytes changed after a failed import that should " \
        "have touched nothing in this configuration"


# ---------------------------------------------------------------------------
# _same_file — inode-identity aliasing, replacing the two Path.resolve()
# string comparisons above (importer.py's staging loop and _discard_uploads).
# Path.resolve() normalises symlinks and ".." but NOT case, and never
# considers inode identity, so two names for one file (a differently-cased
# path on a case-insensitive filesystem, or any other alias reaching the
# same inode by a different string) compare unequal. A case-insensitive
# filesystem can't be mounted in CI; a hard link is the portable proxy —
# os.link gives two path strings, resolve()-distinct, for one inode, which is
# exactly the condition the string compare gets wrong. These tests reference
# `importer._same_file` via module-attribute access (never imported at
# module level) so the whole file still collects and every OTHER test in it
# still runs before the fix lands — an AttributeError is converted to a
# clear AssertionError, the same pattern test_access_gate.py's
# `auth_mod.is_denied` guard and test_admin_router.py's `denied_resp` check
# already use in this codebase for a not-yet-implemented symbol.
# ---------------------------------------------------------------------------

def _not_yet_implemented(name, e):
    raise AssertionError(
        f"app.importer.{name} does not exist yet — this test can only run "
        f"once the fix adds it: {e}") from e


def test_same_file_true_for_a_hardlink_in_another_directory():
    """Regression for the string-identity aliasing bug at both importer.py
    call sites: os.link the SAME inode into a second directory under a
    different name, so the two resolve() strings differ even though it is
    one file. This must fail against TODAY's code (no _same_file exists —
    caught below as a clear AttributeError-derived assertion) AND against a
    normcase-only fix — os.path.normcase is the identity function on POSIX,
    so it wouldn't make these two strings equal either, and a test that
    can't tell the two apart would let a wrong fix through. It passes only
    once _same_file compares real os.stat()+os.path.samestat identity."""
    d = Path(tempfile.mkdtemp())
    dir_a = d / "a"
    dir_b = d / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    original = dir_a / "IPEDS202526.accdb"
    original.write_bytes(b"same file, two names")
    linked = dir_b / "uploaded.accdb"
    os.link(original, linked)
    assert original.resolve() != linked.resolve(), \
        "test setup error: the two paths must differ as strings for this to be a real test"
    try:
        result = importer._same_file(original, linked)
    except AttributeError as e:
        _not_yet_implemented("_same_file", e)
    assert result is True, \
        (f"_same_file must recognize a hard link reached from another "
         f"directory as the same file, got {result!r}")


def test_same_file_true_through_a_symlinked_directory():
    """The behaviour Path.resolve() already had and must not lose: a file
    reached through a symlinked directory is the same file as reached
    directly (importer.py's own comment: 'this also catches a symlinked
    upload_dir'). Not a hard-link proxy — this drives the real symlink case
    resolve() always handled correctly, so _same_file must keep handling it
    too, not just the new hard-link case."""
    d = Path(tempfile.mkdtemp())
    real_dir = d / "real"
    real_dir.mkdir()
    target = real_dir / "IPEDS202526.accdb"
    target.write_bytes(b"reached two ways")
    link_dir = d / "linked"
    os.symlink(real_dir, link_dir)
    via_symlink = link_dir / "IPEDS202526.accdb"
    try:
        result = importer._same_file(target, via_symlink)
    except AttributeError as e:
        _not_yet_implemented("_same_file", e)
    assert result is True, \
        (f"_same_file must still recognize a file reached through a "
         f"symlinked directory as the same file, got {result!r}")


def test_same_file_false_for_two_distinct_files():
    """The false-success bug a blanket .casefold()/normcase-style fix would
    introduce: two genuinely DIFFERENT files, even with byte-identical
    content, must never compare equal — or a real UPLOAD_DIR=/data/accdb,
    DATA_DIR=/data/AccDB pair on a case-SENSITIVE filesystem would be
    declared aliased, the staging loop would skip the real copy, and the job
    would report success while the live rebuild still ran off the OLD
    file."""
    d = Path(tempfile.mkdtemp())
    a = d / "IPEDS202526.accdb"
    b = d / "IPEDS202021.accdb"
    a.write_bytes(b"identical content")
    b.write_bytes(b"identical content")
    try:
        result = importer._same_file(a, b)
    except AttributeError as e:
        _not_yet_implemented("_same_file", e)
    assert result is False, \
        f"_same_file must not treat two distinct files as the same file, got {result!r}"


def test_same_file_false_when_the_candidate_does_not_exist():
    """PR #297's fail-closed rule for the upload-discard guard, pinned so a
    future edit can't quietly widen 'unprovable' to cover this: a MISSING
    candidate must answer False, not None. A nonexistent path definitely
    is NOT the file being compared against, and _discard_uploads must still
    discard the uploaded copy on the preflight-failure exit — where
    data_dir has not been created yet, so the would-be data_dir/<name>
    target never exists. Answering None there instead of False would make
    the caller skip the delete (None means 'can't tell, don't risk it') and
    leak every upload on that exit path."""
    d = Path(tempfile.mkdtemp())
    existing = d / "IPEDS202526.accdb"
    existing.write_bytes(b"the upload")
    missing = d / "does-not-exist" / "IPEDS202526.accdb"
    try:
        result = importer._same_file(existing, missing)
    except AttributeError as e:
        _not_yet_implemented("_same_file", e)
    assert result is False, \
        (f"_same_file must answer False (not None) when a candidate path "
         f"doesn't exist, got {result!r}")


def test_same_file_none_for_an_unstattable_path():
    """The fail-closed 'unprovable' case, distinct from 'missing' above: a
    symlink loop makes os.stat raise OSError(errno ELOOP) — Path.resolve()
    used to raise RuntimeError for the very same thing, which is exactly why
    _discard_uploads' current comment says its resolve()-based catch
    wouldn't have helped even wrapped around the unlink alone. Neither
    caller may treat 'can't prove it' as a green light for the destructive
    branch, so this must come back None (not False, and not True) so both
    call sites keep falling through to their existing fail-closed
    handling."""
    d = Path(tempfile.mkdtemp())
    loop = d / "loop"
    os.symlink(loop, loop)
    existing = d / "IPEDS202526.accdb"
    existing.write_bytes(b"the upload")
    try:
        result = importer._same_file(existing, loop)
    except AttributeError as e:
        _not_yet_implemented("_same_file", e)
    assert result is None, \
        f"_same_file must answer None for an unstattable path (fail closed), got {result!r}"


def test_run_import_does_not_delete_the_dataset_when_the_upload_is_a_hardlink_of_it():
    """End-to-end regression for the string-identity aliasing bug at BOTH
    importer.py call sites (the staging loop's `data_target.resolve() ==
    up.resolve()` and _discard_uploads' `up.resolve() == (data_dir /
    up.name).resolve()`). A case-insensitive filesystem (macOS APFS, NTFS, a
    case-insensitive bind mount) can't be mounted in CI, so this uses the
    same portable proxy as the _same_file unit tests above: os.link the
    dataset already sitting in data_dir into upload_dir under the SAME name,
    giving two resolve()-distinct path strings for one inode.

    VERIFIED against today's actual code first (with shutil.move/copy2/
    _unlink_quietly instrumented to print every call it made): the described
    "_unlink_quietly deletes the dataset source" outcome does NOT reproduce
    through the full run_import pipeline on this ext4/Linux filesystem, and
    the reason is structural, not incidental — a hard link is two
    INDEPENDENT directory entries, so unlinking one (_discard_uploads' own
    `up.unlink`) can never remove the other (data_target) as long as they
    are still two separate entries, which they still are by the time
    _discard_uploads runs. shutil.copy2 in the staging loop always runs
    BEFORE either name is ever removed, so the dataset's BYTES survive today
    purely by accident of that ordering — an existence/byte assertion alone
    cannot tell today's buggy code apart from the fix, and would read as
    already passing (confirmed by tracing an actual run). A genuinely
    case-insensitive filesystem has no such safety net: there is only ONE
    directory entry there, so moving it aside under one spelling makes the
    OTHER spelling stop resolving too — os.link's two independent entries
    are a deliberately weaker stand-in for that, not an exact reproduction,
    which is why this test pins something narrower but still real and still
    red today.

    What the missed alias DOES still provably cause on this filesystem: the
    staging loop fails to recognize data_target and up as one file, so it
    needlessly moves the dataset aside to a .bak and re-copies the upload
    over it — burning a FRESH inode for a file that never needed to move at
    all. That is the direct, mechanical fingerprint of the missed alias, and
    it is exactly what _same_file's fix (recognizing the hard link and
    skipping the backup/copy dance entirely) changes: the dataset's inode is
    preserved. Pinned on both counts — the dataset must still exist with its
    original bytes (the safety net from the task description, already true
    today, kept here as a regression guard against a fix that over-deletes)
    AND its inode must be UNCHANGED (the part that is actually red today)."""
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    upload_dir = d / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    name = "IPEDS202526.accdb"
    dataset = data_dir / name
    dataset.write_bytes(b"the-real-dataset-bytes")
    upload = upload_dir / name
    os.link(dataset, upload)  # same inode, two resolve()-distinct path strings
    orig_ino = os.stat(dataset).st_ino
    assert dataset.resolve() != upload.resolve(), \
        "test setup error: the two paths must differ as strings for this to be a real test"

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
        run_import(jid, [upload])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen
        importer.integrity_checks = orig_checks

    row = _job_row(jid)
    assert row["status"] == "swapped", row
    assert dataset.exists(), \
        "the dataset source in data_dir was deleted by a successful import " \
        "of its own hard-linked alias"
    assert dataset.read_bytes() == b"the-real-dataset-bytes", \
        "the dataset source's bytes changed after a successful import of its own hard-linked alias"
    assert os.stat(dataset).st_ino == orig_ino, (
        "the staging loop's string-compare aliasing guard missed a "
        "same-inode alias reached by two different path strings, so it "
        "needlessly moved the dataset aside to a .bak and re-copied the "
        "upload over it (burning a fresh inode) instead of recognizing the "
        "upload as the file it already had staged")


def test_discard_uploads_removes_a_hardlinked_upload_in_a_separate_directory():
    """Regression the _same_file fix itself introduced: the staging loop and
    _discard_uploads ask two DIFFERENT questions, and _same_file's fix
    answered both the same way. The staging loop asks "does data_target
    already hold exactly this content?" — INODE identity, correctly
    skipping the copy for a hard link (the sibling test above). But
    _discard_uploads asks "will unlinking `up` remove the loader's own
    source ENTRY?" — DIRECTORY-ENTRY identity: a hard link is two
    independent entries, so unlinking `up` provably cannot touch
    data_target, and it SHOULD be unlinked. Using inode identity there
    instead (as landed) makes _discard_uploads treat ANY hard-linked upload
    as aliased and skip the delete — so an operator whose ingest pipeline
    hard-links .accdb files into UPLOAD_DIR (rather than posting through
    the HTTP handler) gets every import leaving its upload behind forever:
    exactly the leak PR #297 shipped to close, reopened for that
    configuration.

    Both operands of the entry-identity question are built from the same
    basename (`s.data_dir / up.name`), so the basenames are identical by
    construction and the real question collapses to "is up.parent the same
    directory as data_dir?" — i.e. _same_file(up.parent, s.data_dir).

    VERIFIED against today's actual code by tracing a real run: with the
    dataset hard-linked into a SEPARATE upload_dir under the same name
    (same setup as the sibling test above, which deliberately never asserts
    on the upload's own survival — this is why), `_same_file(data_dir /
    up.name, up)` reports True (same inode, ignoring which directory either
    path is in), so `if same: continue` skips the delete and the upload is
    still on disk after the job completes. This test is that exact
    reproduction, pinning the OTHER half of the contract: the upload ENTRY
    must be gone while the dataset survives untouched. Together with the
    sibling test (which pins the dataset must never be lost), this covers
    both directions of what "aliased" has to mean at this call site."""
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    upload_dir = d / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    name = "IPEDS202526.accdb"
    dataset = data_dir / name
    dataset.write_bytes(b"the-real-dataset-bytes")
    upload = upload_dir / name
    os.link(dataset, upload)  # same inode, SEPARATE directory entries

    orig_settings, orig_preflight = importer.get_settings, importer.preflight
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (False, "bad file, rejected")
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, [upload])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert not upload.exists(), (
        "the hard-linked upload entry in upload_dir was left on disk — "
        "_discard_uploads must ask whether unlinking `up` would remove the "
        "loader's own source ENTRY (directory identity: is up.parent the "
        "same directory as data_dir?), not whether `up` merely shares an "
        "INODE with it; a hard link in a separate directory can always be "
        "safely unlinked without touching data_dir's copy")
    assert dataset.exists(), "the dataset in data_dir must survive regardless"
    assert dataset.read_bytes() == b"the-real-dataset-bytes", \
        "the dataset's bytes must be untouched"


def test_run_import_stages_a_second_bak_for_a_hardlinked_data_target_in_one_batch():
    """The duplicate-target guard (`if resolved in staged_targets: continue`,
    five lines after the _same_file fix above, in the staging loop) still
    keys `staged_targets` on `data_target.resolve()` — a STRING — so it
    can't recognize that two DIFFERENT upload filenames in one batch
    resolve to the SAME data_dir target when that target is reached by two
    names for one inode. The real trigger is a case-insensitive filesystem:
    "IPEDS202324.accdb" and "ipeds202324.accdb" both pass FILENAME_RE
    (re.IGNORECASE), and on such a filesystem they are literally the same
    directory entry. The guard's own comment names exactly what this is
    meant to prevent: iteration 2 seeing iteration 1's own just-staged copy
    and moving IT aside onto a second `.bak`, "silently OVERWRITING the
    .bak that still held the operator's true original."

    HONESTY ABOUT WHAT THIS DOES AND DOES NOT MODEL — a case-insensitive
    filesystem cannot be mounted in CI. A hard link is the portable proxy:
    two names, resolve()-distinct, one inode. That IS enough to prove the
    guard fails to recognize the alias (verified below: today's code stages
    BOTH names independently, producing TWO `.bak` files for what was, before
    the batch started, ONE file with two names). It does NOT reproduce the
    actual data-LOSS step of the real bug: a hard link is two INDEPENDENT
    directory entries, so moving one aside can never affect the other, and
    the second `.bak` here is an independent, correctly-restorable file —
    not an overwrite of the first. On a genuine case-insensitive filesystem
    there is only ONE directory entry, so the second "move aside" IS the
    first `.bak`'s entry, which is what actually destroys the operator's
    original. Do not read a pass here as proof the data-loss step is
    covered; it is not, and cannot be, in CI — only the guard's missed
    identity is.

    VERIFIED against today's actual code by tracing an instrumented run: a
    FAILING loader's rollback (_restore_all) correctly restores BOTH
    hard-linked names on this filesystem — moving one hard link aside and
    back never disturbs the other, so the guard's miss is completely
    invisible in the FINAL state (0 `.bak` files, both names holding the
    original bytes, indistinguishable from a correctly-behaving guard). The
    miss is only observable MID-PIPELINE, before the rollback erases the
    evidence — which is why this snapshots via a spy on `integrity_checks`
    (mirroring test_run_import_integrity_checks_failure_no_swap's
    forced-failure technique: staging completes normally, then a fake
    integrity_checks captures state and fails cleanly) rather than
    asserting on run_import's return state. That snapshot shows TWO `.bak`
    files today (IPEDS202324.accdb.bak AND ipeds202324.accdb.bak) where a
    guard that recognized the alias would produce exactly ONE — the second
    upload's data_target should have been recognized as already staged and
    skipped, the same way an EXACT duplicate filename in one batch already
    is (see test_run_import_a_duplicate_upload_filename_does_not_clobber_the_previous_year)."""
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    upload_dir = d / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    name_a = "IPEDS202324.accdb"
    name_b = "ipeds202324.accdb"  # case-different; FILENAME_RE is re.IGNORECASE
    original = data_dir / name_a
    original.write_bytes(b"operators-true-original")
    os.link(original, data_dir / name_b)  # data_dir already has TWO names, one inode
    assert (data_dir / name_a).resolve() != (data_dir / name_b).resolve(), \
        "test setup error: the two data_dir paths must differ as strings"

    up1 = upload_dir / name_a
    up1.write_bytes(b"first-uploaded-bytes")
    up2 = upload_dir / name_b
    up2.write_bytes(b"second-uploaded-bytes")

    staging = live.with_name("ipeds_staging.db")

    def _fake_popen(*a, **k):
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"build ok")
        return _FakeProc(0, ["build ok"])

    snapshot = {}

    def _spy_checks(staging_, live_):
        # Mid-pipeline: staging has just finished, _restore_all() has not
        # run yet — the only point the guard's miss is observable on this
        # filesystem (see the docstring).
        snapshot["baks"] = sorted(p.name for p in data_dir.glob("*.bak"))
        return False, ["✗ forced failure to snapshot mid-pipeline state"]

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    orig_checks = importer.integrity_checks
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = _fake_popen
    importer.integrity_checks = _spy_checks
    try:
        jid = create_job("2 files", "admin@example.edu")
        run_import(jid, [up1, up2])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen
        importer.integrity_checks = orig_checks

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert snapshot.get("baks") == [name_a + ".bak"], (
        f"expected exactly one .bak — the second upload's data_target is a "
        f"hard link of the first's already-staged target, so the "
        f"duplicate-target guard should have recognized it (via real "
        f"filesystem identity, not a resolve() string) and skipped "
        f"re-staging it instead of independently backing it up too — got "
        f"{snapshot.get('baks')}")


def test_unlink_quietly_never_raises_on_a_path_it_cannot_delete():
    """Regression: _unlink_quietly is the best-effort cleanup helper the
    upload/backup discard relies on for every path it removes. Path.unlink()
    raises IsADirectoryError (an OSError) when pointed at a directory; if
    _unlink_quietly let that propagate, one unexpected path would crash
    run_import's cleanup and mask the job's real success/failure report
    instead of just logging a warning and moving on."""
    d = Path(tempfile.mkdtemp())
    sub = d / "a_directory_not_a_file"
    sub.mkdir()
    importer._unlink_quietly(0, "test-path", sub)  # must not raise
    assert sub.exists(), \
        "a directory _unlink_quietly could not remove should still be there"


def test_run_import_no_rollback_of_data_dir_after_a_successful_swap():
    """Pins two distinct things about a post-swap exception (here:
    _record_provenance choking). First: _restore_all() at the bottom of
    run_import's outer except-Exception branch (importer.py:706-709) is
    unconditional today, so ANY exception raised AFTER build_check_swap has
    already swapped the live db restores the OLD .accdb over the NEW one in
    data_dir — a silent split-brain where ipeds.db already holds the new
    data but data_dir's source file is the stale one, which then makes the
    NEXT import's superset guard misbehave. Once the swap has happened,
    rollback of data_dir must become a no-op (caught by the bytes
    assertion). Second: once the swap has happened, no rollback can ever
    reach the backup again either, so it must be positively DELETED rather
    than left orphaned on disk when the post-swap step raises (caught by the
    .bak assertion — note a genuine *restore* would also leave no .bak
    behind, since shutil.move consumes it, so this assertion alone cannot
    detect a rollback; it detects an orphaned backup)."""
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    upload = _new_upload(d, content=b"new-upload-bytes")
    data_dir.mkdir(parents=True, exist_ok=True)
    existing = data_dir / upload.name
    existing.write_bytes(b"previous-accdb-bytes")
    staging = live.with_name("ipeds_staging.db")

    def _fake_popen(*a, **k):
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"new-staging-content")
        return _FakeProc(0, ["build ok"])

    def _boom(rows):
        raise RuntimeError("app.db is locked")

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    orig_checks = importer.integrity_checks
    orig_record = importer._record_provenance
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = _fake_popen
    importer.integrity_checks = lambda staging_, live_: (True, ["✓ all good"])
    importer._record_provenance = _boom
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, [upload])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen
        importer.integrity_checks = orig_checks
        importer._record_provenance = orig_record

    assert (data_dir / upload.name).read_bytes() == b"new-upload-bytes", \
        "a post-swap exception rolled the data_dir .accdb back to the old bytes"
    assert not (data_dir / (upload.name + ".bak")).exists(), \
        "the backup was left on disk after a post-swap exception — once the " \
        "swap has happened nothing can restore it, so it must be deleted"


def test_run_import_no_rollback_of_data_dir_when_activate_staging_fails_after_the_move():
    """Regression at the actual swap boundary (distinct from the test above,
    which only breaks _record_provenance — AFTER build_check_swap has
    already returned True). The real swap is shutil.move(staging ->
    ipeds.db) INSIDE _activate_staging, followed by several more fallible
    steps (prev.unlink, a data_version bump + commit, invalidate_cache,
    _update_overall_phase) — none of which is guarded. Any of those raising
    AFTER the move propagates all the way out of build_check_swap uncaught,
    so it never returns True, and run_import concludes the swap never
    happened and calls _restore_all(). On a FRESH year — no pre-existing
    same-named data_dir file, so `backup is None` — that means
    _restore_data_dir UNLINKS the just-staged .accdb entirely, even though
    ipeds.db was already rebuilt from it: data_dir desyncs from the live db,
    and every future import is then wedged on the superset guard. Breaks
    importer.invalidate_cache — called AFTER the move, inside
    _activate_staging — to land squarely in that post-move window."""
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    upload = _new_upload(d, content=b"new-upload-bytes")
    # Deliberately NO pre-existing data_dir/<name>.accdb: a first import of
    # this year, so backup is None and a rollback UNLINKS rather than
    # restores — the destructive variant of this bug.
    staging = live.with_name("ipeds_staging.db")

    def _fake_popen(*a, **k):
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"new-staging-content")
        return _FakeProc(0, ["build ok"])

    def _boom():
        raise RuntimeError("cache invalidation blew up")

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    orig_checks = importer.integrity_checks
    orig_invalidate = importer.invalidate_cache
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = _fake_popen
    importer.integrity_checks = lambda staging_, live_: (True, ["✓ all good"])
    importer.invalidate_cache = _boom
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, [upload])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen
        importer.integrity_checks = orig_checks
        importer.invalidate_cache = orig_invalidate

    assert live.read_bytes() == b"new-staging-content", \
        ("the live db was not actually swapped before invalidate_cache raised "
         "— this test is only meaningful once the move has already happened")
    assert (data_dir / upload.name).exists(), \
        ("the just-staged data_dir .accdb was deleted after a failure INSIDE "
         "_activate_staging that struck AFTER the real swap — data_dir is now "
         "desynced from the live db that was already built from this file")
    assert (data_dir / upload.name).read_bytes() == b"new-upload-bytes", \
        "the data_dir .accdb's bytes changed after a post-move failure"


# ---------------------------------------------------------------------------
# run_import — staging record MUST be taken before the mutation it describes
# can fail, not after (see the three tests below). The append in the staging
# loop currently runs only after shutil.move/shutil.copy2 both succeed, so a
# failure partway through leaves that file's staging unrecorded, and
# _restore_all() (which replays `staged` through _restore_data_dir) never
# sees it. A stranded backup is worse than an ordinary orphan: it survives as
# <name>.accdb.bak, which _data_dir_years' `*.accdb` glob can't see, so
# _guard_no_dropped_years then refuses every LATER import as though that
# year had been dropped from the dataset.
# ---------------------------------------------------------------------------

def _copy2_failing_for(name, errno_=28):
    """Wraps the REAL shutil.copy2, raising only when the destination
    filename matches `name`. A blanket `importer.shutil.copy2 = boom` would
    also break nothing here (copy2 isn't used during rollback), but this
    selective form is kept for symmetry with `_move_failing_for_bak_dest`
    below, where blanket-breaking `shutil.move` WOULD also break
    _restore_data_dir's own rollback call and make every assertion fail for
    the wrong reason."""
    real_copy2 = importer.shutil.copy2

    def f(src, dst, *a, **k):
        if Path(dst).name == name:
            raise OSError(errno_, "No space left on device")
        return real_copy2(src, dst, *a, **k)
    return f


def test_run_import_enospc_on_the_second_file_restores_both_and_leaves_no_bak():
    """Regression for the staging-record-after-mutation bug: staged.append
    only runs after a file's move-aside AND copy succeed, so a copy2 failure
    partway through a multi-file batch (ENOSPC is the realistic case) leaves
    that file's just-taken backup unrecorded. _restore_all() then restores
    only the files that came before the failure, and the failed file's
    backup is stranded as <name>.accdb.bak forever. This asserts both files
    end up back at their ORIGINAL bytes, no *.bak litter remains, and — the
    assertion that matters most, since it's the actual wedge rather than just
    the orphan — _data_dir_years still reports BOTH years afterward: a
    stranded .bak is invisible to the `*.accdb` glob, so
    _guard_no_dropped_years would otherwise refuse every subsequent
    import."""
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    up1 = _new_upload(d, name="IPEDS202021.accdb", content=b"new-upload-1-bytes")
    up2 = _new_upload(d, name="IPEDS202122.accdb", content=b"new-upload-2-bytes")
    existing1 = data_dir / up1.name
    existing2 = data_dir / up2.name
    existing1.write_bytes(b"original-year-1-bytes")
    existing2.write_bytes(b"original-year-2-bytes")

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    orig_copy2 = importer.shutil.copy2
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = lambda *a, **k: _FakeProc(0, [])  # must never run
    importer.shutil.copy2 = _copy2_failing_for(up2.name)
    try:
        jid = create_job("2 files", "admin@example.edu")
        run_import(jid, [up1, up2])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen
        importer.shutil.copy2 = orig_copy2

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert existing1.exists() and existing1.read_bytes() == b"original-year-1-bytes", \
        "file 1 was not restored to its original bytes after file 2's copy2 failed"
    assert existing2.exists() and existing2.read_bytes() == b"original-year-2-bytes", \
        "file 2 (the one whose own copy2 raised) was not restored to its " \
        "original bytes — its backup was stranded, unrecorded, as a .bak"
    leftover_bak = list(data_dir.glob("*.bak"))
    assert leftover_bak == [], \
        f"a stranded .accdb.bak was left behind: {leftover_bak}"
    years = importer._data_dir_years(data_dir)
    assert years == {2021, 2022}, \
        (f"_data_dir_years only reports {years} — a stranded .bak is "
         "invisible to the *.accdb glob, so _guard_no_dropped_years would "
         "wrongly refuse every later import as though a year had been "
         "dropped from the dataset")


def test_run_import_partial_copy_of_a_first_time_year_is_removed():
    """Regression for the FIRST-TIME-year twin of the same bug: no
    pre-existing data_dir file means no move-aside happens (backup stays
    None), but copy2 can still fail PARTWAY, having already written some
    bytes to data_target before ENOSPC hits — and staged.append never ran
    (it's after the copy2 call), so _restore_all() never sees this target and
    the partially-written .accdb is left behind for the loader's next
    rebuild to choke on."""
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    upload = _new_upload(d, name="IPEDS202526.accdb", content=b"full-upload-bytes")
    # Deliberately no pre-existing data_dir/IPEDS202526.accdb.

    def _copy2_partial_then_fail(src, dst, *a, **k):
        Path(dst).write_bytes(b"partial-write-before-enospc")
        raise OSError(28, "No space left on device")

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    orig_copy2 = importer.shutil.copy2
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = lambda *a, **k: _FakeProc(0, [])  # must never run
    importer.shutil.copy2 = _copy2_partial_then_fail
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, [upload])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen
        importer.shutil.copy2 = orig_copy2

    row = _job_row(jid)
    assert row["status"] == "failed", row
    leftover_accdb = list(data_dir.glob("*.accdb"))
    assert leftover_accdb == [], \
        f"a partially-written .accdb was left behind: {leftover_accdb}"
    leftover_bak = list(data_dir.glob("*.bak"))
    assert leftover_bak == [], \
        f"an unexpected .bak was left behind: {leftover_bak}"


def test_run_import_a_failed_move_aside_leaves_the_existing_file_untouched():
    """The test that makes `existed_before` load-bearing rather than
    decorative — it must FAIL against a naive "just hoist staged.append
    above the mutations" fix and PASS against the three-state
    (backup / existed_before) one. If the append merely moves earlier but
    still records whatever `backup` PATH was computed (whether or not that
    path was ever actually created on disk), then a failure IN THE
    MOVE-ASIDE ITSELF still leaves `_restore_data_dir` seeing
    `backup.exists() == False` and falling into the old
    `elif data_target.exists(): unlink` branch — deleting the admin's
    UNTOUCHED previous-year file. That turns "unrestorable orphan" into
    "deletes a file this job never touched", strictly worse than the bug
    it's meant to fix. The three-state fix must instead recognize the
    original is still sitting under its own name (a same-directory
    shutil.move takes the atomic os.rename path, so no partial move is
    possible) and leave it alone."""
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    upload = _new_upload(d, name="IPEDS202526.accdb", content=b"new-upload-bytes")
    existing = data_dir / upload.name
    existing.write_bytes(b"original-existing-bytes")

    real_move = importer.shutil.move

    def _move_failing_for_bak_dest(src, dst, *a, **k):
        if str(dst).endswith(".accdb.bak"):
            raise OSError(28, "No space left on device")
        return real_move(src, dst, *a, **k)

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    orig_move = importer.shutil.move
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = lambda *a, **k: _FakeProc(0, [])  # must never run
    importer.shutil.move = _move_failing_for_bak_dest
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, [upload])
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen
        importer.shutil.move = orig_move

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert existing.exists(), \
        "the pre-existing data_dir file was DELETED after its own " \
        "move-aside failed — existed_before must be tracked separately " \
        "from whether the backup move actually completed"
    assert existing.read_bytes() == b"original-existing-bytes", \
        "the pre-existing data_dir file's bytes changed after a failed move-aside"


def test_run_import_rolls_back_even_when_the_failure_log_write_fails():
    """Regression: run_import's outer `except Exception` handler writes the
    failure LOG (_log) and STATUS (_set_status) — each its own real app.db
    connect()+commit(), with nothing catching either — BEFORE calling
    _restore_all(). So if that log/status write itself raises, the raise
    propagates straight out of the except block and _restore_all() — the one
    call this whole handler exists to make — never runs. This is reachable
    in exactly the scenario the ENOSPC test above already models: app_db_path,
    data_dir and upload_dir all default under one ROOT, and the shipped
    compose.yaml mounts the whole tree as one volume, so the multi-GB copy2
    that just filled the disk is followed by a WAL commit onto that same
    full filesystem — or, just as reachable, a plain 'database is locked'
    after the busy timeout. A rollback ordered AFTER a status write is a
    rollback that does not run on the one occasion it is needed most: when
    the disk is full.

    Drives the same two-file ENOSPC staging failure as the test above, and
    additionally makes the app.db-backed _log call raise on its "ERROR:"
    line — the only _log call inside run_import's own except block, so this
    leaves every OTHER _log call (preflight, staging) working normally and
    is a clean, restorable seam in this file's wrap-the-real-function style.

    The job row's `status` may itself be unwritable in this exact scenario
    (the failing write is the status write), so this asserts only on the
    FILESYSTEM — pinning the job row here would assert something the fix
    cannot deliver."""
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    up1 = _new_upload(d, name="IPEDS202021.accdb", content=b"new-upload-1-bytes")
    up2 = _new_upload(d, name="IPEDS202122.accdb", content=b"new-upload-2-bytes")
    existing1 = data_dir / up1.name
    existing2 = data_dir / up2.name
    existing1.write_bytes(b"original-year-1-bytes")
    existing2.write_bytes(b"original-year-2-bytes")

    real_log = importer._log

    def _log_failing_on_error_line(job_id, line):
        if line.startswith("ERROR:"):
            raise sqlite3.OperationalError("database is locked")
        return real_log(job_id, line)

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    orig_copy2 = importer.shutil.copy2
    orig_log = importer._log
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    importer.subprocess.Popen = lambda *a, **k: _FakeProc(0, [])  # must never run
    importer.shutil.copy2 = _copy2_failing_for(up2.name)
    importer._log = _log_failing_on_error_line
    try:
        jid = create_job("2 files", "admin@example.edu")
        try:
            run_import(jid, [up1, up2])
        except Exception:
            # The engineered failure-log raise (modeling a disk-full/locked
            # app.db) propagates straight out of run_import, uncaught —
            # exactly as it does today on the background thread run_import
            # normally runs on (an unhandled exception printed to stderr,
            # with the job row left stuck at whatever it last wrote). This
            # test's whole point is what ends up on disk despite that raise,
            # so it's swallowed here rather than failing the test harness.
            pass
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen
        importer.shutil.copy2 = orig_copy2
        importer._log = orig_log

    assert existing1.exists() and existing1.read_bytes() == b"original-year-1-bytes", \
        ("file 1 was not restored — the failure-log write raising skipped "
         "_restore_all() entirely")
    assert existing2.exists() and existing2.read_bytes() == b"original-year-2-bytes", \
        ("file 2 was not restored — the failure-log write raising skipped "
         "_restore_all() entirely")
    leftover_bak = list(data_dir.glob("*.bak"))
    assert leftover_bak == [], \
        f"a stranded .accdb.bak was left behind: {leftover_bak}"
    years = importer._data_dir_years(data_dir)
    assert years == {2021, 2022}, \
        (f"_data_dir_years only reports {years} — the rollback that should "
         "have run before the failing log write never ran")


def test_run_import_a_duplicate_upload_filename_does_not_clobber_the_previous_year():
    """Regression for routers/admin.py's duplicate-destination bug: it builds
    `dest = s.upload_dir / Path(uf.filename).name` and appends unconditionally
    for every part of a multipart POST, so two parts sharing a filename yield
    `upload_paths=[p, p]` — the same path, staged twice in one run_import
    batch.

    VERIFIED against today's actual code (already carrying the sibling
    staging-record-order fix in this same file) before writing this
    expectation, by driving this exact scenario and inspecting disk state
    directly: iteration 1 moves the pre-existing file aside to
    `<name>.accdb.bak` and copies the upload over `data_target`. Iteration 2
    then sees `data_target` exists too — but that's iteration 1's OWN
    just-staged copy, not a second pre-existing file — computes the SAME
    `.bak` path, and moves `data_target` onto it, silently OVERWRITING the
    `.bak` (which still held the operator's true original bytes) with a
    second copy of the upload. That corruption happens during STAGING,
    before any failure or rollback. A later failure's `_restore_all()` then
    faithfully round-trips whatever is left in the `.bak` (the corrupted
    upload copy, not the original) onto `data_target`, and the
    `existed_before` no-op branch (the sibling fix in this file) stops the
    second, now-backup-less restore entry from deleting it — so today's job
    ends 'failed' with `data_target` silently holding the UPLOAD's bytes
    and no `.bak`, instead of restoring the operator's previous year. Not a
    regression (the old two-branch `_restore_data_dir` would have DELETED it
    instead — strictly worse) and not reachable from the UI (admin-only),
    but this loop is being rewritten anyway, so it's the cheap moment to
    close it: the fix skips a `data_target` already staged earlier in the
    same batch."""
    d = Path(tempfile.mkdtemp())
    live = d / "ipeds.db"
    data_dir = d / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    upload = _new_upload(d, name="IPEDS202526.accdb", content=b"new-upload-bytes")
    existing = data_dir / upload.name
    existing.write_bytes(b"original-previous-year-bytes")

    orig_settings = importer.get_settings
    orig_preflight = importer.preflight
    orig_popen = importer.subprocess.Popen
    importer.get_settings = lambda: _fake_settings(live, data_dir)
    importer.preflight = lambda p: (True, "Preflight OK")
    # Fail fast right after staging, mirroring the existing loader-failure
    # test, so this doesn't need a real rebuild.
    importer.subprocess.Popen = lambda *a, **k: _FakeProc(1, ["loader output"])
    try:
        jid = create_job(upload.name, "admin@example.edu")
        run_import(jid, [upload, upload])  # the admin.py duplicate-dest bug
    finally:
        importer.get_settings = orig_settings
        importer.preflight = orig_preflight
        importer.subprocess.Popen = orig_popen

    row = _job_row(jid)
    assert row["status"] == "failed", row
    assert existing.exists(), \
        "the pre-existing data_dir file was deleted after a duplicate-filename batch"
    assert existing.read_bytes() == b"original-previous-year-bytes", \
        ("the pre-existing file's ORIGINAL bytes were lost — a duplicate "
         "filename in the same batch let the second staging iteration "
         "overwrite the .bak (still holding the true original) with the "
         "job's own already-staged copy, so the rollback restored the "
         "wrong content instead of the operator's previous year")
    assert not (data_dir / (upload.name + ".bak")).exists(), \
        "a stray .accdb.bak was left behind after the duplicate-filename batch"


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
        run_import(jid, [upload])
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
        run_import(jid, [upload])
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
    # Same contract as an import swap: the moved-aside copy is deleted once
    # staging is live. A year removal is just as rebuildable as a rebuild.
    assert not live.with_suffix(".db.prev").exists(), \
        "the pre-removal database copy was left on disk after a successful swap"


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


def test_loader_script_path_resolves_to_repo_root_scripts():
    # Guards the ROOT anchor run_import uses to find the loader: importer.py
    # lives at backend/app/, so the script is parents[2]/scripts/build_ipeds_db.py.
    # This anchor is otherwise UNGATED — run_import's tests monkeypatch
    # subprocess.run, so a wrong parents[N] (e.g. after a repo restructure) would
    # silently break real Admin -> Imports rebuilds with nothing catching it.
    build = Path(importer.__file__).resolve().parents[2] / "scripts" / "build_ipeds_db.py"
    assert build.exists(), f"loader script not found at {build}"


def test_activate_staging_removes_the_previous_copy():
    """THE REGRESSION: _activate_staging moved live -> ipeds.db.prev and NOTHING
    ever deleted it, so every import or year-removal left a full extra copy of a
    ~2 GB database on disk, permanently — a long-running deployment silently
    carried two datasets. ipeds.db is rebuildable from the .accdb sources (which
    is why it isn't backed up), so the .prev copy is only worth keeping for the
    instant between the two moves."""
    d = Path(tempfile.mkdtemp())
    live, staging = d / "ipeds.db", d / "ipeds_staging.db"
    live.write_bytes(b"OLD-DATABASE")
    staging.write_bytes(b"NEW-DATABASE")

    orig_settings = importer.get_settings
    base = orig_settings()

    class _S:
        ipeds_db_path = live
        def __getattr__(self, name):
            return getattr(base, name)

    importer.get_settings = lambda: _S()
    calls = []
    orig_phase, orig_log = importer._update_overall_phase, importer._log
    importer._update_overall_phase = lambda *a, **k: None
    importer._log = lambda *a, **k: calls.append(a)
    try:
        importer._activate_staging(1, staging)
    finally:
        importer.get_settings = orig_settings
        importer._update_overall_phase, importer._log = orig_phase, orig_log

    assert live.read_bytes() == b"NEW-DATABASE", "staging did not become live"
    assert not (d / "ipeds.db.prev").exists(), \
        "the previous database copy was left behind — that is a full extra dataset on disk"
    assert not staging.exists(), "the staging file should have been moved, not copied"


def test_reconcile_interrupted_jobs_clears_a_ghost_and_spares_terminal_rows():
    """THE REGRESSION: the rebuild runs on a DAEMON thread and only marks itself
    `failed` from its own except block, so a SIGKILL / OOM kill / host reboot /
    `docker compose pull && up -d` leaves the row at `running` forever. Nothing
    ever reconciled it.

    That was cosmetic until Admin -> Imports started ADOPTING a non-terminal job
    on mount: after that, every later visit adopts the ghost, `locked` is true,
    and every control on the tab -- year cards, integrate, upload, the remove
    trashcans -- is disabled behind an "an import is running" notice, forever,
    with no dismiss. Recovery was hand-editing app.db.

    Asserts BOTH directions: the ghost is cleared, and rows that legitimately
    finished are untouched (a sweep that rewrote `swapped` would erase the
    record of a successful import)."""
    con = importer.connect()
    try:
        con.execute("DELETE FROM import_jobs")
        for status in ("running", "checks", "swapped", "failed"):
            con.execute("INSERT INTO import_jobs(filename, status, created_at, updated_at) "
                        "VALUES (?,?,?,?)", (f"j-{status}", status, 0, 0))
        con.commit()
    finally:
        con.close()

    n = importer.reconcile_interrupted_jobs()
    assert n == 2, f"expected 2 non-terminal rows reconciled, got {n}"

    con = importer.connect()
    try:
        got = dict(con.execute(
            "SELECT filename, status FROM import_jobs").fetchall())
    finally:
        con.close()
    assert got["j-running"] == "failed", got
    assert got["j-checks"] == "failed", got
    # Terminal rows must be left exactly as they were.
    assert got["j-swapped"] == "swapped", "a completed import was rewritten"
    assert got["j-failed"] == "failed", got

    # Idempotent: a second boot must not re-touch anything.
    assert importer.reconcile_interrupted_jobs() == 0, \
        "reconciliation is not idempotent across restarts"


def run():
    print("importer contract:")
    check("interrupted jobs are reconciled at boot; terminal rows are spared",
          test_reconcile_interrupted_jobs_clears_a_ghost_and_spares_terminal_rows)
    check("loader script path resolves to repo-root scripts/ (ROOT anchor)",
          test_loader_script_path_resolves_to_repo_root_scripts)
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
    check("_restore_data_dir leaves an existing target alone when no backup "
          "was taken (existed_before=True, backup=None)",
          test_restore_data_dir_leaves_an_existing_target_alone_when_no_backup_was_taken)
    check("run_import: preflight failure fails the job, no swap",
          test_run_import_preflight_failure_no_swap)
    check("_discard_uploads: does not delete when _same_file's identity check is unprovable (None)",
          test_discard_uploads_does_not_delete_when_identity_cannot_be_proven)
    check("run_import: loader failure restores the data dir",
          test_run_import_loader_failure_restores_data_dir)
    check("run_import: integrity-checks failure leaves live db untouched",
          test_run_import_integrity_checks_failure_no_swap)
    check("run_import: unexpected exception is caught and reported",
          test_run_import_unexpected_exception_is_caught)
    check("run_import: refuses a rebuild that would DROP a live year (superset guard)",
          test_run_import_refuses_dropping_a_live_year)
    check("run_import: multi-file success stages all + records provenance for each",
          test_run_import_multi_file_success_records_all_provenance)
    check("run_import: backs up a pre-existing staged .accdb of the same name",
          test_run_import_backs_up_existing_staged_accdb)
    check("run_import: success swaps db, bumps data_version, clears cache",
          test_run_import_success_swaps_and_bumps_data_version)
    check("run_import: success removes the uploaded copy and the .bak backup",
          test_run_import_success_removes_the_uploaded_copy_and_the_backup)
    check("run_import: a failed import still removes the uploaded copy",
          test_run_import_a_failed_import_still_removes_the_uploaded_copy)
    check("run_import: never deletes the upload when upload_dir == data_dir",
          test_run_import_never_deletes_the_upload_when_upload_dir_equals_data_dir)
    check("run_import: a failed import does not delete the dataset when "
          "upload_dir == data_dir",
          test_run_import_a_failed_import_does_not_delete_the_dataset_when_upload_dir_equals_data_dir)
    check("_same_file: true for a hard link reached from another directory",
          test_same_file_true_for_a_hardlink_in_another_directory)
    check("_same_file: true through a symlinked directory (resolve()'s old behavior kept)",
          test_same_file_true_through_a_symlinked_directory)
    check("_same_file: false for two distinct files (casefold/normcase false-success guard)",
          test_same_file_false_for_two_distinct_files)
    check("_same_file: false (not None) when the candidate does not exist",
          test_same_file_false_when_the_candidate_does_not_exist)
    check("_same_file: none for an unstattable path (symlink loop, fail closed)",
          test_same_file_none_for_an_unstattable_path)
    check("run_import: does not delete the dataset when the upload is a hard link of it",
          test_run_import_does_not_delete_the_dataset_when_the_upload_is_a_hardlink_of_it)
    check("_discard_uploads: removes a hard-linked upload sitting in a separate directory",
          test_discard_uploads_removes_a_hardlinked_upload_in_a_separate_directory)
    check("run_import: stages a needless second .bak for a hard-linked data_target in one batch",
          test_run_import_stages_a_second_bak_for_a_hardlinked_data_target_in_one_batch)
    check("_unlink_quietly never raises on a path it cannot delete",
          test_unlink_quietly_never_raises_on_a_path_it_cannot_delete)
    check("run_import: no rollback of data_dir after a successful swap",
          test_run_import_no_rollback_of_data_dir_after_a_successful_swap)
    check("run_import: no rollback of data_dir when _activate_staging fails after the move",
          test_run_import_no_rollback_of_data_dir_when_activate_staging_fails_after_the_move)
    check("run_import: ENOSPC on the second file restores both, leaves no "
          ".bak, and _data_dir_years still sees both years",
          test_run_import_enospc_on_the_second_file_restores_both_and_leaves_no_bak)
    check("run_import: a partial copy of a first-time year is removed, not stranded",
          test_run_import_partial_copy_of_a_first_time_year_is_removed)
    check("run_import: a failed move-aside leaves the existing file untouched",
          test_run_import_a_failed_move_aside_leaves_the_existing_file_untouched)
    check("run_import: rolls back even when the failure-log write itself fails",
          test_run_import_rolls_back_even_when_the_failure_log_write_fails)
    check("run_import: a duplicate upload filename does not clobber the previous year",
          test_run_import_a_duplicate_upload_filename_does_not_clobber_the_previous_year)
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
    check("the atomic swap removes the previous ipeds.db copy",
          test_activate_staging_removes_the_previous_copy)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL IMPORTER TESTS PASSED")


if __name__ == "__main__":
    run()
