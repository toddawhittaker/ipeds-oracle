#!/usr/bin/env python3
"""Restore app.db from a backup (guarded — STOP THE APP FIRST).

Validates the backup is a healthy SQLite database (integrity_check + expected
`users` table), snapshots the current app.db to `<app.db>.pre-restore-<ts>` so a
bad restore is itself reversible, then swaps the backup into place. Requires
--yes to actually write.

    python scripts/restore_app_db.py backups/app-20260714-030000.db --yes
    APP_DB_PATH=/path/to/srv-data/app.db python scripts/restore_app_db.py <backup> --yes
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import time
from pathlib import Path


def is_valid_backup(path: str | Path) -> bool:
    """True iff `path` is a SQLite db that passes integrity_check and has the
    core `users` table (so we never restore a corrupt or wrong-shaped file)."""
    path = Path(path)
    if not path.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            return False
        has_users = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()
        return bool(has_users)
    except sqlite3.Error:
        return False
    finally:
        con.close()


def restore(backup_path: str | Path, target_path: str | Path) -> Path | None:
    """Validate and swap `backup_path` into `target_path`. Snapshots an existing
    target to `<target>.pre-restore-<ts>` first; returns that snapshot path (or
    None if there was no existing target)."""
    backup_path = Path(backup_path)
    target_path = Path(target_path)
    if not is_valid_backup(backup_path):
        raise ValueError(f"not a valid app.db backup: {backup_path}")

    snapshot: Path | None = None
    if target_path.exists():
        snapshot = target_path.with_name(
            f"{target_path.name}.pre-restore-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(target_path, snapshot)
    # Clear stale WAL/SHM sidecars so the restored file is authoritative.
    for sidecar in (target_path.with_name(target_path.name + "-wal"),
                    target_path.with_name(target_path.name + "-shm")):
        sidecar.unlink(missing_ok=True)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_path, target_path)
    return snapshot


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Restore app.db from a backup.")
    ap.add_argument("backup", help="Path to the backup .db to restore.")
    ap.add_argument("--target", default=os.environ.get("APP_DB_PATH"),
                    help="app.db to overwrite (default: $APP_DB_PATH or the app's config).")
    ap.add_argument("--yes", action="store_true", help="Confirm the overwrite.")
    args = ap.parse_args(argv)

    target = args.target
    if not target:
        from app.config import get_settings
        target = str(get_settings().app_db_path)

    if not is_valid_backup(args.backup):
        print(f"REFUSING: {args.backup} is not a valid app.db backup.")
        return 2
    if not args.yes:
        print(f"Would restore {args.backup} -> {target} "
              f"(snapshotting the current file first). Re-run with --yes.")
        return 1

    snapshot = restore(args.backup, target)
    print(f"restored: {args.backup} -> {target}")
    if snapshot:
        print(f"previous app.db snapshotted to: {snapshot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
