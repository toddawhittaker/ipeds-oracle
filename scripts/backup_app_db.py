#!/usr/bin/env python3
"""Consistent online backup of app.db (the irreplaceable state).

Uses SQLite's online backup API — safe while the app is running and WAL-aware —
to write a timestamped copy under a backup dir, prunes to the most recent N, and
optionally pushes the new backup off-site. `ipeds.db` is intentionally NOT backed
up here: it is rebuildable from `data/` via scripts/build_ipeds_db.py.

    python scripts/backup_app_db.py                       # -> backups/app-<ts>.db
    python scripts/backup_app_db.py --keep 30 --out-dir /srv/backups
    APP_DB_PATH=/path/to/srv-data/app.db python scripts/backup_app_db.py

Off-site (optional): if the BACKUP_REMOTE env var is set (an rclone
"remote:path"), the new backup is uploaded with `rclone copy`. Configure the
remote once with `rclone config` — any S3-compatible object store works. See the
README (Self-hosting).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import time
from pathlib import Path


def make_backup(db_path: str | Path, out_dir: str | Path, keep: int = 14) -> Path:
    """Write a consistent backup of `db_path` into `out_dir` and prune to the
    newest `keep`. Returns the path of the new backup."""
    db_path = Path(db_path)
    out_dir = Path(out_dir)
    keep = max(1, keep)
    if not db_path.exists():
        raise FileNotFoundError(f"app.db not found: {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    dest = out_dir / f"app-{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)  # online snapshot — consistent even under WAL writes
        finally:
            dst.close()
    finally:
        src.close()

    _prune(out_dir, keep)
    return dest


def _prune(out_dir: Path, keep: int) -> None:
    backups = sorted(out_dir.glob("app-*.db"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[keep:]:
        old.unlink()


def _maybe_upload_remote(path: Path) -> bool:
    remote = os.environ.get("BACKUP_REMOTE")
    if not remote:
        return False
    subprocess.run(["rclone", "copy", str(path), remote], check=True)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Back up app.db (online, consistent).")
    ap.add_argument("--db", default=os.environ.get("APP_DB_PATH"),
                    help="Path to app.db (default: $APP_DB_PATH or the app's config).")
    ap.add_argument("--out-dir", default=os.environ.get("BACKUP_DIR", "backups"))
    ap.add_argument("--keep", type=int, default=int(os.environ.get("BACKUP_KEEP", "14")))
    args = ap.parse_args(argv)

    db = args.db
    if not db:
        from app.config import get_settings
        db = str(get_settings().app_db_path)

    dest = make_backup(db, args.out_dir, args.keep)
    print(f"backup: {dest}")
    if _maybe_upload_remote(dest):
        print(f"uploaded off-site: {os.environ['BACKUP_REMOTE']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
