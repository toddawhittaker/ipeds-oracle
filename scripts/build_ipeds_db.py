#!/usr/bin/env python3
"""
Build a single unified SQLite database from the per-year IPEDS Access (.accdb)
files, so data can be queried across collection years.

Design (see memory/unified-ipeds-db-design.md):
  * Every physical table is grouped into a "family" by stripping the year token
    from its name (C2024_A -> c_a, HD2024 -> hd, F2324_F1A -> f_f1a, ...).
  * All years of a family are stacked into one table, tagged with:
        survey_year   TEXT     e.g. '2024-25'  (which collection file it came from)
        year          INTEGER  ending year, e.g. 2025
        src_table     TEXT     original physical table name (provenance)
  * Column set per family = superset across years (NULL-filled where absent).
  * Column affinity comes from IPEDS's own data dictionary (varTable.DataType):
        'A' -> TEXT (preserves leading zeros, e.g. CIPCODE '01.0000')
        'N' -> NUMERIC ; unitid -> INTEGER ; unknown -> TEXT
  * The metadata tables (tables/vartable/valuesets) load through the same
    machinery and become the label lookups; friendly views meta_* alias them.

Usage:
    python3 build_ipeds_db.py [--data-dir DIR] [--out ipeds.db] [--dry-run]
"""
import argparse
import csv
import os
import re
import sqlite3
import subprocess
import sys
from collections import OrderedDict, defaultdict

csv.field_size_limit(1 << 24)  # long LongDescription fields

# --- metadata table name prefixes (these become the label lookups) ---
META_PREFIXES = ("valuesets", "vartable", "tables")


def discover_files(data_dir):
    """Return [(path, start_year, survey_year, year_end)] sorted by year."""
    out = []
    for fn in os.listdir(data_dir):
        m = re.match(r"IPEDS(\d{4})(\d{2})\.accdb$", fn, re.I)
        if not m:
            continue
        start = int(m.group(1))          # 2020..2024
        year_end = start + 1             # 2021..2025
        survey_year = f"{start}-{str(year_end)[2:]}"
        out.append((os.path.join(data_dir, fn), start, survey_year, year_end))
    return sorted(out, key=lambda r: r[1])


def derive_family(name, start_year):
    """Strip the year token from a physical table name -> stable family key."""
    y4 = str(start_year)                      # '2024'
    y2 = y4[2:]                               # '24'
    span = f"{(start_year - 1) % 100:02d}{start_year % 100:02d}"  # '2324' (finance/SFA)
    s = name
    if span in s and span != y4:             # finance / financial-aid fiscal span
        s = s.replace(span, "", 1)
    elif y4 in s:                            # normal 4-digit year
        s = s.replace(y4, "", 1)
    else:                                    # trailing 2-digit year (GR200_20, Tables20...)
        s = re.sub(rf"_?{y2}$", "", s)
    s = re.sub(r"_+", "_", s.lower()).strip("_")
    return s


def list_tables(accdb):
    out = subprocess.run(["mdb-tables", "-1", accdb], capture_output=True, text=True, check=True)
    return [t for t in out.stdout.splitlines() if t.strip()]


def stream_table(accdb, table):
    """Yield (header_list, row_iter) for a table via mdb-export (streamed)."""
    p = subprocess.Popen(
        ["mdb-export", "-D", "%Y-%m-%d %H:%M:%S", accdb, table],
        stdout=subprocess.PIPE, text=True,
    )
    reader = csv.reader(p.stdout)
    try:
        header = next(reader)
    except StopIteration:
        p.wait()
        return [], iter(())
    header = [h.strip().lower() for h in header]

    def rows():
        try:
            yield from reader
        finally:
            p.stdout.close()
            p.wait()

    return header, rows()


def header_only(accdb, table):
    header, rows = stream_table(accdb, table)
    # drain a tiny bit to let the process close cleanly
    for _ in rows:
        break
    return header


ADDED = ("survey_year", "year", "src_table")


def build_type_map(files):
    """var_lower -> 'A'/'N' from every varTable; 'A' wins if a var is ever alpha."""
    tmap = {}
    for path, _start, *_ in files:
        vt = next((t for t in list_tables(path) if t.lower().startswith("vartable")), None)
        if not vt:
            continue
        header, rows = stream_table(path, vt)
        try:
            iv, it = header.index("varname"), header.index("datatype")
        except ValueError:
            continue
        for r in rows:
            if len(r) <= max(iv, it):
                continue
            var, dt = r[iv].strip().lower(), r[it].strip().upper()
            if not var:
                continue
            if var not in tmap or dt == "A":   # 'A' (text) wins
                tmap[var] = dt
    return tmap


def affinity(col, type_map):
    if col in ADDED:
        return "INTEGER" if col == "year" else "TEXT"
    if col == "unitid":
        return "INTEGER"
    dt = type_map.get(col)
    if dt == "N":
        return "NUMERIC"
    return "TEXT"   # 'A' or unknown -> preserve text/leading zeros


