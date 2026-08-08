"""Admin data import: validate a new IPEDS .accdb, rebuild into a STAGING
database, run integrity + magnitude checks, and only then atomically swap it in.
The live ipeds.db is never written in place, so a bad import can't corrupt it.

The heavy lifting reuses scripts/build_ipeds_db.py unchanged (it already accepts
--out and does a safe full rebuild).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import NamedTuple

import app.nces as nces
from app import estimate
from app.config import get_settings
from app.db import connect, set_meta
from app.skills import invalidate_cache
from app.tools.sql import run_sql

log = logging.getLogger("ipeds.importer")

FILENAME_RE = re.compile(r"^IPEDS(\d{4})(\d{2})\.accdb$", re.IGNORECASE)
REQUIRED_FAMILIES = ("c_a", "hd", "valuesets", "vartable", "_years")
# The Imports tab polls every 2s (frontend/src/Admin.jsx) — persisting per-year
# progress more often than that is pure overhead. See _ProgressWriter below.
PROGRESS_MIN_INTERVAL_SECONDS = 1.5
# scripts/build_ipeds_db.py emits these machine-readable lines (in addition to
# its normal human-readable prints) so build_check_swap can drive a
# determinate rebuild progress bar without screen-scraping the log text.
PROGRESS_MARKER_RE = re.compile(r"^##PROGRESS## (\w+)=(\d+)$")


class NCESFetchError(RuntimeError):
    """Raised by run_integrate's fetch loop when an NCES year can't be
    retrieved. Its message is already deliberately worded for the job report
    (which year, why, that the live DB is unchanged), so the outer handler
    must pass it through verbatim rather than prepending 'Unexpected
    error:' — that combination reads as self-contradictory."""


def _log(job_id: int, line: str) -> None:
    con = connect()
    try:
        con.execute("UPDATE import_jobs SET log = COALESCE(log,'') || ?, "
                    "updated_at=? WHERE id=?", (line + "\n", time.time(), job_id))
        con.commit()
    finally:
        con.close()


def _write_progress_json(job_id: int, payload: str) -> None:
    """The raw DB write behind _set_progress — takes an already-serialized
    JSON string so a caller can serialize under a lock and write outside it
    (see _ProgressThrottle below)."""
    con = connect()
    try:
        con.execute("UPDATE import_jobs SET progress=?, updated_at=? WHERE id=?",
                    (payload, time.time(), job_id))
        con.commit()
    finally:
        con.close()


def _set_progress(job_id: int, progress: dict) -> None:
    """Persist the structured per-year JSON progress blob the Imports tab
    polls (import_jobs.progress). Shape: {"overall": {"phase", "message"},
    "years": {"<start_year>": {start_year, year_label, step,
    downloaded_bytes, total_bytes, pct}}}."""
    _write_progress_json(job_id, json.dumps(progress))


class _ProgressThrottle:
    """Coalesces run_integrate's per-chunk progress callback so it doesn't
    serialize a full-DB-connection UPDATE+commit on every streamed chunk —
    across nces_download_concurrency concurrent downloads that's tens of
    thousands of commits, which collapses throughput and can raise `database
    is locked` past app.db's busy_timeout (which would otherwise abort the
    whole transfer/integrate for what's purely a progress-display concern).

    persist() only actually writes when the integer pct for that year has
    changed OR PROGRESS_MIN_INTERVAL_SECONDS has passed since its last
    write (force=True bypasses both, for one-off step transitions like
    "downloading"/"fetched"/"failed"). The shared `progress` dict is mutated
    and serialized to a JSON string WHILE HOLDING `lock` (so concurrent
    per-year threads can't tear a read of it), but the DB write itself always
    happens AFTER releasing the lock — and a failure there is logged and
    swallowed, never re-raised, so it can never abort the transfer/integrate
    it's merely reporting on."""

    def __init__(self, job_id: int, progress: dict):
        self.job_id = job_id
        self.progress = progress
        self.lock = threading.Lock()
        self._last: dict[int, tuple[int, float]] = {}

    def persist(self, sy: int, *, force: bool = False) -> None:
        now = time.time()
        with self.lock:
            entry = self.progress["years"][str(sy)]
            last_pct, last_t = self._last.get(sy, (None, 0.0))
            if not force and entry["pct"] == last_pct and \
                    (now - last_t) < PROGRESS_MIN_INTERVAL_SECONDS:
                return
            self._last[sy] = (entry["pct"], now)
            payload = json.dumps(self.progress)
        try:
            _write_progress_json(self.job_id, payload)
        except Exception as e:  # noqa: BLE001 — progress display only, never fatal
            log.warning("could not persist import progress for job %s: %s: %s",
                       self.job_id, type(e).__name__, e)


def _record_provenance(rows: list[tuple[int, int, str | None, str]]) -> None:
    """Record (or update) each (start_year, end_year, release, source) row in
    year_provenance. Called ONLY after a successful swap — run_import passes
    a single ('manual', release=None) row; run_integrate passes one
    ('nces', release=<actual release fetched>) row per union year."""
    con = connect()
    try:
        now = time.time()
        for start_year, end_year, release, source in rows:
            con.execute(
                "INSERT INTO year_provenance(start_year, end_year, release, source, updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(start_year) DO UPDATE SET "
                "end_year=excluded.end_year, release=excluded.release, "
                "source=excluded.source, updated_at=excluded.updated_at",
                (start_year, end_year, release, source, now))
        con.commit()
    finally:
        con.close()


