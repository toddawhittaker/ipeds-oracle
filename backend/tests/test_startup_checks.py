"""Contract for the startup preflight (backend/app/startup_checks.py), run as
`python -m app.startup_checks` from scripts/docker-entrypoint.sh, BEFORE `exec
uvicorn` (that script is `set -e`, so a non-zero exit here stops the container
from ever starting the app).

THE REGRESSION THIS MODULE EXISTS TO PREVENT: an unwritable data directory
(e.g. a bind-mounted ./srv-data still owned by root after the image switches to
a non-root uid — Docker never chowns a bind mount for you) currently surfaces as
`sqlite3.OperationalError: unable to open database file` raised from INSIDE
uvicorn's application import. Two things make that crash uniquely bad, both
verified directly in the code before writing these tests:

  1. `backend/app/main.py` calls `logbuffer.install()` at IMPORT TIME (module
     level, line 41 -- `_install_logbuffer()`, executed while `app.main` is
     being imported, long before uvicorn ever calls `lifespan`).
  2. `SqliteLogHandler.__init__` (backend/app/logbuffer.py) does
     `Path(db_path).parent.mkdir(parents=True, exist_ok=True)` then
     `sqlite3.connect(str(db_path), ...)` with NO try/except around either
     call -- an unwritable directory raises straight out of `__init__`.

Because the crash happens at IMPORT time, it is also too early for
`app.main.lifespan` to help: `lifespan` is an async context manager that only
runs once uvicorn has already imported `app.main:app` successfully, and the
crash above happens DURING that import, before `lifespan` is ever reached. A
preflight living inside the app (in `lifespan`, or anywhere reachable only
after import) cannot intercept this -- it has to run as a genuinely separate
process/module that resolves `Settings` and probes the filesystem WITHOUT
importing `app.main` (and therefore without triggering the logbuffer install).
That is the whole reason `startup_checks.py` exists as its own
`python -m app.startup_checks` invocation rather than a function called from
`lifespan`.

The resulting traceback (raised from inside uvicorn's import machinery) names
no path, no uid, no fix -- just a bare sqlite error several frames deep. This
preflight turns that into a legible, actionable message on stderr and a clean
process exit code, so `set -e` in docker-entrypoint.sh stops the container with
something an operator can act on (a `chown` command) instead of a stack trace.

Every test below names the specific regression it guards against in its
docstring. Cases 1-3 mirror the spec's three probe scenarios in order (clean
tree / read-only directory / read-only existing file); case 4 is the drift
test (a new writable Path setting added to config.Settings without being
wired into the preflight); case 5 pins the message contract; case 6 pins the
process-exit contract, including that main() must never let an internal bug
escape as a raw traceback -- exactly the failure mode this module exists to
eliminate.
"""
import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))  # backend/ -> `app`

from app.config import Settings  # noqa: E402 -- exists already, safe to import unconditionally

# The module under test does not exist yet. Per repo convention (see
# test_access_gate.py's ImportError/AttributeError handling), the import is
# guarded here so the WHOLE suite reports clean, informative RED failures
# instead of crashing at collection with a bare ImportError traceback.
try:
    from app import startup_checks
    _IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 -- converted into ordinary failing tests below
    startup_checks = None
    _IMPORT_ERROR = e

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _require_module():
    if startup_checks is None:
        raise AssertionError(
            "app.startup_checks does not exist yet (import failed: "
            f"{_IMPORT_ERROR!r}). This whole suite is RED until "
            "backend/app/startup_checks.py is implemented.")


