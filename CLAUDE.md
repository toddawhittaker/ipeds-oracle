# IPEDS project

Unified, cross-year **IPEDS** (Integrated Postsecondary Education Data System)
database for natural-language querying. IPEDS is the U.S. Dept. of Education's
census of colleges/universities.

## Starting a conversation
When a new conversation opens and the user hasn't already asked something
specific, greet them and ask what they'd like to query. Offer a few concrete
natural-language examples to prime them, e.g.:
- "Top 20 institutions awarding Associate's degrees in Registered Nursing
  (CIP 51.3801) over the last 3 years."
- "How many Computer Science (CIP 11.0701) bachelor's degrees did California
  public universities award last year?"
- "National total of Associate's degrees per year, all programs."
- "Which states awarded the most Master's degrees in Education?"

(If their first message is already a data question, just answer it — skip the
greeting.)

## Layout
- `ipeds.db` — **the** database: SQLite, ~1.9 GB, all IPEDS survey tables stacked
  across collection years **2020-21 … 2024-25**.
- `SCHEMA.md` — **read this before writing any query.** Data model, conventions,
  family catalog, code references, query patterns, and worked NL→SQL examples.
- `scripts/build_ipeds_db.py` — repeatable loader that builds `ipeds.db` from the
  Access files. `--dry-run` prints the table→family mapping.
- `data/` — source `IPEDS{YYYY}{YY}.accdb` files (one per collection year).
- `docs/` — official IPEDS Excel table documentation (human-readable backup).

## Answering a natural-language data question
1. **Load `SCHEMA.md`** for the model + the relevant family/columns. The DB is
   self-describing — use the *Discovery* queries in §3 (`tables`, `vartable`,
   `valuesets`) to look up any table, variable, or code you're unsure about
   rather than guessing column names.
2. Write SQL and run it: `sqlite3 -header -column ipeds.db "…"`.
3. **Sanity-check magnitudes** against reality before reporting (e.g. ~1M
   associate's/yr nationally). A number that's 2–4× off usually means an
   aggregation-level mistake (see the CIP/award-level rollup rule in SCHEMA §2).

## Critical gotchas (details in SCHEMA.md)
- **"Recent N years" = constant bound**, never a join:
  `WHERE year > (SELECT MAX(year)-3 FROM _years)`. A `JOIN (SELECT DISTINCT
  year …)` makes SQLite full-scan the 8M-row `c_a` and effectively hangs.
- **Never mix CIP/award-level aggregation levels in a SUM.** In `c_a`, cipcode
  exists at 2-/4-/6-digit + a `'99'` grand-total row, each summing to the same
  total. Match an exact 6-digit code, or use `'99'`/`length(cipcode)=7` for
  totals — never `LIKE '51.%'`.
- Text code columns keep leading zeros (`cipcode='01.0000'`, `stabbr='CA'`);
  numeric codes are numeric (`awlevel=3`, `control=1`).
- Use the `institutions_current` view for clean current institution names.
- `year` = **ending** year of the collection (2024-25 → 2025).

## Operational notes
- Query timeouts: wrap ad-hoc CLI queries in `timeout 30 …` so a bad plan can't
  hang a shell. **Never** poll with `until [ -s outfile ]` — a zero-row or
  hanging query never fills the file → infinite loop. If a query hangs, find the
  holder with `fuser ipeds.db` and `kill -9` it (a stuck `sqlite3` locks the DB).
- Tools installed via apt: `mdbtools` (reads `.accdb`), `sqlite3` CLI.
- Rebuild: `python3 scripts/build_ipeds_db.py` (drop a new year's `.accdb` into
  `data/` first to extend coverage).