def _update_overall_phase(job_id: int, phase: str, message: str) -> None:
    """Update just the progress["overall"] phase/message, preserving whatever
    per-year progress (if any) is already there. Used by build_check_swap so
    it composes with run_integrate's per-year progress structure without
    needing to know about it, and works fine standalone for run_import (which
    never populates "years" at all)."""
    con = connect()
    try:
        row = con.execute(
            "SELECT progress FROM import_jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        con.close()
    progress = None
    if row and row["progress"]:
        try:
            progress = json.loads(row["progress"])
        except ValueError:
            progress = None
    if not isinstance(progress, dict):
        progress = {}
    progress.setdefault("years", {})
    progress["overall"] = {"phase": phase, "message": message}
    _set_progress(job_id, progress)


def _update_rebuild_progress(job_id: int, tables_total: int, tables_done: int) -> None:
    """Update just the progress["rebuild"] block ({tables_total, tables_done,
    pct}), preserving whatever overall/years progress is already there — same
    read-merge-write shape as _update_overall_phase, just for the loader's
    ##PROGRESS## markers (see build_check_swap). The caller (build_check_swap)
    throttles calls to once per integer-pct change; this function itself
    always writes when called."""
    con = connect()
    try:
        row = con.execute(
            "SELECT progress FROM import_jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        con.close()
    progress = None
    if row and row["progress"]:
        try:
            progress = json.loads(row["progress"])
        except ValueError:
            progress = None
    if not isinstance(progress, dict):
        progress = {}
    progress.setdefault("years", {})
    pct = int(tables_done * 100 / tables_total) if tables_total else 0
    progress["rebuild"] = {"tables_total": tables_total, "tables_done": tables_done, "pct": pct}
    _set_progress(job_id, progress)


def _human_bytes(n: float) -> str:
    """A short human-readable byte size for job-report messages (e.g. '6.4 GB')."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def _set_status(job_id: int, status: str, report: str | None = None) -> None:
    con = connect()
    try:
        if report is None:
            con.execute("UPDATE import_jobs SET status=?, updated_at=? WHERE id=?",
                        (status, time.time(), job_id))
        else:
            con.execute("UPDATE import_jobs SET status=?, report=?, updated_at=? WHERE id=?",
                        (status, report, time.time(), job_id))
        con.commit()
    finally:
        con.close()


TERMINAL_JOB_STATUSES = ("failed", "swapped")


def reconcile_interrupted_jobs() -> int:
    """Mark any import job left mid-flight by a process exit as `failed`.

    The rebuild runs on a daemon `threading.Thread`, and `_set_status(...,
    "failed")` only runs from that thread's own `except`. So a SIGKILL, an OOM
    kill, a host reboot, or an ordinary `docker compose pull && up -d` leaves the
    row at `running`/`checks` forever -- nothing has ever reconciled it.

    That used to be cosmetic: one stale line at the bottom of the jobs table.
    It stopped being cosmetic when Admin -> Imports began ADOPTING a non-terminal
    job on mount, because then EVERY later visit to the tab adopts the ghost,
    `locked` is true, and every control -- year cards, integrate, upload, the
    remove trashcans -- is disabled with a notice saying an import is running,
    permanently, with no dismiss. Meanwhile `_import_lock` is a process-level
    lock the restart already released, so the server would happily accept the
    very import the UI is refusing. Recovery was hand-editing app.db.

    Safe precisely BECAUSE the worker is a daemon thread: it cannot outlive the
    process, so anything non-terminal at boot is definitionally dead. Returns the
    number reconciled so the caller can log it.

    Honest limit: that is a property of the SINGLE-PROCESS deployment, not of
    this code. `docker-entrypoint.sh` runs uvicorn with no `--workers`, and this
    runs inside `lifespan` before the app serves, so no request in this process
    can have created a job yet. With two processes on one app.db, B's boot would
    mark A's LIVE import failed and append a "nothing was swapped" note that is
    false. That topology already breaks `_import_lock` (a process-local
    threading.Lock) and is unsupported — but the constraint belongs in writing.
    """
    con = connect()
    try:
        placeholders = ",".join("?" for _ in TERMINAL_JOB_STATUSES)
        cur = con.execute(
            f"UPDATE import_jobs SET status='failed', updated_at=?, "
            f"report=COALESCE(report,'') || ? "
            f"WHERE status NOT IN ({placeholders})",
            (time.time(),
             "\n\nInterrupted: the server restarted while this job was running. "
             "Nothing was swapped into the live database — start it again.",
             *TERMINAL_JOB_STATUSES))
        con.commit()
        return cur.rowcount or 0
    finally:
        con.close()


def create_job(filename: str, created_by: str) -> int:
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO import_jobs(filename, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?)", (filename, "pending", created_by, time.time(), time.time()))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


class _Staged(NamedTuple):
    """One data_dir staging record. `backup` is set ONLY after the
    move-aside actually completed (never just the computed .bak path), and
    `existed_before` records whether *anything* was sitting at `target`
    before this job touched it. Both are needed to tell apart three distinct
    _restore_data_dir cases — see its docstring."""
    target: Path
    backup: Path | None
    existed_before: bool


def _restore_data_dir(data_target: Path, backup_accdb: Path | None, *,
                      existed_before: bool = False) -> None:
    """Undo the data_dir staging done before a rebuild so a failed import
    doesn't leave a bad/corrupt .accdb sitting in the loader's data dir for a
    later rebuild to pick up.

    Two branches, both guarded more loosely than "backup_accdb is
    None/not-None" on purpose:
      - `backup_accdb and backup_accdb.exists()`: restore from it.
        `shutil.move` first tries `os.rename(src, dst)`; if THAT raises for
        ANY reason (not only crossing a filesystem boundary — an
        in-directory rename can also fail, e.g. a target name that lands
        past `NAME_MAX`), it falls back to `copy_function(src, dst)` (here
        `copy2`) followed by `os.unlink(src)` — verified against
        `shutil.move`'s own source, which has no in-directory special case.
        Either way, removing the source is the LAST step of every path
        through `shutil.move`, so whenever the move-aside itself raises,
        the source is still there — no partial move is possible. That's
        what makes `existed_before`, not `backup_accdb is None`, the right
        test for "was the original left under its own name".
      - otherwise, `existed_before`: True leaves `data_target` alone
        (nothing to restore, and nothing this job is entitled to remove —
        either the move-aside never ran/never finished, per above, or a
        backup WAS recorded but its `.bak` is no longer on disk, e.g.
        `run_import`'s `_restore_all()` already restored it on an earlier
        pass through this same entry: it runs inside `run_import`'s `try`,
        so a raise from `_restore_data_dir` itself on a later entry lands
        back in the `except` and replays the whole loop, including entries
        already restored). False unlinks `data_target` if present — nothing
        was there before, so only this job could have put a file there (a
        first-time year, or a partial copy that failed mid-write).

    One residual leak the move-aside branch of `shutil.move` can still
    cause: `copy_function(src, dst)` succeeds (so `.bak` now holds a full
    copy of the original), but the following `os.unlink(src)` then raises —
    `shutil.move` as a whole still raises, so `run_import`'s staging loop
    never reaches the point of recording that path as `backup` in `staged`,
    and this function is called with `backup_accdb=None`,
    `existed_before=True` and leaves `data_target` (still its original,
    untouched, since the unlink that would have removed it failed) alone —
    correctly. The full-size `.bak` copy is then just disk-space overhead,
    not a correctness problem: `_data_dir_years`' `*.accdb` glob does not
    match a `.bak`, so it's inert to `_guard_no_dropped_years`."""
    if backup_accdb and backup_accdb.exists():
        shutil.move(str(backup_accdb), str(data_target))
    elif existed_before:
        return  # nothing to restore/remove — see the docstring's two cases
    elif data_target.exists():
        data_target.unlink(missing_ok=True)


def _unlink_quietly(job_id: int, what: str, path: Path) -> None:
    """Best-effort delete of a leftover .accdb (an uploaded copy, or a file
    backed up before staging) once it's no longer needed. Logs a warning
    instead — this must NEVER raise, and the reason differs by caller:

    - called from run_import's success branch (inside its own `try`), an
      escaping error would fall into the outer `except Exception` at
      run_import's tail and report an import that is ALREADY LIVE as failed,
      naming a stray file as the cause. (It would not also roll data_dir
      back: `swapped` is set by then, so _restore_all no-ops — that guard is
      what downgraded this from data loss to a wrong status.)
    - called from run_import's `finally`, an escaping error would mask
      whatever exception is already propagating out of the daemon thread
      run_import runs on, with no caller left to see it.

    Mirrors _ProgressThrottle.persist's precedent above: display-or-disk
    housekeeping never decides a job's outcome. Catches ANY exception, not
    just OSError, for the same reason: one call site sits in a `finally`
    wrapping `_record_provenance`, and a non-OSError escaping there would
    REPLACE whatever exception `_record_provenance` was already raising —
    making the job report name this cleanup as the failure instead of the
    real one."""
    try:
        path.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001 — best-effort cleanup, never fatal
        log.warning("could not remove %s for job %s (%s): %s: %s",
                    what, job_id, path, type(e).__name__, e)


def preflight(upload_path: Path) -> tuple[bool, str]:
    """Cheap checks before the (slow) rebuild."""
    name = upload_path.name
    if not FILENAME_RE.match(name):
        return False, (f"Filename '{name}' must match IPEDS{{YYYY}}{{YY}}.accdb "
                       "(e.g. IPEDS202526.accdb) — the loader keys on this.")
    try:
        out = subprocess.run(["mdb-tables", "-1", str(upload_path)],
                             capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        return False, f"Could not read the Access file with mdb-tables: {e}"
    tables = [t.lower() for t in out.stdout.split()]
    if not any(t.startswith("c") and "_a" in t for t in tables):
        return False, "No Completions (C…_A) table found — is this a full IPEDS file?"
    if not any(t.startswith("hd") for t in tables):
        return False, "No HD (directory) table found."
    return True, f"Preflight OK — {len(tables)} tables found."


def _family_counts(db_path: Path) -> dict[str, int]:
    r = run_sql("SELECT family, SUM(n_rows) AS n FROM _family_map GROUP BY family",
                limit=1000, db_path=db_path)
    return {row[0]: row[1] for row in r.rows}


def _years(db_path: Path) -> list[int]:
    r = run_sql("SELECT year FROM _years ORDER BY year", limit=100, db_path=db_path)
    return [row[0] for row in r.rows]


def _family_year_counts(db_path: Path) -> dict[int, int]:
    """Per-year SUM(n_rows) from _family_map, across every family — a
    bookkeeping-consistency check used by deintegrate_checks. NOTE: this is
    NOT proof the actual data rows are untouched — the year-removal DELETE
    never touches _family_map rows for surviving years, so this can only
    catch a bug that corrupted the bookkeeping table itself, not an
    over/under-deletion of real data (see _core_family_row_count for that)."""
    r = run_sql("SELECT year, SUM(n_rows) AS n FROM _family_map GROUP BY year",
                limit=1000, db_path=db_path)
    return {row[0]: row[1] for row in r.rows}


def _core_family_row_count(db_path: Path, year: int) -> int | None:
    """REAL-DATA assurance: actual row count in the core `c_a` family for one
    `year`, straight off the physical table (not the _family_map bookkeeping
    copy, which the removal DELETE never touches for surviving years and so
    can't detect over/under-deletion of actual rows). Returns None if `c_a`
    is somehow absent — it's a REQUIRED_FAMILY, so this should never happen
    outside a badly corrupt fixture, but the caller must not crash on it."""
    try:
        r = run_sql(f"SELECT COUNT(*) FROM c_a WHERE year={int(year)}",
                    limit=1, db_path=db_path)
    except Exception:  # noqa: BLE001 — treated as "can't confirm", not a crash
        return None
    return r.rows[0][0] if r.rows else None


def _associates_latest(db_path: Path) -> int | None:
    r = run_sql("SELECT SUM(ctotalt) FROM c_a WHERE awlevel=3 AND majornum=1 "
                "AND cipcode='99' AND year=(SELECT MAX(year) FROM _years)",
                limit=1, db_path=db_path)
    return r.rows[0][0] if r.rows and r.rows[0][0] is not None else None


def integrity_checks(staging: Path, live: Path | None) -> tuple[bool, list[str]]:
    """Compare the freshly-built staging DB to the current live DB."""
    report: list[str] = []
    ok = True

    fams = _family_counts(staging)
    for fam in REQUIRED_FAMILIES:
        present = fam in fams or fam == "_years"
        if fam == "_years":
            present = bool(_years(staging))
        if not present:
            ok = False
            report.append(f"✗ required family/object missing or empty: {fam}")
    if all((f in fams or f == "_years") for f in REQUIRED_FAMILIES):
        report.append("✓ required families present")

    new_years = _years(staging)
    report.append(f"years in staging: {new_years}")
    if len(new_years) < 1:
        ok = False
        report.append("✗ no years loaded")

    assoc = _associates_latest(staging)
    if assoc is None:
        ok = False
        report.append("✗ could not compute national associate's total")
    elif not (600_000 <= assoc <= 1_400_000):
        ok = False
        report.append(f"✗ national associate's total {assoc:,} outside sane range "
                      "(600k–1.4M) — likely an aggregation/load problem")
    else:
        report.append(f"✓ national associate's total {assoc:,} (sane)")

    if live and live.exists():
        old = _family_counts(live)
        old_years = _years(live)
        if new_years and old_years and max(new_years) <= max(old_years):
            report.append(f"⚠ staging max year {max(new_years)} is not newer than "
                          f"live {max(old_years)} — a rebuild without a new year?")
        # flag any family that lost >20% of its rows
        for fam, oldn in old.items():
            newn = fams.get(fam, 0)
            if oldn > 1000 and newn < oldn * 0.8:
                ok = False
                report.append(f"✗ family {fam} shrank {oldn:,} → {newn:,} (>20% drop)")
        report.append("✓ per-family row-count comparison done")
    else:
        report.append("(no existing live DB to compare against — first build)")

    return ok, report


def deintegrate_checks(staging: Path, live: Path, removed_year: int) -> tuple[bool, list[str]]:
    """De-integration-specific integrity checks for run_deintegrate.

    Deliberately NOT integrity_checks — its >20%-family-shrink rule exists to
    catch an accidental data loss on IMPORT, and would falsely fail a
    deliberate year removal (which is exactly a big, intentional shrink).
    Instead: confirm the required families/objects are still present, the
    removed year's rows are ACTUALLY gone from the core `c_a` table (a
    real-data check — the _family_map bookkeeping comparison below can't
    prove this on its own, see _core_family_row_count), no OTHER year was
    lost in the process, at least one year remains, every surviving year's
    _family_map bookkeeping is internally consistent, and the new max year's
    associate's total is still sane."""
    report: list[str] = []
    ok = True

    fams = _family_counts(staging)
    missing = [f for f in REQUIRED_FAMILIES if f != "_years" and f not in fams]
    new_years = _years(staging)
    if missing or not new_years:
        ok = False
        report.append("✗ required family/object missing or empty: "
                      + (", ".join(missing) if missing else "_years"))
    else:
        report.append("✓ required families present")

    report.append(f"years in staging: {new_years}")

    if removed_year in new_years:
        ok = False
        report.append(f"✗ removed year {removed_year} is still present in staging")
    else:
        report.append(f"✓ removed year {removed_year} is gone from _years")

    # REAL-DATA check: the removed year's rows must actually be gone from the
    # core c_a table — _family_map's bookkeeping (below) never gets touched
    # for surviving years by the removal DELETE, so it can't by itself prove
    # actual over/under-deletion of real rows.
    core_remaining = _core_family_row_count(staging, removed_year)
    if core_remaining is None:
        ok = False
        report.append("✗ could not confirm removal — c_a table missing or unreadable in staging")
    elif core_remaining > 0:
        ok = False
        report.append(f"✗ removed year {removed_year} still has {core_remaining:,} rows in c_a")
    else:
        report.append(f"✓ removed year {removed_year} has 0 rows in c_a")

    old_years = _years(live)
    expected_years = [y for y in old_years if y != removed_year]
    if set(new_years) != set(expected_years):
        ok = False
        report.append(f"✗ surviving years {sorted(new_years)} do not match expected "
                      f"{sorted(expected_years)} — more than the removed year changed")

    if not new_years:
        ok = False
        report.append("✗ no years remain after removal — the database would be empty")

    staging_by_year = _family_year_counts(staging)
    live_by_year = _family_year_counts(live)
    mismatches = [y for y in expected_years if staging_by_year.get(y) != live_by_year.get(y)]
    if mismatches:
        ok = False
        for y in mismatches:
            report.append(f"✗ surviving year {y} bookkeeping row count changed: "
                          f"{live_by_year.get(y)} -> {staging_by_year.get(y)}")
    elif expected_years:
        report.append("✓ surviving years' bookkeeping row counts consistent")

    assoc = _associates_latest(staging)
    if assoc is None:
        ok = False
        report.append("✗ could not compute national associate's total after removal")
    elif not (600_000 <= assoc <= 1_400_000):
        ok = False
        report.append(f"✗ national associate's total {assoc:,} outside sane range "
                      "(600k–1.4M) after removal — likely an aggregation problem")
    else:
        report.append(f"✓ national associate's total {assoc:,} (sane) after removal")

    return ok, report


def _activate_staging(job_id: int, staging: Path,
                      done_message: str = "Import succeeded and is now live.",
                      *, on_swapped: Callable[[], None] | None = None) -> None:
    """Atomic swap: move live aside, move staging -> live, DELETE the moved-aside
    copy, bump data_version, invalidate the semantic cache. Shared swap tail used by
    BOTH build_check_swap (import/integrate) and run_deintegrate (year
    removal) — the only difference between callers is what happened before
    this point (a full rebuild vs. an offline in-place delete + VACUUM).

    `on_swapped` (keyword-only, added after `done_message` so run_deintegrate's
    existing positional call is unaffected) fires the instant the staging ->
    live move below completes — BEFORE `prev.unlink`, the app.db data_version
    write, or cache invalidation, any of which can still raise. That's the
    only truthful place to mark "the swap happened": everything after it is
    real work but is no longer reversible-worthy, and a caller that instead
    waits for this function to return without raising would miss a swap
    followed by a failure in one of those later steps (run_import's `swapped`
    flag exists exactly to avoid that)."""
    s = get_settings()
    _update_overall_phase(job_id, "swapping", "Swapping the staging database into place…")
    prev = s.ipeds_db_path.with_suffix(".db.prev")
    if s.ipeds_db_path.exists():
        shutil.move(str(s.ipeds_db_path), str(prev))
    shutil.move(str(staging), str(s.ipeds_db_path))
    if on_swapped is not None:
        on_swapped()
    # The .prev copy exists ONLY to make the two-step move recoverable: if the
    # process died between them, the old database is still on disk under that
    # name. Once staging is in place that window is closed, so drop it —
    # ipeds.db is ~2 GB and REBUILDABLE from the .accdb sources (the README says
    # so, which is why it isn't backed up), and nothing ever deleted this, so a
    # long-running deployment silently carried two full datasets forever.
    #
    # Safe to unlink with queries in flight: the swap is a rename, so an
    # already-open connection holds the old INODE — unlinking removes the name,
    # not the file, and that reader finishes normally.
    prev.unlink(missing_ok=True)
    _log(job_id, "Swapped staging → live ipeds.db (previous copy removed)")

    con = connect()
    try:
        dv = int((con.execute(
            "SELECT value FROM meta WHERE key='data_version'").fetchone() or [1])[0])
        set_meta(con, "data_version", str(dv + 1))
        con.commit()
    finally:
        con.close()
    invalidate_cache()
    _log(job_id, "Bumped data_version and cleared semantic cache.")
    _update_overall_phase(job_id, "done", done_message)


def build_check_swap(job_id: int, data_dir: Path, *,
                     on_swapped: Callable[[], None] | None = None) -> bool:
    """The core rebuild pipeline, shared by run_import (an uploaded .accdb
    staged into `data_dir`) and run_integrate (a temp work dir holding a whole
    union of fetched .accdb files): full rebuild of `data_dir` into a staging
    DB (reusing scripts/build_ipeds_db.py unchanged), integrity + magnitude
    checks, and — only on success — the atomic swap + data_version bump +
    semantic-cache invalidation.

    Returns True on a completed swap, False on a handled failure (the job row
    is already marked 'failed' with a report; the caller decides what else,
    if anything, needs cleaning up). Unexpected exceptions are NOT caught here
    — they propagate to the caller, which mirrors run_import's/run_integrate's
    own top-level except-and-fail-the-job handling — and that includes any
    raised AFTER `_activate_staging` has already moved staging into place, so
    a truthful "did the swap happen" signal cannot be this function's return
    value. `on_swapped` (forwarded to `_activate_staging`, by KEYWORD — a
    third positional argument would bind to `_activate_staging`'s
    `done_message`) is that signal instead: it fires at the swap itself, not
    at this function's return.
    """
    s = get_settings()
    staging = s.ipeds_db_path.with_name("ipeds_staging.db")

    # Full rebuild into a staging DB (reuse the loader unchanged).
    _update_overall_phase(job_id, "building", "Rebuilding the staging database…")
    _log(job_id, "Rebuilding staging database (this can take several minutes)…")
    build = Path(__file__).resolve().parents[2] / "scripts" / "build_ipeds_db.py"
    proc = subprocess.Popen(
        [sys.executable, str(build), "--data-dir", str(data_dir),
         "--out", str(staging)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    # Stream loader output (incl. collision warnings) into the human log —
    # except ##PROGRESS## marker lines, which are parsed into
    # progress["rebuild"] for the determinate rebuild bar and kept OUT of the
    # log (they're not meant for a human to read). Writes are throttled to
    # once per integer-pct change so hundreds of tables don't mean hundreds
    # of DB writes.
    rebuild_total = 0
    rebuild_done = 0
    last_pct = None
    for line in proc.stdout:
        line = line.rstrip()
        m = PROGRESS_MARKER_RE.match(line)
        if m or line.startswith("##PROGRESS##"):
            if m:
                key, val = m.group(1), int(m.group(2))
                if key == "tables_total":
                    rebuild_total, rebuild_done = val, 0
                elif key == "tables_done":
                    rebuild_done = val
                pct = int(rebuild_done * 100 / rebuild_total) if rebuild_total else 0
                if pct != last_pct:
                    last_pct = pct
                    _update_rebuild_progress(job_id, rebuild_total, rebuild_done)
            continue
        _log(job_id, line)
    proc.wait()
    if proc.returncode != 0:
        _set_status(job_id, "failed", f"Loader exited with code {proc.returncode}.")
        _update_overall_phase(job_id, "failed", f"Loader exited with code {proc.returncode}.")
        return False

    # Integrity + magnitude checks.
    _set_status(job_id, "checks")
    _update_overall_phase(job_id, "checking", "Running integrity + magnitude checks…")
    _log(job_id, "Running integrity checks…")
    passed, report = integrity_checks(
        staging, s.ipeds_db_path if s.ipeds_db_path.exists() else None)
    report_text = "\n".join(report)
    _log(job_id, report_text)
    if not passed:
        _set_status(job_id, "failed",
                    "Integrity checks FAILED — live DB untouched.\n\n" + report_text)
        _update_overall_phase(job_id, "failed", "Integrity checks failed — live DB untouched.")
        staging.unlink(missing_ok=True)
        return False

    # Atomic swap + data_version bump + semantic-cache invalidation (shared
    # with run_deintegrate — see _activate_staging).
    _activate_staging(job_id, staging, on_swapped=on_swapped)
    _set_status(job_id, "swapped", "Import succeeded and is now live.\n\n" + report_text)
    return True


def _year_label(ending_year: int) -> str:
    """Ending year (2021) -> the collection label ("2020-21")."""
    return f"{ending_year - 1}-{str(ending_year)[2:]}"


def _data_dir_years(data_dir: Path) -> set[int]:
    """The set of ENDING years encoded in the IPEDS{YYYY}{YY}.accdb filenames
    sitting in data_dir (start year + 1, to match _years())."""
    years: set[int] = set()
    if data_dir.exists():
        for p in data_dir.glob("*.accdb"):
            m = FILENAME_RE.match(p.name)
            if m:
                years.add(int(m.group(1)) + 1)
    return years


def _guard_no_dropped_years(data_dir: Path, live_db: Path) -> tuple[bool, str]:
    """A manual rebuild replaces the dataset with EXACTLY the .accdb now in
    data_dir, so its year set must be a SUPERSET of the live years — otherwise
    the swap would silently drop a year. Refuse (before the multi-minute
    rebuild) if any live year is missing from the upload. No live DB yet = a
    first build, allowed."""
    if not live_db.exists():
        return True, ""
    try:
        live_years = set(_years(live_db))
    except Exception:  # noqa: BLE001 — an unreadable/corrupt live DB can't be guarded;
        return True, ""  # fail open (integrity_checks' shrink rule is the backstop).
    if not live_years:
        return True, ""
    dropped = sorted(live_years - _data_dir_years(data_dir))
    if not dropped:
        return True, ""
    return False, (
        "Upload refused — it would DROP year(s) currently in the database: "
        f"{', '.join(_year_label(y) for y in dropped)}. A manual upload rebuilds "
        "the database from exactly the files you provide, so include every year "
        f"you want to keep ({', '.join(_year_label(y) for y in sorted(live_years))}) "
        "plus any new ones — or use NCES Integrate to add a year online. Live "
        "database unchanged.")


def run_import(job_id: int, upload_paths: list[Path]) -> None:
    """Full pipeline for one or more uploaded .accdb files. Intended to run in a
    background thread. Preflights every file, stages them all into DATA_DIR
    (backing up any existing same-named files), refuses the rebuild if it would
    DROP a currently-live year (the superset guard), then hands off to
    build_check_swap — restoring DATA_DIR to its pre-import state on any failure
    before the swap happens (never after — see the `swapped` guard below).

    run_import also owns the WHOLE LIFETIME of `upload_paths`: it deletes each
    upload_dir copy on the way out, success or failure — mirroring
    run_integrate's documented "work dir is always removed afterward" contract.
    A function deleting its own argument is a surprising contract, so it's
    stated here explicitly. On SUCCESS the data_dir/<name>.accdb copy it staged
    survives as the loader's source; on failure that copy is reverted or
    removed by _restore_data_dir, which is the point of staging it."""
    s = get_settings()
    staged: list[_Staged] = []
    swapped = False  # True once build_check_swap has swapped in a new live db

    def _restore_all() -> None:
        # Once the swap has happened, data_dir already fed the NEW live
        # ipeds.db — restoring the old .accdb over it would leave data_dir
        # inconsistent with what's actually live, and the next manual
        # import's superset guard would then refuse a rebuild that includes
        # the very year already live (it can't see the swapped-in year in
        # data_dir, only the stale restored file). Restoring only ever made
        # sense while the live db was still the old one.
        if swapped:
            return
        for e in staged:
            _restore_data_dir(e.target, e.backup, existed_before=e.existed_before)

    def _discard_uploads() -> None:
        for up in upload_paths:
            # upload_dir and data_dir are both free-form settings with no
            # validator between them (config.py:90-91), so a self-hoster CAN
            # point UPLOAD_DIR at data_dir. In that configuration the
            # "uploaded copy" IS data_dir/<name>.accdb — the loader's live
            # source for that file — and deleting it would silently destroy
            # the dataset instead of just the leaked streaming copy. Compare
            # with .resolve() (not Path.samefile(), which raises
            # FileNotFoundError, and non-strict resolve() is safe even on the
            # preflight-failure exit, which returns before data_dir exists);
            # this also catches a symlinked upload_dir.
            #
            # .resolve() itself can raise (measured: a symlink loop raises
            # RuntimeError, not OSError, on this box's Python — so
            # _unlink_quietly's catch wouldn't help even wrapped around the
            # unlink alone). This runs from run_import's `finally`, so an
            # escape here would abort the loop mid-batch (every LATER upload
            # leaks) and mask whatever exception was already propagating out
            # of the daemon thread. Fail CLOSED: if we can't prove `up` is
            # NOT the loader's own data_dir source file, leaving a stray
            # upload on disk is strictly better than the alternative of
            # deleting the dataset on a wrong guess.
            try:
                aliased = up.resolve() == (s.data_dir / up.name).resolve()
            except Exception as e:  # noqa: BLE001 — see above: fail closed, keep going
                log.warning("could not resolve upload path for job %s (%s): "
                            "%s: %s — leaving it on disk", job_id, up,
                            type(e).__name__, e)
                continue
            if aliased:
                continue
            _unlink_quietly(job_id, "the uploaded copy", up)

    try:
        _set_status(job_id, "running")
        # Preflight every file first — reject the whole batch if any is bad.
        for up in upload_paths:
            _log(job_id, f"Preflight on {up.name}…")
            ok, msg = preflight(up)
            _log(job_id, msg)
            if not ok:
                _set_status(job_id, "failed", msg)
                return

        # Stage all where the loader discovers them (back up any existing ones).
        s.data_dir.mkdir(parents=True, exist_ok=True)
        staged_targets: set[Path] = set()  # resolved data_target paths seen so far
        for up in upload_paths:
            data_target = s.data_dir / up.name
            aliased = data_target.resolve() == up.resolve()
            # In the UPLOAD_DIR == DATA_DIR configuration `up` IS
            # data_target: no staging happens, this job changes NOTHING
            # about the file, and it must never be recorded — recording it
            # anyway would make a failed import's rollback call
            # _restore_data_dir(data_target, None), which UNLINKS the
            # admin's own dataset file that this job never touched.
            if aliased:
                continue
            # A multipart POST with two parts sharing a filename (routers/admin.py
            # builds `dest` from the filename alone and appends unconditionally)
            # stages the SAME data_target twice in one batch. Without this guard,
            # iteration 2 would see iteration 1's own just-copied upload sitting
            # at data_target, move IT aside onto the same .bak path — silently
            # OVERWRITING the .bak that still held the operator's true original
            # with a second copy of the upload — and leave no way to tell the
            # two apart afterward. Skip it: the first occurrence already staged
            # this target for the batch, and re-staging it can only destroy that
            # record.
            resolved = data_target.resolve()
            if resolved in staged_targets:
                continue
            staged_targets.add(resolved)
            existed = data_target.exists()
            # Record BEFORE mutating anything, so a raise from the
            # move-aside or the copy still leaves a record for _restore_all
            # to replay — that's the fix for the staging-record-after-
            # mutation bug (see this function's docstring). `backup` starts
            # None and is filled in only once the move-aside has actually
            # completed; `existed` is what lets _restore_data_dir tell "the
            # move-aside never completed, the original is still under its
            # own name" apart from "nothing was here before".
            staged.append(_Staged(data_target, None, existed))
            if existed:
                backup = data_target.with_suffix(".accdb.bak")
                shutil.move(str(data_target), str(backup))
                staged[-1] = staged[-1]._replace(backup=backup)
            shutil.copy2(str(up), str(data_target))
        _log(job_id, f"Staged {len(upload_paths)} source file(s) into {s.data_dir} "
                     "(the uploaded copies are temporary and are removed once "
                     "this job finishes, whatever the outcome)")

        # Superset guard — refuse a rebuild that would drop a live year.
        ok, msg = _guard_no_dropped_years(s.data_dir, s.ipeds_db_path)
        if not ok:
            _log(job_id, msg)
            _set_status(job_id, "failed", msg)
            _restore_all()
            return

        def _mark_swapped() -> None:
            # Fires from inside _activate_staging the instant the rename
            # completes, NOT from build_check_swap's return — see both of
            # their docstrings. Setting `swapped` from the return value left
            # it False whenever the swap tail raised after the rename, and
            # _restore_all() then rolled data_dir back under a live db built
            # from those very files.
            nonlocal swapped
            swapped = True

        if build_check_swap(job_id, s.data_dir, on_swapped=_mark_swapped):
            try:
                prov = []
                for e in staged:
                    m = FILENAME_RE.match(e.target.name)
                    if m:
                        start_year = int(m.group(1))
                        prov.append((start_year, start_year + 1, None, "manual"))
                if prov:
                    _record_provenance(prov)
            finally:
                # Once the swap has happened this backup can never usefully be
                # restored (see _restore_all above), so drop it here
                # regardless of whether recording provenance itself succeeded
                # — the same reasoning _activate_staging gives for dropping
                # ipeds.db.prev once its own swap window has closed. Read the
                # STORED backup Path rather than recomputing one: it is None
                # exactly when nothing was moved aside, so there is no second
                # rule about when a .bak exists to get wrong.
                for e in staged:
                    if e.backup is not None:
                        _unlink_quietly(job_id, "the backed-up .accdb", e.backup)
        else:
            _restore_all()
    except Exception as e:  # noqa: BLE001
        # _restore_all() runs FIRST, ahead of the log/status writes below —
        # each of those is its own real app.db connect()+commit(), with
        # nothing here catching either, so a raise from one of them (a WAL
        # commit onto a disk this same job just filled, or a plain
        # "database is locked" after the busy timeout) would otherwise skip
        # _restore_all() entirely and strand the rollback on the one
        # occasion it's needed most. The `swapped` guard inside
        # _restore_all makes this reordering safe: it's already a no-op
        # once a swap has happened, regardless of when it's called.
        _restore_all()
        _log(job_id, f"ERROR: {type(e).__name__}: {e}")
        _set_status(job_id, "failed", f"Unexpected error: {e}")
    finally:
        _discard_uploads()


def run_integrate(job_id: int, start_years: list[int]) -> None:
    """Fetch + rebuild the union of already-integrated ending years and the
    newly-selected NCES start years into a temp work dir, then hand off to
    build_check_swap — a full rebuild of that union, never an incremental
    merge. Intended to run in a background thread; never propagates (mirrors
    run_import's own catch-and-fail-the-job handling). The work dir is always
    removed afterward, success or failure.

    Before fetching anything, refuses (fails the job, touches neither the
    live db nor the network) if app.estimate says the union's estimated
    download+extract+staging footprint (padded by nces_disk_safety_factor)
    won't fit in the free space on the ipeds.db volume. Downloads then run
    CONCURRENTLY (a thread pool, width nces_download_concurrency), with
    structured per-year progress (import_jobs.progress) updated throughout.
    """
    s = get_settings()
    work = Path(s.nces_work_dir) / f"integrate_{job_id}"
    progress: dict | None = None
    try:
        _set_status(job_id, "running")

        existing_end_years: list[int] = []
        if Path(s.ipeds_db_path).exists():
            existing_end_years = _years(s.ipeds_db_path)
        already_integrated_starts = {y - 1 for y in existing_end_years}
        newly_selected_starts = set(start_years)
        union = sorted(already_integrated_starts | newly_selected_starts)
        _log(job_id, f"Integrating start years {union} "
                    f"(already integrated: {sorted(already_integrated_starts)}, "
                    f"newly selected: {sorted(newly_selected_starts)})")

        # --- (a) disk-headroom preflight refusal, BEFORE any fetch ---------
        try:
            catalog_by_year = {e["start_year"]: e for e in nces.probe_catalog()}
        except Exception as e:  # noqa: BLE001 — a probe failure must not block the estimate
            _log(job_id, f"NCES probe_catalog failed while estimating disk needs "
                        f"({type(e).__name__}: {e}) — estimating with unknown sizes.")
            catalog_by_year = {}
        # A year with an unknown zip size (probe failure above, or NCES
        # simply didn't send Content-Length) must NOT be estimated at the
        # small calibration default (nces_default_per_year_db_mb) — the
        # ENFORCED per-year caps (nces_zip_max_mb compressed,
        # nces_accdb_max_mb extracted) allow far more than that default, so a
        # run could pass this refusal and still fill the disk. Substitute the
        # enforced compressed-size cap for any unknown year instead, so the
        # refusal is a real backstop. (estimate_integrate's own None handling
        # — a default_per_year_db_mb slice — is left untouched; this
        # substitution happens only here, at the caller, before the values
        # ever reach the estimator. The post-download nces_total_max_mb
        # total-cap check below is a separate, complementary detector, bounded
        # by concurrency × max per-year size.)
        unknown_year_cap_bytes = s.nces_zip_max_mb * estimate.MB
        zip_bytes = []
        for sy in union:
            z = catalog_by_year.get(sy, {}).get("zip_bytes")
            zip_bytes.append(z if z is not None else unknown_year_cap_bytes)

        live_db_bytes = (Path(s.ipeds_db_path).stat().st_size
                         if Path(s.ipeds_db_path).exists() else 0)
        current_integrated_year_count = len(existing_end_years)
        already_integrated_count = len(already_integrated_starts)
        selected_count = len(union) - already_integrated_count

        du = shutil.disk_usage(Path(s.ipeds_db_path).parent)
        est = estimate.estimate_integrate(
            zip_bytes=zip_bytes,
            already_integrated_count=already_integrated_count,
            selected_count=selected_count,
            live_db_bytes=live_db_bytes,
            current_integrated_year_count=current_integrated_year_count,
            disk_free_bytes=du.free,
            disk_total_bytes=du.total,
            expand_factor=s.nces_accdb_expand_factor,
            default_per_year_db_mb=s.nces_default_per_year_db_mb,
            bandwidth_mbps=s.nces_est_bandwidth_mbps,
            build_seconds_per_year=s.nces_est_build_seconds_per_year,
            safety_factor=s.nces_disk_safety_factor,
        )
        if not est["sufficient"]:
            msg = (f"Not enough disk: need ~{_human_bytes(est['needed_with_safety_bytes'])}, "
                  f"have ~{_human_bytes(du.free)} free")
            _log(job_id, f"ERROR: {msg}")
            _set_status(job_id, "failed", msg)
            return

        # --- (b) init per-year progress -------------------------------------
        progress = {
            "overall": {"phase": "downloading",
                       "message": f"Fetching {len(union)} year(s) from NCES…"},
            "years": {
                str(sy): {
                    "start_year": sy,
                    "year_label": f"{sy}-{str(sy + 1)[-2:]}",
                    "step": "queued",
                    "downloaded_bytes": 0,
                    "total_bytes": None,
                    "pct": 0,
                }
                for sy in union
            },
        }
        _set_progress(job_id, progress)

        work.mkdir(parents=True, exist_ok=True)
        max_total_bytes = s.nces_total_max_mb * 1024 * 1024
        size_state = {"total": 0}
        size_lock = threading.Lock()
        prog = _ProgressThrottle(job_id, progress)
        releases: dict[int, str] = {}

        def _fetch_one(sy: int) -> tuple[int, str]:
            with prog.lock:
                progress["years"][str(sy)]["step"] = "downloading"
            prog.persist(sy, force=True)

            def on_progress(written, total):
                with prog.lock:
                    entry = progress["years"][str(sy)]
                    entry["downloaded_bytes"] = written
                    entry["total_bytes"] = total
                    entry["pct"] = int(written * 100 / total) if total else 0
                prog.persist(sy)  # throttled — see _ProgressThrottle

            try:
                accdb_path, release = nces.fetch_year(sy, work, on_progress=on_progress)
            except Exception as e:
                with prog.lock:
                    progress["years"][str(sy)]["step"] = "failed"
                prog.persist(sy, force=True)
                # A full-union rebuild re-fetches EVERY already-integrated year,
                # not just the newly-selected one(s) — if NCES has since removed
                # or relocated an already-integrated year, say exactly which
                # year and which kind it was, rather than a generic error that
                # reads like the newly-selected year(s) are the problem.
                which = ("an already-integrated" if sy in already_integrated_starts
                        else "a newly-selected")
                year_label = f"{sy}-{str(sy + 1)[-2:]}"
                raise NCESFetchError(
                    f"Could not fetch {which} year {year_label} from NCES "
                    f"(it may have been moved or withdrawn). Live database "
                    f"unchanged. ({type(e).__name__}: {e})") from e

            with prog.lock:
                progress["years"][str(sy)]["step"] = "fetched"
            prog.persist(sy, force=True)

            with size_lock:
                size_state["total"] += accdb_path.stat().st_size
                if size_state["total"] > max_total_bytes:
                    raise ValueError(
                        f"union download size exceeded the {s.nces_total_max_mb} MB cap")

            return sy, release

        # --- (c) concurrent downloads ----------------------------------------
        with ThreadPoolExecutor(max_workers=s.nces_download_concurrency) as ex:
            futures = {ex.submit(_fetch_one, sy): sy for sy in union}
            try:
                for fut in as_completed(futures):
                    sy, release = fut.result()
                    releases[sy] = release
                    _log(job_id, f"Fetched start year {sy} ({release})")
            except Exception:
                ex.shutdown(wait=False, cancel_futures=True)
                raise

        # --- (d) build/check/swap + provenance --------------------------------
        ok = build_check_swap(job_id, work)
        if ok:
            _record_provenance([(sy, sy + 1, releases.get(sy), "nces") for sy in union])
            progress["overall"] = {"phase": "done",
                                   "message": "Integration complete and now live."}
            _set_progress(job_id, progress)
    except NCESFetchError as e:
        # Deliberately-worded failure — pass the message through as-is (no
        # "Unexpected error:" prefix; see NCESFetchError's docstring).
        if progress is not None:
            progress["overall"] = {"phase": "failed", "message": str(e)}
            _set_progress(job_id, progress)
        _log(job_id, f"ERROR: {e}")
        _set_status(job_id, "failed", str(e))
    except Exception as e:  # noqa: BLE001 — mirror run_import: never propagate
        if progress is not None:
            progress["overall"] = {"phase": "failed", "message": f"Unexpected error: {e}"}
            _set_progress(job_id, progress)
        _log(job_id, f"ERROR: {type(e).__name__}: {e}")
        _set_status(job_id, "failed", f"Unexpected error: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_deintegrate(job_id: int, start_year: int) -> None:
    """Remove one already-integrated year from the live ipeds.db (the
    "trashcan"). This is an in-place DELETE + VACUUM on a COPY of live — NOT
    a rebuild (it never invokes scripts/build_ipeds_db.py or touches the
    network): copy live -> staging, DELETE that year's rows from EVERY base
    table that carries a `year` column (every family table plus the
    bookkeeping tables _family_map/_years/valuesets/vartable/tables),
    special-case _column_presence's CSV `years` field, VACUUM to reclaim the
    removed year's space, run deintegrate_checks (never integrity_checks —
    see its docstring for why), and only then activate staging via the same
    swap tail build_check_swap uses. A column that was present ONLY in the
    removed year is left behind as an all-NULL physical column on its family
    table (harmless to queries — nothing selects it into existence — and
    `_column_presence` is still correctly pruned so callers/metadata don't
    advertise it as present).

    Entirely offline — no NCES, no network. Intended to run in a background
    thread; NEVER propagates (mirrors run_import's catch-all -> status
    'failed'). Live is never mutated in place, and the staging file is always
    removed on the way out, success or failure.

    `start_year` is the NCES/catalog start year (the UI's key); the DB
    `_years.year` value the loader actually stores is `start_year + 1`.
    """
    s = get_settings()
    end_year = start_year + 1
    live = Path(s.ipeds_db_path)
    staging = s.ipeds_db_path.with_name("ipeds_staging.db")
    try:
        _set_status(job_id, "running")

        if not live.exists() or end_year not in _years(live):
            msg = f"Year {end_year} is not integrated."
            _log(job_id, f"ERROR: {msg}")
            _set_status(job_id, "failed", msg)
            return
        if len(_years(live)) <= 1:
            msg = ("Can't remove the only integrated year — the database "
                  "would be empty.")
            _log(job_id, f"ERROR: {msg}")
            _set_status(job_id, "failed", msg)
            return

        # Disk-headroom preflight BEFORE copying: need room for the live-db
        # copy plus VACUUM's own temp rebuild — roughly 2x the live db size,
        # padded by the same safety factor run_integrate's estimator uses.
        live_bytes = live.stat().st_size
        needed = live_bytes * 2 * s.nces_disk_safety_factor
        du = shutil.disk_usage(live.parent)
        if du.free < needed:
            msg = (f"Not enough disk space to remove this year: need ~"
                  f"{_human_bytes(needed)}, have ~{_human_bytes(du.free)} free.")
            _log(job_id, f"ERROR: {msg}")
            _set_status(job_id, "failed", msg)
            return

        year_label = f"{start_year}-{str(end_year)[2:]}"
        _update_overall_phase(job_id, "removing", f"Removing year {year_label}…")
        _log(job_id, f"Removing year {year_label} (end_year={end_year})…")
        shutil.copy2(str(live), str(staging))

        survey_year_token = f"{start_year}-{str(end_year)[2:]}"
        con = sqlite3.connect(staging)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for t in tables:
                # Table names come from our own sqlite_master (first-party,
                # never attacker-controlled) — quote-escape anyway as
                # defense-in-depth against an embedded double-quote.
                qt = '"' + t.replace('"', '""') + '"'
                cols = [r[1] for r in con.execute(f"PRAGMA table_info({qt})").fetchall()]
                if "year" in cols:
                    con.execute(f"DELETE FROM {qt} WHERE year=?", (end_year,))
            if "_column_presence" in tables:
                rows = con.execute(
                    "SELECT rowid, years FROM _column_presence").fetchall()
                for rowid, years_csv in rows:
                    tokens = [t for t in (years_csv or "").split(",") if t]
                    tokens = [t for t in tokens if t != survey_year_token]
                    if tokens:
                        con.execute("UPDATE _column_presence SET years=? WHERE rowid=?",
                                    (",".join(tokens), rowid))
                    else:
                        con.execute("DELETE FROM _column_presence WHERE rowid=?", (rowid,))
            con.commit()
            con.execute("VACUUM")
        finally:
            con.close()

        _set_status(job_id, "checks")
        _update_overall_phase(job_id, "checking", "Running de-integration checks…")
        _log(job_id, "Running de-integration checks…")
        passed, report = deintegrate_checks(staging, live, end_year)
        report_text = "\n".join(report)
        _log(job_id, report_text)
        if not passed:
            _set_status(job_id, "failed",
                        "De-integration checks FAILED — live DB untouched.\n\n" + report_text)
            _update_overall_phase(job_id, "failed",
                                  "De-integration checks failed — live DB untouched.")
            return

        _activate_staging(job_id, staging,
                          done_message=f"Year {year_label} removed and the database is now live.")

        # The swap above is irreversible — the removal has ALREADY succeeded
        # at this point. Tidying up year_provenance is best-effort bookkeeping:
        # a failure here must never flip the job to 'failed' (that would
        # falsely claim "live database was not changed"), just leave a stale
        # provenance row behind (a cosmetic orphan, not a correctness issue —
        # _integrated_starts() derives status from live _years, not
        # year_provenance).
        try:
            con2 = connect()
            try:
                con2.execute("DELETE FROM year_provenance WHERE start_year=?", (start_year,))
                con2.commit()
            finally:
                con2.close()
        except Exception as e:  # noqa: BLE001 — best-effort only, see above
            log.warning("run_deintegrate: could not delete year_provenance row "
                       "for start_year=%s after a successful swap (job %s): %s: %s",
                       start_year, job_id, type(e).__name__, e)
            _log(job_id, f"WARNING: removal succeeded, but could not clean up "
                        f"year_provenance for start_year={start_year}: {e}")

        _set_status(
            job_id, "swapped",
            f"Year {year_label} removed and the database is now live.\n\n" + report_text)
    except Exception as e:  # noqa: BLE001 — mirror run_import: never propagate
        _log(job_id, f"ERROR: {type(e).__name__}: {e}")
        _set_status(job_id, "failed", f"Unexpected error: {e}")
    finally:
        staging.unlink(missing_ok=True)
