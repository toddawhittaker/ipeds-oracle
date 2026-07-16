"""Backup/restore contract for app.db.

Exercises the drill end-to-end without touching the real db: build a throwaway
app.db, back it up (online snapshot), prune to N, and restore it into a fresh
target — asserting data survives and that a corrupt/wrong file is refused.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.backup_app_db import _prune, make_backup
from scripts.restore_app_db import is_valid_backup, restore

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def _make_app_db(path: Path, marker: str):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    con.execute("INSERT INTO users(email) VALUES (?)", (marker,))
    con.commit()
    con.close()


def test_backup_captures_data():
    d = Path(tempfile.mkdtemp())
    _make_app_db(d / "app.db", "todd@example.edu")
    dest = make_backup(d / "app.db", d / "backups")
    assert dest.exists(), "no backup file written"
    con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    email = con.execute("SELECT email FROM users").fetchone()[0]
    con.close()
    assert email == "todd@example.edu", email


def test_prune_keeps_newest_n():
    d = Path(tempfile.mkdtemp())
    # Hand-make 5 backups with increasing mtimes.
    for i in range(5):
        f = d / f"app-2026010{i}-000000.db"
        f.write_bytes(b"x")
        os.utime(f, (1_000_000 + i, 1_000_000 + i))
    _prune(d, keep=2)
    left = sorted(p.name for p in d.glob("app-*.db"))
    assert left == ["app-20260103-000000.db", "app-20260104-000000.db"], left


def test_restore_round_trip_and_snapshot():
    d = Path(tempfile.mkdtemp())
    _make_app_db(d / "app.db", "backup-marker@x.edu")
    backup = make_backup(d / "app.db", d / "backups")

    target = d / "restored" / "app.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Pre-existing target so a snapshot is taken.
    _make_app_db(target, "old-state@x.edu")
    snapshot = restore(backup, target)

    con = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    email = con.execute("SELECT email FROM users").fetchone()[0]
    con.close()
    assert email == "backup-marker@x.edu", email
    assert snapshot and snapshot.exists(), "pre-restore snapshot missing"


def test_invalid_backup_refused():
    d = Path(tempfile.mkdtemp())
    junk = d / "app-junk.db"
    junk.write_bytes(b"not a sqlite database at all")
    assert not is_valid_backup(junk), "corrupt file accepted as a backup"
    try:
        restore(junk, d / "app.db")
        raise AssertionError("restore did not refuse an invalid backup")
    except ValueError:
        pass


def test_valid_backup_without_users_refused():
    d = Path(tempfile.mkdtemp())
    wrong = d / "app-wrong.db"
    con = sqlite3.connect(wrong)
    con.execute("CREATE TABLE other (x)")
    con.commit()
    con.close()
    assert not is_valid_backup(wrong), "a db with no users table was accepted"


def run():
    print("app.db backup/restore contract:")
    check("backup captures the data", test_backup_captures_data)
    check("prune keeps the newest N", test_prune_keeps_newest_n)
    check("restore round-trips + snapshots the old file", test_restore_round_trip_and_snapshot)
    check("corrupt file is refused", test_invalid_backup_refused)
    check("valid sqlite without users table is refused", test_valid_backup_without_users_refused)
    print()
    if FAILURES:
        print(f"{len(FAILURES)} contract(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL BACKUP/RESTORE TESTS PASSED")


if __name__ == "__main__":
    run()