def _settings_for(root: Path, **overrides) -> Settings:
    """A Settings instance whose every path lives under `root`, isolated from
    any real .env / production ipeds.db / app.db on this box. Directories are
    deliberately NOT pre-created (except where a test needs one to already
    exist to chmod it) -- the preflight itself is what must create them.

    Constructor kwargs win over both OS env vars and a real .env file in
    pydantic-settings' precedence order, so this is safe to use even on a dev
    box with its own .env sitting at ROOT/.env.
    """
    kwargs = dict(
        app_db_path=root / "state" / "app.db",
        ipeds_db_path=root / "ipeds" / "ipeds.db",
        log_db_path=root / "state" / "logs.db",
        data_dir=root / "data",
        upload_dir=root / "uploads",
        nces_work_dir=root / "work",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _skip_if_root(name):
    """Permission-bit tests are meaningless as root (which ignores rwx checks
    on files/dirs it doesn't strictly own via other mechanisms -- in practice
    root can write almost anything). Skip loudly rather than silently, so a
    root CI runner doesn't look like it exercised a case it didn't."""
    if os.geteuid() == 0:
        print(f"  ⚠ {name}: SKIPPED (running as root; permission bits are not enforced)")
        return True
    return False


# ---------------------------------------------------------------------------
# 1. A clean tmpdir passes, and missing subdirectories get created.
# ---------------------------------------------------------------------------

def test_clean_tree_passes_and_creates_missing_directories():
    """THE ORDINARY FIRST-BOOT CASE. A brand-new deployment's data/uploads/work
    directories don't exist yet -- run_checks must create them (mkdir
    parents=True) and report zero failures, not treat "doesn't exist yet" as
    a problem. Regression this catches: a preflight that only VALIDATES
    existing directories (os.path.isdir + writable) rather than creating
    missing ones would false-positive-fail every fresh install."""
    _require_module()
    root = Path(tempfile.mkdtemp())
    settings = _settings_for(root)
    for d in (settings.data_dir, settings.upload_dir, settings.nces_work_dir,
              settings.app_db_path.parent, settings.ipeds_db_path.parent):
        assert not d.exists(), f"test setup bug: {d} already exists"

    failures = startup_checks.run_checks(settings)
    assert failures == [], f"a clean, writable tree must report no failures: {failures}"

    for d in (settings.data_dir, settings.upload_dir, settings.nces_work_dir,
              settings.app_db_path.parent, settings.ipeds_db_path.parent):
        assert d.is_dir(), f"{d} should have been created by the preflight"

    # The preflight PROBES app.db/logs.db only if they already exist -- it must
    # not fabricate an empty db file the app's own init_db hasn't created yet.
    assert not settings.app_db_path.exists(), \
        "the preflight must not create app.db itself, only its directory"
    assert not settings.resolved_log_db_path.exists(), \
        "the preflight must not create logs.db itself, only its directory"


# ---------------------------------------------------------------------------
# 2. A read-only directory (0o555) is reported, with the path in the message.
# ---------------------------------------------------------------------------

def test_read_only_directory_is_reported_with_its_path():
    """THE UPGRADE-CRASH CASE, DIRECTORY HALF: a directory that already exists
    but can't be written to (the shape of a root-owned bind mount under a new
    non-root uid) must be reported, and the reported message must NAME the
    failing path -- an operator can't act on "something failed"."""
    _require_module()
    if _skip_if_root("read_only_directory_is_reported"):
        return
    root = Path(tempfile.mkdtemp())
    ro_dir = root / "uploads"
    ro_dir.mkdir()
    ro_dir.chmod(0o555)
    try:
        settings = _settings_for(root, upload_dir=ro_dir)
        failures = startup_checks.run_checks(settings)
        assert failures, "a read-only directory must be reported as a failure"
        assert any(str(ro_dir) in f for f in failures), \
            f"the read-only directory's path must appear in a failure message: {failures}"
    finally:
        ro_dir.chmod(0o755)


# ---------------------------------------------------------------------------
# 3. A writable directory holding a read-only app.db is reported, and the
#    message names app.db. THE CASE A DIRECTORY-ONLY PROBE MISSES.
# ---------------------------------------------------------------------------

def test_writable_dir_with_read_only_app_db_is_reported_naming_app_db():
    """THE UPGRADE-CRASH CASE, FILE HALF -- and the one most likely to bite the
    real upgrade: chowning the directory but forgetting the files inside it
    leaves a root-owned app.db that SQLite still cannot write, even though
    `mkdir`+write-and-unlink on the directory succeeds cleanly. A probe that
    only checks the directory (os.access/mkstemp on the parent) would report
    this tree as healthy and let the container start into the same opaque
    sqlite3.OperationalError this module exists to prevent. The sanity
    assertion below on probe_directory() proves this is specifically about
    the FILE, not a directory problem."""
    _require_module()
    if _skip_if_root("writable_dir_with_read_only_app_db"):
        return
    root = Path(tempfile.mkdtemp())
    state_dir = root / "state"
    state_dir.mkdir()
    app_db = state_dir / "app.db"
    app_db.write_bytes(b"not a real sqlite file, just needs to exist")
    app_db.chmod(0o444)
    try:
        # Sanity: the directory alone is perfectly writable. If this assertion
        # fails, the test isn't isolating what it claims to.
        assert startup_checks.probe_directory(state_dir) is None, \
            "the directory itself must probe clean -- this test is about the file"

        settings = _settings_for(root, app_db_path=app_db)
        failures = startup_checks.run_checks(settings)
        assert failures, (
            "a read-only existing app.db must be reported even though its "
            "directory is fully writable")
        assert any("app.db" in f for f in failures), \
            f"a failure message must name app.db specifically: {failures}"
    finally:
        app_db.chmod(0o644)


# ---------------------------------------------------------------------------
# Direct unit coverage of the two probe primitives (mechanism-level, distinct
# from the end-to-end run_checks() scenarios above).
# ---------------------------------------------------------------------------

def test_probe_directory_creates_a_missing_directory():
    """mkdir(parents=True, exist_ok=True) must reach multiple missing levels
    at once -- a shallow mkdir() (no parents=True) would raise FileNotFoundError
    on a nested missing path and this would go red."""
    _require_module()
    root = Path(tempfile.mkdtemp())
    target = root / "a" / "b" / "c"
    assert not target.exists()
    result = startup_checks.probe_directory(target)
    assert result is None, result
    assert target.is_dir()


def test_probe_directory_passes_for_an_existing_writable_directory():
    _require_module()
    root = Path(tempfile.mkdtemp())
    assert startup_checks.probe_directory(root) is None


def test_probe_directory_reports_a_read_only_directory():
    _require_module()
    if _skip_if_root("probe_directory_reports_a_read_only_directory"):
        return
    root = Path(tempfile.mkdtemp())
    root.chmod(0o555)
    try:
        result = startup_checks.probe_directory(root)
        assert result is not None, "a read-only directory must not probe clean"
        assert str(root) in result, result
    finally:
        root.chmod(0o755)


def test_probe_file_skips_a_file_that_does_not_exist_yet():
    """A not-yet-created app.db/logs.db is not this probe's job -- the app's
    own init_db()/logbuffer.install() create it. Regression this guards: a
    probe that tries to open a nonexistent file and reports THAT as a failure
    would false-positive-fail every fresh install (the same trap as the
    directory case in test 1)."""
    _require_module()
    root = Path(tempfile.mkdtemp())
    missing = root / "app.db"
    assert not missing.exists()
    assert startup_checks.probe_file(missing) is None


def test_probe_file_passes_for_an_existing_writable_file():
    _require_module()
    root = Path(tempfile.mkdtemp())
    f = root / "app.db"
    f.write_bytes(b"x")
    assert startup_checks.probe_file(f) is None


def test_probe_file_reports_a_read_only_existing_file():
    """THE MECHANISM PIN: this must open with 'r+b' (read+write), not plain
    'r'. A probe that opens read-only would never notice a file it can't
    WRITE to -- silently reproducing the exact upgrade-crash bug (SQLite
    needs write access, plain read succeeds regardless of the write bit)."""
    _require_module()
    if _skip_if_root("probe_file_reports_a_read_only_existing_file"):
        return
    root = Path(tempfile.mkdtemp())
    f = root / "app.db"
    f.write_bytes(b"x")
    f.chmod(0o444)
    try:
        result = startup_checks.probe_file(f)
        assert result is not None, "a read-only existing file must not probe clean"
        assert str(f) in result, result
    finally:
        f.chmod(0o644)


# ---------------------------------------------------------------------------
# 4. The probed set equals the set derived from Settings (the drift test).
# ---------------------------------------------------------------------------

def test_every_path_setting_is_covered_by_a_probe_or_explicitly_excluded():
    """THE REGRESSION THIS CATCHES: a new writable Path setting is added to
    config.Settings (the next LOG_DB_PATH) and nobody teaches the preflight
    about it -- so on the very upgrade this module exists for, that one path
    silently keeps crashing at import time while every OTHER path passes.

    Expressed WITHOUT re-hardcoding startup_checks' own path list (which would
    make this vacuous -- it would just check the module agrees with itself):
    the set of "candidate" settings is derived structurally from
    config.Settings by NAMING CONVENTION (every field ending in `_path` or
    `_dir`), independently of anything startup_checks.py does. Each candidate
    must then be reachable through directories()/files() -- as itself, or via
    its parent directory -- or be named in EXCLUDED_SETTINGS with a reason.
    Adding a field like `foo_dir` to Settings and forgetting to wire it in
    fails this test; so does silently dropping ipeds_db_path.parent (the
    integrate/rebuild swap target) or app_db_path.parent (where db.py writes
    pre-migration snapshots) from directories().
    """
    _require_module()
    root = Path(tempfile.mkdtemp())
    settings = _settings_for(root)

    path_setting_names = {
        name for name in Settings.model_fields
        if name.endswith("_path") or name.endswith("_dir")
    }
    # Sanity: prove this derivation isn't accidentally empty (which would make
    # the whole test vacuously pass).
    assert {"ipeds_db_path", "app_db_path", "data_dir", "upload_dir",
            "nces_work_dir", "schema_md_path"} <= path_setting_names, path_setting_names

    probed_dirs = set(startup_checks.directories(settings))
    probed_files = set(startup_checks.files(settings))
    excluded_names = set(startup_checks.EXCLUDED_SETTINGS)
    assert len(excluded_names) <= 3, (
        "EXCLUDED_SETTINGS has grown past a handful -- probe the setting "
        "instead of exempting it.")

    unaccounted = []
    for name in sorted(path_setting_names):
        if name in excluded_names:
            continue
        value = (settings.resolved_log_db_path if name == "log_db_path"
                  else getattr(settings, name))
        covered = (value in probed_files or value in probed_dirs
                   or value.parent in probed_dirs)
        if not covered:
            unaccounted.append((name, str(value)))
    assert not unaccounted, (
        "Settings path field(s) not reachable through startup_checks.directories()/"
        "files(), and not in EXCLUDED_SETTINGS: "
        f"{unaccounted}. Wire each one in, or exclude it with a documented reason.")


def test_schema_md_path_is_never_probed():
    """The one expected exclusion: docs/SCHEMA.md is shipped read-only and the
    app only ever READS it (it's injected into every agent prompt) -- it must
    never be probed as writable, and its directory (docs/) must not be
    required to be writable either. If this ever starts failing because
    schema_md_path became genuinely writable, update EXCLUDED_SETTINGS
    deliberately rather than letting this test rot."""
    _require_module()
    root = Path(tempfile.mkdtemp())
    settings = _settings_for(root, schema_md_path=root / "docs" / "SCHEMA.md")
    probed_dirs = set(startup_checks.directories(settings))
    probed_files = set(startup_checks.files(settings))
    assert settings.schema_md_path not in probed_files
    assert settings.schema_md_path not in probed_dirs
    assert settings.schema_md_path.parent not in probed_dirs


# ---------------------------------------------------------------------------
# 5. The failure message: live uid/gid, a chown command, every failing path,
#    and never a traceback.
# ---------------------------------------------------------------------------

def test_failure_message_has_uid_gid_chown_command_paths_no_traceback():
    """THE OPERATOR-FACING CONTRACT. The whole point of this module is
    replacing an opaque sqlite3.OperationalError several frames deep inside
    uvicorn's import with something an operator can act on immediately --
    which means the message must be self-contained: WHO is running (uid/gid),
    WHAT to run (a chown command using those exact ids), and WHICH paths
    failed, all on stderr, and it must never look like a Python crash."""
    _require_module()
    if _skip_if_root("failure_message_has_uid_gid_chown_command_paths_no_traceback"):
        return
    root = Path(tempfile.mkdtemp())
    ro_dir = root / "uploads"
    ro_dir.mkdir()
    ro_dir.chmod(0o555)
    state_dir = root / "state"
    state_dir.mkdir()
    bad_app_db = state_dir / "app.db"
    bad_app_db.write_bytes(b"x")
    bad_app_db.chmod(0o444)
    try:
        settings = _settings_for(root, upload_dir=ro_dir, app_db_path=bad_app_db)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code = startup_checks.main(settings)
        text = buf.getvalue()

        assert code == 1, f"expected exit code 1, got {code!r}"
        uid, gid = os.getuid(), os.getgid()
        assert re.search(rf"\bchown\b.*\b{uid}:{gid}\b", text, re.I), (
            f"expected a chown command naming uid:gid {uid}:{gid} together, got:\n{text}")
        assert str(ro_dir) in text, f"the read-only directory's path is missing:\n{text}"
        assert "app.db" in text, f"app.db must be named in the message:\n{text}"
        assert "Traceback" not in text, f"a preflight failure must never look like a crash:\n{text}"
    finally:
        ro_dir.chmod(0o755)
        bad_app_db.chmod(0o644)


# ---------------------------------------------------------------------------
# 6. main() returns 1 on failure and 0 on success, and never propagates an
#    exception -- the entrypoint must see an exit code, not a stack trace.
# ---------------------------------------------------------------------------

def test_main_returns_0_on_a_clean_tree():
    _require_module()
    root = Path(tempfile.mkdtemp())
    settings = _settings_for(root)
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        code = startup_checks.main(settings)
    assert code == 0, f"expected 0 on success, got {code!r}"
    assert "Traceback" not in buf.getvalue()


def test_main_returns_1_on_a_broken_tree():
    _require_module()
    if _skip_if_root("main_returns_1_on_a_broken_tree"):
        return
    root = Path(tempfile.mkdtemp())
    ro_dir = root / "uploads"
    ro_dir.mkdir()
    ro_dir.chmod(0o555)
    try:
        settings = _settings_for(root, upload_dir=ro_dir)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code = startup_checks.main(settings)
        assert code == 1, f"expected 1 on failure, got {code!r}"
    finally:
        ro_dir.chmod(0o755)


def test_main_never_propagates_an_unexpected_exception():
    """THE ENTRYPOINT MUST NEVER SEE A TRACEBACK, EVEN FOR A BUG INSIDE
    run_checks() ITSELF -- not just an anticipated OSError from a filesystem
    probe. This forces a code path no fs-permission scenario can reach: a
    genuinely unexpected internal failure. If main() only wraps the individual
    probes in try/except OSError (rather than wrapping the whole check pass
    broadly), a defect in run_checks()'s own logic -- unrelated to filesystem
    permissions -- would crash `python -m app.startup_checks` with a raw
    traceback, exactly the failure mode this whole module exists to remove
    for docker-entrypoint.sh's `set -e`."""
    _require_module()
    root = Path(tempfile.mkdtemp())
    settings = _settings_for(root)
    orig = startup_checks.run_checks

    def _boom(_settings):
        raise RuntimeError("boom - an unrelated internal bug, not a permission error")

    startup_checks.run_checks = _boom
    try:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                code = startup_checks.main(settings)
        except Exception as e:  # noqa: BLE001 -- this IS the failure being tested for
            raise AssertionError(
                f"main() let an internal exception escape instead of catching it: {e!r}") from e
    finally:
        startup_checks.run_checks = orig

    assert code == 1, f"an internal failure must still exit 1, got {code!r}"
    assert "Traceback" not in buf.getvalue(), buf.getvalue()


# ---------------------------------------------------------------------------
# End-to-end smoke test of the REAL invocation shape: `python -m
# app.startup_checks`, reading Settings from the environment exactly like
# scripts/docker-entrypoint.sh will run it. Everything above drives main()
# directly with an injected Settings object (fast, precise); these two prove
# the `-m` / `if __name__ == "__main__"` wiring and env-var resolution
# actually work end to end, isolated from any real ipeds.db/app.db on this box.
# ---------------------------------------------------------------------------

def _run_entrypoint(extra_env):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "backend")
    # Pin every path setting explicitly so this can never read a real .env's
    # production paths (mirrors ci_env.sh's "pin everything that could bleed").
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "app.startup_checks"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=30)