def plan(files):
    """Pass A: map every physical table to a family and compute the column union."""
    fam_cols = OrderedDict()          # family -> ordered union of columns
    fam_srcs = defaultdict(list)      # family -> [(path, table, start, survey_year, year_end)]
    col_years = defaultdict(set)      # (family,col) -> set(survey_year)
    for path, start, survey_year, year_end in files:
        seen_fams = {}
        for tbl in list_tables(path):
            fam = derive_family(tbl, start)
            if fam in seen_fams:
                sys.stderr.write(
                    f"!! collision in {os.path.basename(path)}: {tbl} and "
                    f"{seen_fams[fam]} both map to '{fam}'\n")
            seen_fams[fam] = tbl
            cols = header_only(path, tbl)
            fam_cols.setdefault(fam, OrderedDict())
            for c in cols:
                fam_cols[fam][c] = None
                col_years[(fam, c)].add(survey_year)
            fam_srcs[fam].append((path, tbl, start, survey_year, year_end))
    return fam_cols, fam_srcs, col_years


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "ipeds.db"))
    ap.add_argument("--dry-run", action="store_true", help="print family mapping and exit")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    files = discover_files(data_dir)
    if not files:
        sys.exit(f"No IPEDS*.accdb files found in {data_dir}")
    print(f"Found {len(files)} files: " + ", ".join(sy for _, _, sy, _ in files))

    fam_cols, fam_srcs, col_years = plan(files)
    print(f"\n{len(fam_cols)} families:")
    for fam in sorted(fam_cols):
        yrs = sorted({s[3] for s in fam_srcs[fam]})
        ncol = len(fam_cols[fam])
        drift = "" if len(yrs) == len(files) else f"  [only {','.join(yrs)}]"
        print(f"  {fam:<22} {ncol:>4} cols  x {len(yrs)} yrs{drift}")

    if args.dry_run:
        return

    type_map = build_type_map(files)
    print(f"\nType map: {len(type_map)} variables from varTable")

    out = os.path.abspath(args.out)
    if os.path.exists(out):
        os.remove(out)
    con = sqlite3.connect(out)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    cur = con.cursor()

    fam_map_rows = []
    for fam in fam_cols:
        cols = list(fam_cols[fam].keys()) + list(ADDED)
        coldefs = ", ".join(f'"{c}" {affinity(c, type_map)}' for c in cols)
        cur.execute(f'CREATE TABLE "{fam}" ({coldefs})')

        placeholders = ", ".join("?" for _ in cols)
        insert = f'INSERT INTO "{fam}" VALUES ({placeholders})'
        for path, tbl, _start, survey_year, year_end in fam_srcs[fam]:
            header, rows = stream_table(path, tbl)
            idx = {c: i for i, c in enumerate(header)}
            batch, n = [], 0
            for r in rows:
                rec = []
                for c in fam_cols[fam]:
                    j = idx.get(c)
                    v = r[j] if j is not None and j < len(r) else ""
                    rec.append(None if v == "" else v)
                rec += [survey_year, year_end, tbl]
                batch.append(rec)
                if len(batch) >= 5000:
                    cur.executemany(insert, batch); n += len(batch); batch = []
            if batch:
                cur.executemany(insert, batch); n += len(batch)
            fam_map_rows.append((tbl, fam, survey_year, year_end, n))
            print(f"  loaded {tbl:<28} -> {fam:<22} {n:>8} rows")
        con.commit()

    # provenance + column-presence bookkeeping
    cur.execute("CREATE TABLE _family_map (src_table TEXT, family TEXT, survey_year TEXT,"
                " year INTEGER, n_rows INTEGER)")
    cur.executemany("INSERT INTO _family_map VALUES (?,?,?,?,?)", fam_map_rows)
    cur.execute("CREATE TABLE _column_presence (family TEXT, column_name TEXT, years TEXT)")
    cur.executemany(
        "INSERT INTO _column_presence VALUES (?,?,?)",
        [(f, c, ",".join(sorted(col_years[(f, c)]))) for (f, c) in col_years],
    )
    # tiny helper so "recent N years" is a fast constant bound, never a join
    cur.execute("CREATE TABLE _years (survey_year TEXT, year INTEGER PRIMARY KEY)")
    cur.executemany("INSERT INTO _years VALUES (?,?)",
                    sorted({(sy, ye) for _, _, sy, ye, _ in fam_map_rows}, key=lambda r: r[1]))
    con.commit()

    # indexes: (year, unitid) on every family that has unitid; valuesets lookup
    for fam in fam_cols:
        if "unitid" in fam_cols[fam]:
            cur.execute(f'CREATE INDEX "ix_{fam}_unitid" ON "{fam}" (unitid, year)')
    if "c_a" in fam_cols:
        cur.execute('CREATE INDEX ix_c_a_cip ON c_a (cipcode, awlevel, year)')
    if "valuesets" in fam_cols:
        cur.execute('CREATE INDEX ix_valuesets_lk ON valuesets (tablename, varname, codevalue)')

    # friendly views
    for meta, phys in (("meta_tables", "tables"), ("meta_variables", "vartable"),
                       ("meta_valuesets", "valuesets")):
        if phys in fam_cols:
            cur.execute(f"CREATE VIEW {meta} AS SELECT * FROM \"{phys}\"")
    if "hd" in fam_cols:
        cur.execute("""
            CREATE VIEW institutions_current AS
            SELECT h.* FROM hd h
            JOIN (SELECT unitid, MAX(year) AS y FROM hd GROUP BY unitid) m
              ON h.unitid = m.unitid AND h.year = m.y
        """)
    con.commit()
    cur.execute("PRAGMA optimize")
    con.close()
    sz = os.path.getsize(out) / 1e9
    print(f"\nDone -> {out}  ({sz:.2f} GB)")


if __name__ == "__main__":
    main()
