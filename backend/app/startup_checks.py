"""Filesystem preflight, run as its own process BEFORE uvicorn ever imports
`app.main` -- see `scripts/docker-entrypoint.sh`.

WHY THIS IS A SEPARATE PROCESS, NOT A `lifespan` STARTUP HOOK: `app/main.py`
calls `logbuffer.install()` at IMPORT TIME (module level), and
`SqliteLogHandler.__init__` (`app/logbuffer.py`) does
`Path(db_path).parent.mkdir(parents=True, exist_ok=True)` then
`sqlite3.connect(...)` with no try/except around either call. An unwritable
data directory therefore raises `sqlite3.OperationalError: unable to open
database file` from INSIDE uvicorn's import of `app.main:app`, before
`lifespan` is ever reached -- a preflight living inside the app can't
intercept a crash that happens while the app is still being imported. This
module resolves `Settings` and probes the filesystem directly, WITHOUT
importing `app.main`, so it can run and fail cleanly first.

This lands ahead of the container switching to a non-root uid. Docker never
chowns a bind mount for you, so on that upgrade an operator's existing
./srv-data is root-owned and unwritable -- turning what used to be a bare
traceback several frames deep into a message naming the exact paths, the uid
the container is running as, and the `chown` command to fix it.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from app.config import Settings, get_settings

# The one deliberate opt-out. Every other Path-shaped setting must be reachable
# through directories()/files() below -- see the drift test in
# backend/tests/test_startup_checks.py, which derives the candidate set
# structurally from Settings.model_fields rather than trusting this list.
EXCLUDED_SETTINGS: dict[str, str] = {
    "schema_md_path": (
        "docs/SCHEMA.md ships read-only inside the image and is only ever "
        "READ (injected into every agent prompt) -- the app never writes it, "
        "so it must never be probed as writable."
    ),
}


def probe_directory(path: Path) -> str | None:
    """Create `path` (and any missing parents) if needed, then prove it's
    genuinely writable with a real write-and-unlink -- never `os.access`,
    which lies about NFS root-squash, POSIX ACLs, and immutable flags.
    Returns None on success, else a message naming `path`.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path)
        try:
            os.close(fd)
        finally:
            os.unlink(tmp_name)
    except OSError as e:
        reason = e.strerror or str(e)
        return f"{path} is not writable ({reason})"
    return None


def probe_file(path: Path) -> str | None:
    """Probe an EXISTING file for write access by actually opening it for
    read+write ('r+b', never plain 'r' -- a read-only open would never notice
    a file it can't write, silently reproducing the bug this module exists to
    catch). A file that hasn't been created yet is skipped, not a failure --
    that's the app's own init_db()/logbuffer.install() to create, once the
    containing directory has already probed writable. Returns None on
    success/absence, else a message naming `path`.
    """
    if not path.exists():
        return None
    try:
        with open(path, "r+b"):
            pass
    except OSError as e:
        reason = e.strerror or str(e)
        return f"{path} is not writable ({reason})"
    return None


def directories(settings: Settings) -> list[Path]:
    """Every directory the app must be able to create files in. The ipeds.db
    integrate/rebuild swap renames a staging file into ipeds_db_path's
    directory, and db.py writes `app.db.pre-v<N>` snapshots beside app.db --
    both need their PARENT directory writable, not just the db file itself.
    De-duplicated (LOG_DB_PATH left at its default sits beside app.db) while
    preserving a stable order for the failure list.
    """
    candidates = [
        settings.data_dir,
        settings.upload_dir,
        settings.nces_work_dir,
        settings.app_db_path.parent,
        settings.ipeds_db_path.parent,
        settings.resolved_log_db_path.parent,
    ]
    return list(dict.fromkeys(candidates))


def files(settings: Settings) -> list[Path]:
    """Existing files a chowned-directory-but-not-files upgrade can still
    leave root-owned and unwritable -- the case a directory-only probe
    misses. Deliberately narrow: just the two SQLite stores the app opens for
    writing outside of any request (app.db at import time via logbuffer,
    logs.db the same way).
    """
    return [settings.app_db_path, settings.resolved_log_db_path]


def run_checks(settings: Settings) -> list[str]:
    """Probe every directory then every file, returning one message per
    failure (empty list = a clean, writable tree)."""
    failures: list[str] = []
    for d in directories(settings):
        problem = probe_directory(d)
        if problem:
            failures.append(problem)
    for f in files(settings):
        problem = probe_file(f)
        if problem:
            failures.append(problem)
    return failures


def _format_failure_message(failures: list[str]) -> str:
    uid, gid = os.getuid(), os.getgid()
    lines = [
        "Startup preflight failed: the app cannot write to the following path(s):",
        "",
    ]
    lines.extend(f"  - {f}" for f in failures)
    lines.extend([
        "",
        f"This container is running as uid={uid} gid={gid}. The usual cause is a "
        "data directory (mounted at /data inside the container -- the shipped "
        "compose default is ./srv-data on the host) that is still owned by a "
        "different user, e.g. because it existed before the image switched to "
        "a non-root user. Docker does not chown a bind mount for you.",
        "",
        "Fix it from the host by running:",
        "",
        f"  sudo chown -R {uid}:{gid} /data   # or ./srv-data, whatever you mounted",
        "",
    ])
    return "\n".join(lines)


def main(settings: Settings | None = None) -> int:
    """Entry point for `python -m app.startup_checks`. Never lets an
    exception escape -- including a bug inside run_checks() itself, not just
    an anticipated filesystem OSError -- so docker-entrypoint.sh's `set -e`
    sees a clean exit code, never a raw traceback, either way.
    """
    try:
        resolved = settings if settings is not None else get_settings()
        failures = run_checks(resolved)
    except Exception as e:  # noqa: BLE001 -- deliberately broad, see docstring
        failures = [f"internal error while running the startup preflight: {e!r}"]

    if not failures:
        return 0

    print(_format_failure_message(failures), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