def test_entrypoint_exits_0_on_a_clean_writable_tree():
    _require_module()
    root = Path(tempfile.mkdtemp())
    r = _run_entrypoint({
        "APP_DB_PATH": str(root / "state" / "app.db"),
        "IPEDS_DB_PATH": str(root / "ipeds" / "ipeds.db"),
        "LOG_DB_PATH": str(root / "state" / "logs.db"),
        "DATA_DIR": str(root / "data"),
        "UPLOAD_DIR": str(root / "uploads"),
        "NCES_WORK_DIR": str(root / "work"),
    })
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "Traceback" not in r.stderr, r.stderr


def test_entrypoint_exits_1_on_an_unwritable_tree():
    """The genuine `set -e`-stopping contract: docker-entrypoint.sh relies on
    this exact process's exit code to decide whether to `exec uvicorn` at
    all."""
    _require_module()
    if _skip_if_root("entrypoint_exits_1_on_an_unwritable_tree"):
        return
    root = Path(tempfile.mkdtemp())
    ro_dir = root / "uploads"
    ro_dir.mkdir()
    ro_dir.chmod(0o555)
    try:
        r = _run_entrypoint({
            "APP_DB_PATH": str(root / "state" / "app.db"),
            "IPEDS_DB_PATH": str(root / "ipeds" / "ipeds.db"),
            "LOG_DB_PATH": str(root / "state" / "logs.db"),
            "DATA_DIR": str(root / "data"),
            "UPLOAD_DIR": str(ro_dir),
            "NCES_WORK_DIR": str(root / "work"),
        })
        assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert "Traceback" not in r.stderr, r.stderr
        assert str(ro_dir) in r.stderr, r.stderr
    finally:
        ro_dir.chmod(0o755)


