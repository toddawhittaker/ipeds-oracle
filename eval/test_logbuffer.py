"""The admin log store: persists across restarts, is searchable/date-rangeable,
and never retains secrets.

The Logs view is readable by any admin, and in dev the mailer logs full email
bodies (with magic-link tokens), so this guards that (a) the mail logger is
dropped and (b) token/bearer values are redacted. It also exercises the SQLite
store's persistence, level filter, substring search (with literal wildcards),
date-range window, retention pruning, and newest-last ordering.
"""
import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.logbuffer import SqliteLogHandler

FAILURES = []


def check(name, cond):
    print(f"  {'✓' if cond else '✗'} {name}")
    if not cond:
        FAILURES.append(name)


def _rec(name, msg, level=logging.INFO, created=None):
    r = logging.LogRecord(name, level, __file__, 1, msg, None, None)
    if created is not None:
        r.created = created
    return r


def run():
    print("persistent log store:")
    db = str(Path(tempfile.mkdtemp()) / "logs.db")
    h = SqliteLogHandler(db, retention_days=30)
    h.emit(_rec("ipeds.mail", "[DEV] link https://x/api/auth/verify?token=SECRET123 body"))
    h.emit(_rec("ipeds.app", "consumed verify?token=ABCDEF12345 for user"))
    h.emit(_rec("ipeds.app", "header Authorization: Bearer sk-or-v1-abc123 kept-tail"))
    h.emit(_rec("ipeds.app", "import completed OK", level=logging.WARNING))
    recs = h.records()
    blob = str(recs)

    # --- secret scrubbing (security regression) ---------------------------
    check("mail logger is excluded entirely", not any(r["name"] == "ipeds.mail" for r in recs))
    check("no mail-token leaks", "SECRET123" not in blob)
    check("token= value redacted", "ABCDEF12345" not in blob)
    check("bearer/api-key value redacted", "abc123" not in blob and "sk-or-v1-abc123" not in blob)
    check("non-secret content retained", any("kept-tail" in r["msg"] for r in recs))

    # --- persistence across a restart -------------------------------------
    h2 = SqliteLogHandler(db, retention_days=30)  # reopen same file
    check("records persist across a restart", any("kept-tail" in r["msg"] for r in h2.records()))

    # --- level filter ------------------------------------------------------
    warns = h2.records(level="WARNING")
    check("level filter", len(warns) == 1 and warns[0]["level"] == "WARNING")

    # --- substring search --------------------------------------------------
    hits = h2.records(q="kept-tail")
    check("substring search matches", len(hits) == 1 and "kept-tail" in hits[0]["msg"])
    check("substring search excludes non-matches", h2.records(q="nonexistent-zzz") == [])
    check("substring search is case-insensitive", len(h2.records(q="KEPT-TAIL")) == 1)

    # --- LIKE wildcards are treated literally ------------------------------
    h2.emit(_rec("ipeds.app", "literal 100% done"))
    check("percent searched literally", any("100%" in r["msg"] for r in h2.records(q="100%")))
    check("underscore/percent don't act as wildcards", h2.records(q="zz%zz") == [])

    # --- date range --------------------------------------------------------
    now = time.time()
    h2.emit(_rec("ipeds.app", "OLD-EVENT", created=now - 10 * 86400))
    h2.emit(_rec("ipeds.app", "NEW-EVENT", created=now))
    windowed = h2.records(since=now - 3600, until=now + 3600)
    check("date-range includes in-window", any("NEW-EVENT" in r["msg"] for r in windowed))
    check("date-range excludes out-of-window", not any("OLD-EVENT" in r["msg"] for r in windowed))

    # --- retention prunes stale rows --------------------------------------
    rdb = str(Path(tempfile.mkdtemp()) / "logs.db")
    hr = SqliteLogHandler(rdb, retention_days=7)
    hr.emit(_rec("ipeds.app", "ANCIENT", created=time.time() - 30 * 86400))
    hr.emit(_rec("ipeds.app", "RECENT"))
    hr._prune()
    left = str(hr.records())
    check("retention prunes stale rows", "ANCIENT" not in left and "RECENT" in left)

    # --- ordering: newest last --------------------------------------------
    odb = str(Path(tempfile.mkdtemp()) / "logs.db")
    ho = SqliteLogHandler(odb, retention_days=30)
    ho.emit(_rec("ipeds.app", "first"))
    ho.emit(_rec("ipeds.app", "second"))
    check("records returned newest-last (insertion order)",
          [r["msg"] for r in ho.records()] == ["first", "second"])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL LOG STORE TESTS PASSED")


if __name__ == "__main__":
    run()
