# The dataset (`ipeds.db`)

The app's agent queries `ipeds.db`; you'll also query it directly — to verify an
aggregation, derive an eval's expected answer, or debug the agent's SQL.

- **`docs/SCHEMA.md` is authoritative — read it before writing or verifying any query.**
  It's injected into every agent prompt. The DB is self-describing: use its
  *Discovery* queries (§3: `tables`, `vartable`, `valuesets`) to look up any
  table/variable/code rather than guessing.
- Inspect it with `sqlite3 -header -column ipeds.db "…"`, and **sanity-check
  magnitudes** against reality (~1M associate's/yr nationally) — a number 2–4× off
  usually means an aggregation-level mistake.

## Critical query gotchas (details in `docs/SCHEMA.md`)
- **One VALUE is capped at 1 MiB** (`SQL_MAX_VALUE_BYTES`, applied via
  `con.setlimit(SQLITE_LIMIT_LENGTH, …)` in `tools/sql.py`'s `_connect_ro`). The
  row cap bounds how MANY rows come back, never how BIG one is, and that gap was
  reachable in a single query: `SELECT length(hex(zeroblob(400000000)))`
  allocated **1,178 MB RSS in 0.98 s** (measured; capped it refuses in 0.000 s at
  34 MB). The `sql_timeout_seconds` watchdog **structurally cannot fire** inside a
  one-second allocation, so nothing stopped it — and 200 rows × 5 MB, or the
  100k-row CSV cap, is an OOM-kill of the container. The cap does not replace the
  watchdog, it **restores** it: with each value bounded, serious memory now needs
  thousands of values and therefore long enough for `con.interrupt()` to land.
  **That last claim is true at the 200-row model cap and FALSE at the 100k-row
  CSV cap** — measured at ~2.3 GB/s with values *under* the per-value cap, so
  the 25 s watchdog is irrelevant there. Three bounds are therefore needed, and
  each was added only after the previous one was defeated:
  **(1) one value** ≤ 1 MiB (this cap); **(2) the whole result** ≤
  `SQL_MAX_RESULT_BYTES` (64 MiB), accumulated **per ROW** — an earlier version
  sized a `fetchmany` from the running AVERAGE row size, which a
  small-rows-then-large result defeated for ~1 GB resident; **(3) one ROW**,
  which is `n_columns × 1 MiB` and reached 5,046 MB before anything refused it.
  The row bound needs the column count *before* a row exists, and
  `con.execute()` **steps once**, so reading `cur.description` is already too
  late (measured: tightening the limit there does nothing). A
  `SELECT * FROM (<sql>) LIMIT 0` probe gives the count with zero rows built.
  It **fails CLOSED** (to a 4 KiB floor) when a statement will not nest — failing
  open was itself a HIGH finding, because the probe adds exactly one nesting
  level and SQLite's parser overflows at depth 15, so SQL written at depth 14
  parses while making the probe fail: 2,975 MB measured, i.e. the same hole
  re-reached through nesting. And the derived limit must **only ever tighten** —
  `SQLITE_LIMIT_LENGTH` is not a ratchet, and without a `min()` against the
  per-value cap a 1-column query RAISED the documented 1 MiB cap 64×, returning
  a 66 MB value. Net: 5,046 MB → 35 MB, and an ordinary 100k-row export is
  unchanged at 0.18 s / 56 MB.
  Note `_value_bytes` is deliberately ROUGH (a flat 8 for non-strings), so a
  numeric-heavy result under-accounts ~5.6× and trips nearer 360 MB than 64 MB —
  bounded, but not bounded *at* 64 MiB.
  Surfaces as **`sqlite3.DataError`**, NOT `OperationalError` — it needs its own
  `except` branch (`SQLResultTooLargeError` → `"SQL TOO LARGE: …"` in
  `tools/registry.py`) or it falls through to the generic handler and the model
  gets no steer. A module constant on purpose, not a setting: raising it re-opens
  the hole. Pinned by the single-value-size-cap block in `test_sql_guards.py`.
- **"Recent N years" = a constant bound**, never a join:
  `WHERE year > (SELECT MAX(year)-3 FROM _years)`. A `JOIN (SELECT DISTINCT
  year …)` makes SQLite full-scan the 8M-row `c_a` and effectively hang.
- **Never mix CIP/award-level aggregation levels in a SUM.** In `c_a`, cipcode
  exists at 2-/4-/6-digit + a `'99'` grand-total row, each summing to the same
  total. Match an exact 6-digit code, or use `'99'`/`length(cipcode)=7` for
  totals — never `LIKE '51.%'`.
  **`awlevel` nests the same way, and SCHEMA.md used to say it didn't.** The
  mutually-exclusive real levels are `1,2,3,4,5,6,7,8,17,18,19` — **`20` and `21`
  are SUBDIVISIONS of `1`** (`20`+`21` = `1`, exactly), and `12`–`15` are rollups
  (`13`=`1`+`2`+`4`, `14`=`6`+`8`, `12`=`3`+`5`+`7`+`17`+`18`+`19`,
  `15`=`12`+`13`+`14`). SCHEMA.md claimed "1–8, 17–21 are mutually exclusive",
  the agent wrote exactly that list, and shipped a 12.8%-overcounted total that
  `grounding.py` graded `exact` and marked ✓ **verified** — grounding attests
  reproduction from the query result, never that the query was right, so a false
  invariant in the prompt is upstream of every guard. Prefer the rollup
  (`awlevel=15`/`12`) for an all-levels total over a hand-written list.
  `sqllint`'s `awlevel-cert-double-count` / `awlevel-rollup-mix` now catch both,
  and unlike the CIP heuristics they test an arithmetic identity, so they can be
  strict.
- Text code columns keep leading zeros (`cipcode='01.0000'`, `stabbr='CA'`);
  numeric codes are numeric (`awlevel=3`, `control=1`).
- Use the `institutions_current` view for clean current institution names.
- `year` = **ending** year of the collection (2024-25 → 2025).
- **A truncated result is an aggregation foot-gun, not just a display cap.**
  `run_sql` caps at `sql_row_cap_model` (200) and, when it cuts, now raises the
  same **`⚠ AGGREGATION CHECK (truncated)`** marker the rollup lints use
  (`tools/sql.py`) — so prompt step 3's "treat as blocking, fix and re-run"
  covers it: never sum/count/average a cut page as a TOTAL; aggregate in SQL or
  narrow the query. (Model-facing signal only — the server-side grounding/compute
  doesn't yet refuse a total over a truncated result; that's backlog #0.)

## Operational notes
- Wrap ad-hoc CLI queries in `timeout 30 …` so a bad plan can't hang a shell.
  **Never** poll with `until [ -s outfile ]` — a zero-row/hanging query never
  fills the file → infinite loop. If a query hangs, find the holder with
  `fuser ipeds.db` and `kill -9` it (a stuck `sqlite3` locks the DB).
- Tools (apt): `mdbtools` (reads `.accdb`), `sqlite3` CLI.
- Rebuild/extend: drop a new year's `.accdb` into `data/`, then
  `python3 scripts/build_ipeds_db.py`.

---