def run():
    print("Startup preflight (app/startup_checks.py):")
    check("clean tree passes and creates missing directories",
          test_clean_tree_passes_and_creates_missing_directories)
    check("read-only directory is reported with its path",
          test_read_only_directory_is_reported_with_its_path)
    check("writable dir with read-only app.db is reported, naming app.db",
          test_writable_dir_with_read_only_app_db_is_reported_naming_app_db)
    check("probe_directory creates a missing directory",
          test_probe_directory_creates_a_missing_directory)
    check("probe_directory passes for an existing writable directory",
          test_probe_directory_passes_for_an_existing_writable_directory)
    check("probe_directory reports a read-only directory",
          test_probe_directory_reports_a_read_only_directory)
    check("probe_file skips a file that does not exist yet",
          test_probe_file_skips_a_file_that_does_not_exist_yet)
    check("probe_file passes for an existing writable file",
          test_probe_file_passes_for_an_existing_writable_file)
    check("probe_file reports a read-only existing file",
          test_probe_file_reports_a_read_only_existing_file)
    check("every path setting is covered by a probe or explicitly excluded",
          test_every_path_setting_is_covered_by_a_probe_or_explicitly_excluded)
    check("schema_md_path is never probed", test_schema_md_path_is_never_probed)
    check("failure message has uid/gid, chown command, paths, no traceback",
          test_failure_message_has_uid_gid_chown_command_paths_no_traceback)
    check("main() returns 0 on a clean tree", test_main_returns_0_on_a_clean_tree)
    check("main() returns 1 on a broken tree", test_main_returns_1_on_a_broken_tree)
    check("main() never propagates an unexpected exception",
          test_main_never_propagates_an_unexpected_exception)
    check("entrypoint (`python -m app.startup_checks`) exits 0 on a clean tree",
          test_entrypoint_exits_0_on_a_clean_writable_tree)
    check("entrypoint exits 1 on an unwritable tree",
          test_entrypoint_exits_1_on_an_unwritable_tree)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL STARTUP-PREFLIGHT TESTS PASSED")


if __name__ == "__main__":
    run()
