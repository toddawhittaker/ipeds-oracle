"""Admin data import: validate a new IPEDS .accdb, rebuild into a STAGING
database, run integrity + magnitude checks, and only then atomically swap it in.
The live ipeds.db is never written in place, so a bad import can't corrupt it.

The heavy lifting reuses scripts/build_ipeds_db.py unchanged (it already accepts
--out and does a safe full rebuild).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from app.config import get_settings
from app.db import connect, set_meta
from app.skills import invalidate_cache
from app.tools.sql import run_sql

FILENAME_RE = re.compile(r"^IPEDS(\d{4})(\d{2})\.accdb$", re.IGNORECASE)
REQUIRED_FAMILIES = ("c_a", "hd", "valuesets", "vartable", "_years")


def _log(job_id: int, line: str) -> None:
    con = connect()
    try:
        con.execute("UPDATE import_jobs SET log = COALESCE(log,'') || ?, "
                    "updated_at=? WHERE id=?", (line + "\n", time.time(), job_id))
        con.commit()
    finally:
        con.close()


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


def run_import(job_id: int, upload_path: Path) -> None:
    """Full pipeline. Intended to run in a background thread."""
    s = get_settings()
    staging = s.ipeds_db_path.with_name("ipeds_staging.db")
    data_target = s.data_dir / upload_path.name
    backup_accdb = None
    try:
        _set_status(job_id, "running")
        _log(job_id, f"Preflight on {upload_path.name}…")
        ok, msg = preflight(upload_path)
        _log(job_id, msg)
        if not ok:
            _set_status(job_id, "failed", msg)
            return

        # Place the file where the loader discovers it (back up any existing one).
        s.data_dir.mkdir(parents=True, exist_ok=True)
        if data_target.exists() and data_target.resolve() != upload_path.resolve():
            backup_accdb = data_target.with_suffix(".accdb.bak")
            shutil.move(str(data_target), str(backup_accdb))
        if upload_path.resolve() != data_target.resolve():
            shutil.copy2(str(upload_path), str(data_target))
        _log(job_id, f"Staged source file at {data_target}")

        # Full rebuild into a staging DB (reuse the loader unchanged).
        _log(job_id, "Rebuilding staging database (this can take several minutes)…")
        build = Path(__file__).resolve().parents[1] / "scripts" / "build_ipeds_db.py"
        proc = subprocess.Popen(
            [sys.executable, str(build), "--data-dir", str(s.data_dir),
             "--out", str(staging)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:  # stream loader output (incl. collision warnings)
            _log(job_id, line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            _set_status(job_id, "failed", f"Loader exited with code {proc.returncode}.")
            return

        # Integrity + magnitude checks.
        _set_status(job_id, "checks")
        _log(job_id, "Running integrity checks…")
        passed, report = integrity_checks(staging, s.ipeds_db_path if s.ipeds_db_path.exists() else None)
        report_text = "\n".join(report)
        _log(job_id, report_text)
        if not passed:
            _set_status(job_id, "failed", "Integrity checks FAILED — live DB untouched.\n\n" + report_text)
            staging.unlink(missing_ok=True)
            return

        # Atomic swap: back up live, move staging into place.
        if s.ipeds_db_path.exists():
            shutil.move(str(s.ipeds_db_path), str(s.ipeds_db_path.with_suffix(".db.prev")))
        shutil.move(str(staging), str(s.ipeds_db_path))
        _log(job_id, "Swapped staging → live ipeds.db")

        # Bump data_version + clear the now-stale semantic cache.
        con = connect()
        try:
            dv = int((con.execute("SELECT value FROM meta WHERE key='data_version'").fetchone() or [1])[0])
            set_meta(con, "data_version", str(dv + 1))
            con.commit()
        finally:
            con.close()
        invalidate_cache()
        _log(job_id, "Bumped data_version and cleared semantic cache.")
        _set_status(job_id, "swapped", "Import succeeded and is now live.\n\n" + report_text)
    except Exception as e:  # noqa: BLE001
        _log(job_id, f"ERROR: {type(e).__name__}: {e}")
        _set_status(job_id, "failed", f"Unexpected error: {e}")
        if backup_accdb and backup_accdb.exists():
            shutil.move(str(backup_accdb), str(data_target))
