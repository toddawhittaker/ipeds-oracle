"""The admin log store: persists across restarts, is searchable/date-rangeable,
and never retains secrets.

The Logs view is readable by any admin, and in dev the mailer logs full email
bodies (with magic-link tokens), so this guards that (a) the mail logger is
dropped and (b) token/bearer values are redacted. It also exercises the SQLite
store's persistence, level filter, substring search (with literal wildcards),
date-range window, retention pruning, and newest-last ordering.
"""
import logging
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.logbuffer as logbuffer
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

    # --- row cap: enforced via a REAL prune trigger (not a hand-called _prune)
    # _PRUNE_EVERY emit() calls forces the periodic sweep; emit exactly that many
    # so the cap-enforcing prune fires organically, once, mid-loop.
    print("row cap (max_rows):")
    n = logbuffer._PRUNE_EVERY
    cap = 100
    cdb = str(Path(tempfile.mkdtemp()) / "logs.db")
    hc = SqliteLogHandler(cdb, retention_days=30, max_rows=cap)
    for i in range(n):
        hc.emit(_rec("ipeds.app", f"MSG-{i:04d}"))
    crecs = hc.records(limit=2000)
    check(f"cap settles at max_rows after the automatic {n}th-emit prune",
          len(crecs) == cap)
    cblob = str(crecs)
    # Surviving ids are the newest `cap` of the n emitted (indices n-cap .. n-1).
    check("newest row survives the cap prune", f"MSG-{n - 1:04d}" in cblob)
    check("oldest surviving row is exactly the cap boundary",
          f"MSG-{n - cap:04d}" in cblob)
    check("row just past the cap boundary was dropped",
          f"MSG-{n - cap - 1:04d}" not in cblob)
    check("first-ever row was dropped by the cap",
          "MSG-0000" not in cblob)

    # --- max_rows=0 disables the cap entirely -------------------------------
    zdb = str(Path(tempfile.mkdtemp()) / "logs.db")
    hz = SqliteLogHandler(zdb, retention_days=30, max_rows=0)
    total_z = n + 100  # past _PRUNE_EVERY, so a real sweep still fires
    for i in range(total_z):
        hz.emit(_rec("ipeds.app", f"Z-{i:05d}"))
    zrecs = hz.records(limit=100000)
    check("max_rows=0 disables the cap (nothing dropped by row count)",
          len(zrecs) == total_z)

    # --- fewer rows than the cap is a no-op (the `id < NULL` path) ---------
    # An off-by-one or a NULL mishandling here would silently empty the store.
    ndb = str(Path(tempfile.mkdtemp()) / "logs.db")
    hn = SqliteLogHandler(ndb, retention_days=30, max_rows=1000)
    for i in range(5):
        hn.emit(_rec("ipeds.app", f"KEEP-{i}"))
    hn._prune()
    nrecs = hn.records()
    check("fewer rows than max_rows is a no-op, not a wipe", len(nrecs) == 5)
    check("all under-cap rows retained",
          {r["msg"] for r in nrecs} == {f"KEEP-{i}" for i in range(5)})

    # --- age pruning composes with the cap in the same _prune() call -------
    adb = str(Path(tempfile.mkdtemp()) / "logs.db")
    ha = SqliteLogHandler(adb, retention_days=7, max_rows=3)
    old_ts = time.time() - 30 * 86400
    for i in range(5):
        ha.emit(_rec("ipeds.app", f"ANCIENT-{i}", created=old_ts))
    for i in range(5):
        ha.emit(_rec("ipeds.app", f"RECENT-{i}"))
    ha._prune()
    ablob = str(ha.records())
    check("age prune removed all stale rows even under a row cap",
          all(f"ANCIENT-{i}" not in ablob for i in range(5)))
    check("cap then kept only the newest survivors of the age prune",
          len(ha.records()) == 3)
    check("cap kept the 3 newest recent rows",
          all(f"RECENT-{i}" in ablob for i in (2, 3, 4)))
    check("cap dropped the oldest recent rows once age pruning left too many",
          all(f"RECENT-{i}" not in ablob for i in (0, 1)))

    # --- auto_vacuum migrates a PRE-EXISTING plain (auto_vacuum=0) logs.db --
    print("auto_vacuum migration:")
    mdb = str(Path(tempfile.mkdtemp()) / "logs.db")
    raw = sqlite3.connect(mdb)
    raw.executescript(logbuffer._SCHEMA)
    raw.execute("INSERT INTO logs(ts, level, name, msg) VALUES (?,?,?,?)",
                (time.time(), "INFO", "ipeds.app", "PRE-EXISTING-ROW"))
    raw.commit()
    pre_av = raw.execute("PRAGMA auto_vacuum").fetchone()[0]
    raw.close()
    check("fixture starts as plain auto_vacuum (the real-world logs.db today)",
          pre_av == 0)

    hm = SqliteLogHandler(mdb, retention_days=30, max_rows=0)
    post_av = hm._con.execute("PRAGMA auto_vacuum").fetchone()[0]
    check("opening a handler migrates it to INCREMENTAL auto_vacuum (2)",
          post_av == 2)
    check("migration (a full VACUUM) preserved pre-existing rows",
          any("PRE-EXISTING-ROW" in r["msg"] for r in hm.records()))

    # --- reclaim actually returns freed pages: PRAGMA freelist_count, not ---
    # --- "file got smaller", is the signal (see _reclaim's .fetchall() note) -
    # PRAGMA incremental_vacuum frees ONE page per sqlite3_step; execute()
    # alone steps once. Without draining the cursor with .fetchall(), a big
    # prune would only reclaim a single page while the rest sit in the
    # freelist -- the file can still look "smaller than before" (a few KB) even
    # though the freelist is still full of unreturned pages. freelist_count
    # after a prune that deleted plenty of data is the check that actually
    # tells the two implementations apart.
    print("space reclamation (freelist_count, not just file size):")
    rdb2 = str(Path(tempfile.mkdtemp()) / "logs.db")
    hrec = SqliteLogHandler(rdb2, retention_days=30, max_rows=50)
    payload = "Q" * 300  # bulk the rows so a prune frees more than one page
    for i in range(n):
        hrec.emit(_rec("ipeds.app", f"R-{i:04d} {payload}"))
    freelist = hrec._con.execute("PRAGMA freelist_count").fetchone()[0]
    page_count = hrec._con.execute("PRAGMA page_count").fetchone()[0]
    check(f"reclaim drains the freed pages (freelist_count == 0, got "
          f"{freelist} of {page_count} pages)", freelist == 0)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL LOG STORE TESTS PASSED")


if __name__ == "__main__":
    run()
