"""Disk/time preflight estimator for the NCES "integrate" flow.

`estimate_integrate` is a PURE function — no I/O, no settings lookups, every
input passed explicitly by the caller — so it's testable byte-for-byte
against the shared fixture `eval/fixtures/estimate_cases.json` (also mirrored,
key-for-key in camelCase, by web/src/estimate.js — see web/e2e/estimate.spec.js
for the cross-language agreement test). See eval/test_estimate.py for the full
pinned contract; the arithmetic below must not drift from it.

`disk_and_calibration` is the (impure) helper that gathers the live facts
(disk usage, current live-db size/year-count, the calibration knobs from
Settings) that app/routers/admin.py embeds in GET /import/catalog and that
app/importer.py's run_integrate reads for its pre-fetch disk-headroom refusal.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

MB = 1024 * 1024


def _per_year_db_bytes(live_db_bytes: float, current_integrated_year_count: int,
                       default_per_year_db_mb: float) -> int:
    """live_db_bytes / current_integrated_year_count, floored — UNLESS either
    is zero/absent (fresh install, no live db yet, or nothing integrated),
    in which case fall back to default_per_year_db_mb*MB rather than
    dividing by zero."""
    if not live_db_bytes or not current_integrated_year_count:
        return int(default_per_year_db_mb * MB)
    return int(live_db_bytes / current_integrated_year_count)


def estimate_integrate(
    *,
    zip_bytes: list[float | None],
    already_integrated_count: int,
    selected_count: int,
    live_db_bytes: float,
    current_integrated_year_count: int,
    disk_free_bytes: float,
    disk_total_bytes: float,
    expand_factor: float,
    default_per_year_db_mb: float,
    bandwidth_mbps: float,
    build_seconds_per_year: float,
    safety_factor: float,
) -> dict:
    """Estimate the disk/time cost of one integrate run.

    zip_bytes: one entry per union year to be (re)downloaded; a None entry
    (unprobed/unknown size) contributes exactly one default_per_year_db_mb*MB
    slice instead of zero.
    """
    known_total = sum(z for z in zip_bytes if z is not None)
    none_count = sum(1 for z in zip_bytes if z is None)
    total_download_bytes = int(known_total + default_per_year_db_mb * MB * none_count)

    extracted_bytes = int(total_download_bytes * expand_factor)

    per_year_db_bytes = _per_year_db_bytes(
        live_db_bytes, current_integrated_year_count, default_per_year_db_mb)
    staging_db_bytes = int(per_year_db_bytes * (already_integrated_count + selected_count))

    additional_bytes_needed = int(total_download_bytes + extracted_bytes + staging_db_bytes)

    used_now_bytes = int(disk_total_bytes - disk_free_bytes)
    peak_used_bytes = int(used_now_bytes + additional_bytes_needed)

    bandwidth_bytes_per_sec = bandwidth_mbps * 1_000_000 / 8
    est_download_seconds = (
        total_download_bytes / bandwidth_bytes_per_sec if bandwidth_bytes_per_sec else 0.0)
    est_build_seconds = build_seconds_per_year * (already_integrated_count + selected_count)

    needed_with_safety_bytes = int(additional_bytes_needed * safety_factor)
    sufficient = disk_free_bytes >= needed_with_safety_bytes

    return {
        "total_download_bytes": total_download_bytes,
        "extracted_bytes": extracted_bytes,
        "staging_db_bytes": staging_db_bytes,
        "per_year_db_bytes": per_year_db_bytes,
        "additional_bytes_needed": additional_bytes_needed,
        "used_now_bytes": used_now_bytes,
        "peak_used_bytes": peak_used_bytes,
        "disk_free_bytes": disk_free_bytes,
        "disk_total_bytes": disk_total_bytes,
        "est_download_seconds": est_download_seconds,
        "est_build_seconds": est_build_seconds,
        "safety_factor": safety_factor,
        "needed_with_safety_bytes": needed_with_safety_bytes,
        "sufficient": sufficient,
    }


def disk_and_calibration(settings, integrated_year_count: int | None = None,
                         live_db_bytes: int | None = None) -> dict:
    """Gather the live facts + calibration knobs that both GET
    /import/catalog (app/routers/admin.py) and run_integrate's disk-headroom
    refusal (app/importer.py) need: current live-db size/year-count,
    shutil.disk_usage of the volume ipeds.db lives on, and the 8 nces_est_*/
    nces_disk_*/nces_*_concurrency Settings knobs.

    `integrated_year_count`/`live_db_bytes` are accepted as overrides so a
    caller that already computed them (e.g. admin.py's `_integrated_starts()`)
    doesn't need to re-derive them here; when omitted they're derived from
    `settings.ipeds_db_path` directly (0 if the live db doesn't exist yet).
    """
    ipeds_db_path = Path(settings.ipeds_db_path)

    if live_db_bytes is None:
        live_db_bytes = os.path.getsize(ipeds_db_path) if ipeds_db_path.exists() else 0

    if integrated_year_count is None:
        if ipeds_db_path.exists():
            from app import importer as _importer  # deferred: avoid a module cycle
            integrated_year_count = len(_importer._years(ipeds_db_path))
        else:
            integrated_year_count = 0

    du = shutil.disk_usage(ipeds_db_path.parent)
    per_year_db_bytes = _per_year_db_bytes(
        live_db_bytes, integrated_year_count, settings.nces_default_per_year_db_mb)

    return {
        "disk": {
            "free_bytes": du.free,
            "total_bytes": du.total,
            "used_bytes": du.used,
        },
        "calibration": {
            "expand_factor": settings.nces_accdb_expand_factor,
            "default_per_year_db_mb": settings.nces_default_per_year_db_mb,
            "bandwidth_mbps": settings.nces_est_bandwidth_mbps,
            "build_seconds_per_year": settings.nces_est_build_seconds_per_year,
            "safety_factor": settings.nces_disk_safety_factor,
            "per_year_db_bytes": per_year_db_bytes,
            "live_db_bytes": live_db_bytes,
            "already_integrated_count": integrated_year_count,
        },
    }
