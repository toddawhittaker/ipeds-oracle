# Unified IPEDS database

`ipeds.db` — a single SQLite database stacking **all IPEDS survey tables across
collection years 2020-21 … 2024-25**, built from the Access files in `data/`.

> **Web app:** there is now a private natural-language query app on top of this
> database (FastAPI + React, magic-link auth, self-learning agent). See
> [DEPLOY.md](DEPLOY.md) to run it; app code lives in `app/` and `web/`.

## Build / rebuild

```bash
python3 scripts/build_ipeds_db.py            # build ipeds.db from data/*.accdb
python3 scripts/build_ipeds_db.py --dry-run  # just print the table→family map
```
Add a new year by dropping its `IPEDS{YYYY}{YY}.accdb` into `data/` and rerunning.
Requires `mdbtools` (`sudo apt-get install mdbtools`).

## How it's organized

Each physical Access table (e.g. `C2024_A`, `HD2024`, `F2324_F1A`) is grouped
into a **family** by stripping the year from its name, and all years are stacked
into one table. Every row carries:

| column        | meaning                                   |
|---------------|-------------------------------------------|
| `survey_year` | collection year, e.g. `'2024-25'`         |
| `year`        | ending year, e.g. `2025` (use for sorting/filtering) |
| `src_table`   | original Access table name (provenance)   |

Key families: `c_a` (completions by CIP/award level — the main one), `c_b`,
`c_c`, `hd` (institution directory), `ef*` (fall enrollment), `effy` (12-month
enrollment), `gr*` (graduation rates), `sfa*` (student financial aid),
`f_f1a/f_f2/f_f3` (finance), `adm` (admissions), `om` (outcome measures), etc.

Column affinity comes from IPEDS's own dictionary: text codes like `CIPCODE`
keep leading zeros (`'01.0000'`); numeric fields are numeric.

### Metadata / label lookups (from the Access files themselves)
- `valuesets` — code → label (e.g. `AWLEVEL 3` → "Associate's degree"). Views: `meta_valuesets`.
- `vartable` — full data dictionary (variable titles, long descriptions). View: `meta_variables`.
- `tables` — table catalog (survey, coverage, description). View: `meta_tables`.

### Convenience objects
- `institutions_current` — latest known directory row per `unitid` (clean current names).
- `_years` — the loaded years (fast source for "recent N years").
- `_family_map` — every source table → family, year, row count.
- `_column_presence` — which columns exist in which years (schema drift).

## ⚠️ Query pattern for "recent N years"

Express it as a **constant bound on `year`**, NOT as a join to a year list — a
join flips SQLite's plan into full table scans and can hang on the 8M-row `c_a`.

```sql
-- Top 20 institutions granting Associate's degrees (AWLEVEL=3) in a CIP,
-- per year, over the last 3 years. Runs in <1s.
WITH grads AS (
  SELECT c.year, c.unitid, SUM(c.ctotalt) AS awards
  FROM c_a c
  WHERE c.cipcode = '51.3801'          -- Registered Nursing
    AND c.awlevel = 3                  -- Associate's degree
    AND c.majornum = 1                 -- first majors only
    AND c.year > (SELECT MAX(year) - 3 FROM _years)   -- ← constant bound
  GROUP BY c.year, c.unitid
),
ranked AS (
  SELECT year, unitid, awards,
         RANK() OVER (PARTITION BY year ORDER BY awards DESC) AS rk
  FROM grads
)
SELECT r.year, r.rk, ic.instnm, ic.stabbr, r.awards
FROM ranked r
JOIN institutions_current ic ON ic.unitid = r.unitid
WHERE r.rk <= 20
ORDER BY r.year DESC, r.rk;
```

Look up a CIP or any code's label:
```sql
SELECT DISTINCT codevalue, valuelabel FROM valuesets
WHERE varname='AWLEVEL' AND year=2025 ORDER BY CAST(codevalue AS INT);
```
