#!/usr/bin/env python3
"""Build a tiny stand-in `ipeds.db` for CI.

The real database is ~1.9 GB and gitignored, so it cannot live in CI. The
deterministic guard/backend/security suites don't check specific magnitudes —
they only need the tables and columns they query to *exist and execute*:

  * `c_a(year, ctotalt, awlevel, majornum, cipcode)` — the SQL-guard tests run
    real aggregations and a 3-way cross join that must be big enough to trip the
    2-second timeout watchdog, so `c_a` gets a few thousand rows.
  * `hd(unitid, instnm)` — the validator false-positive probes run `LIKE`/
    `REPLACE` queries against institution names.

This is NOT a substitute for the real data. `eval/eval_nl2sql.py` asserts
known-good national totals (CA public CS bachelor's = 7,679, …) and must be run
locally/against the real `ipeds.db`; it is intentionally not part of hosted CI.

Usage:
    python scripts/make_ci_fixture_db.py <output_path>
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Enough rows that COUNT(*) over c_a×c_a×c_a (~N^3) can't finish inside the
# guard suite's 2s cap, so the watchdog interrupt is genuinely exercised.
C_A_ROWS = 2000


def build(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    con = sqlite3.connect(out_path)
    cur = con.cursor()

    # --- c_a: Completions, the workhorse table the guards query -------------
    cur.execute(
        "CREATE TABLE c_a ("
        " year INTEGER, ctotalt INTEGER, awlevel INTEGER,"
        " majornum INTEGER, cipcode TEXT)"
    )
    years = [2021, 2022, 2023, 2024, 2025]
    rows = []
    for i in range(C_A_ROWS):
        year = years[i % len(years)]
        # Every year gets a `cipcode='99'`, awlevel=3, majornum=1 grand-total
        # row so the CSV-export query in test_backend returns data.
        if i < len(years):
            rows.append((years[i], 1_000_000 + years[i], 3, 1, "99"))
        else:
            rows.append((year, (i * 7) % 5000, (i % 5) + 1, (i % 2) + 1,
                         f"{(i % 54) + 1:02d}.{(i % 9999):04d}"))
    cur.executemany(
        "INSERT INTO c_a(year,ctotalt,awlevel,majornum,cipcode)"
        " VALUES (?,?,?,?,?)", rows
    )

    # --- _years: ending years present -- the fresh-deploy "no data" guard
    # (app.tools.sql.ipeds_years / has_ipeds_data) checks this table exists
    # and is non-empty, so the fixture must carry one matching c_a's years or
    # every chat/guard suite would see this fixture as a data-less deploy.
    cur.execute("CREATE TABLE _years (year INTEGER)")
    cur.executemany("INSERT INTO _years(year) VALUES (?)", [(y,) for y in years])

    # --- hd: institution directory, used by the LIKE/REPLACE probes ---------
    cur.execute("CREATE TABLE hd (unitid INTEGER, instnm TEXT)")
    institutions = [
        "Ohio State University",
        "Columbus State Community College",
        "University of Update Falls",     # matches LIKE '%update%' probe
        "Delete County College",          # matches LIKE '%Delete%' probe
        "Create College of the Commit",   # matches '%Create%College%' / '%Commit%'
    ]
    cur.executemany(
        "INSERT INTO hd(unitid,instnm) VALUES (?,?)",
        [(100000 + i, name) for i, name in enumerate(institutions)],
    )

    con.commit()
    con.close()
    print(f"wrote CI fixture: {out_path} "
          f"(c_a={C_A_ROWS} rows, hd={len(institutions)} rows)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: make_ci_fixture_db.py <output_path>")
    build(Path(sys.argv[1]).resolve())
