# IPEDS unified database — schema & query guide

`ipeds.db` (SQLite, ~1.9 GB) stacks **every IPEDS survey table across collection
years 2020-21 … 2024-25** into one file. This document is the context needed to
translate natural-language questions into correct SQL. The database is
**self-describing** — the `tables`, `vartable`, and `valuesets` tables let you
look up any table, variable, or code on demand (see *Discovery* below), so this
file documents the model + conventions + the highest-value tables, not every
column.

Query with: `sqlite3 -header -column ipeds.db "…"` (or Python's `sqlite3`).

---

## 1. Data model

Each Access table (e.g. `C2024_A`, `HD2024`, `F2324_F1A`) is grouped into a
**family** by stripping the year from its name, and all years are stacked into
one table. Query the **family** name (lowercase), never the year-specific name.

Every row carries three added columns:

| column        | type    | meaning                                                            |
|---------------|---------|-------------------------------------------------------------------|
| `survey_year` | TEXT    | collection year, e.g. `'2024-25'`                                 |
| `year`        | INTEGER | **ending year, e.g. `2025`** — use this for filtering/sorting/grouping |
| `src_table`   | TEXT    | original Access table name (provenance)                           |

- **`year` is the ending year of the collection**: 2020-21→2021 … 2024-25→2025.
- "Last N years" ⇒ `year > (SELECT MAX(year) - N FROM _years)`. **Always a
  constant bound, never a JOIN to a year list** (see Gotchas).
- Finance (`f_*`) and Financial Aid (`sfa*`) data lag ~1 year; `year` still
  reflects the collection file, and `meta_tables.yearcoverage` gives the true
  fiscal period.

Column affinity comes from IPEDS's own dictionary: text codes (`cipcode`,
`stabbr`, …) keep **leading zeros** (`'01.0000'`); numeric fields are numeric.
Missing values are `NULL`; some code fields use sentinels like `-3` = "not
available", `-2` = "not applicable", `-1` = "not reported".

---

## 2. Conventions

### Institution key
`unitid` (INTEGER) is the stable institution ID across all years and all tables.
Join anything to the directory on `unitid`.

### Race/ethnicity × gender columns (Completions, Enrollment, Staff)
Count columns follow `<prefix><group><sex>`. In Completions the prefix is `C`:
- **Sex suffix:** `T` = total, `M` = men, `W` = women.
- **Grand total:** `CTOTALT` / `CTOTALM` / `CTOTALW` (`CTOTALT` already sums all races).
- **9 race/ethnicity groups** (append `T`/`M`/`W`):
  `CAIAN` American Indian/Alaska Native · `CASIA` Asian · `CBKAA` Black ·
  `CHISP` Hispanic · `CNHPI` Native Hawaiian/Pacific Islander · `CWHIT` White ·
  `C2MOR` Two or more races · `CUNKN` Unknown · `CNRAL` U.S. Nonresident.

Fall-enrollment tables (`ef*`) use analogous columns with an `E` prefix plus
attendance-status/level dimensions baked into the column names — look them up in
`vartable` rather than guessing.

### CIP codes (programs) — ⚠️ nested aggregation levels
`cipcode` is TEXT, **2020 CIP taxonomy** for all 5 years, with leading zeros,
e.g. `'51.3801'` (Registered Nursing), `'11.0701'` (Computer Science; note
`'11.0101'` is the broader "Computer & Information Sciences, General"). When a
question names a field, confirm the code via the `valuesets`/`CIPCODE` lookup —
similar-sounding programs have distinct codes.

**Completions (`c_a`) stores every program at THREE nested levels plus a grand
total, and each level independently sums to the same total.** For one
institution/award level:

| level        | example    | filter                              |
|--------------|------------|-------------------------------------|
| grand total  | `'99'`     | `cipcode='99'`                      |
| 2-digit series | `'51'`   | `length(cipcode)=2 AND cipcode<>'99'` |
| 4-digit sub-series | `'51.38'` | `length(cipcode)=5`             |
| 6-digit detail | `'51.3801'` | `length(cipcode)=7`               |

**Never mix levels in a SUM** (summing all rows overcounts ~4×). Pick one level:
- **Specific program** → exact 6-digit match: `cipcode='51.3801'` (safe, no rollup).
- **A whole 2-digit field** (e.g. all Health = 51) → the rollup row `cipcode='51'`,
  or 6-digit-only `cipcode LIKE '51.____'`. **Not** `LIKE '51.%'` (that double-counts
  the 4- and 6-digit levels).
- **All programs / national total** → `cipcode='99'` (grand-total row) or
  restrict to 6-digit detail `length(cipcode)=7`.

`majornum`: `1` = first major (the usual filter for "graduates in a program"),
`2` = second major. Summing both counts double-majors twice.

The same "pick one level" rule applies to **`awlevel`**: codes 1–8 & 17–21 are
real, mutually-exclusive levels; 12–15 are rollup totals — never sum a real level
together with a rollup.

### ⚠️ Nested totals / rollups beyond completions (same trap, other surveys)
The completions "pick ONE level, never sum a rollup together with its parts" rule
recurs across surveys: many tables carry a categorical key whose codes mix a grand
total with its own components, so a blanket `SUM` of the measure over all rows
multi-counts. Look the key up in `valuesets` and filter to the single total (or
single level) you mean. When in doubt, the `drv*` derived family already holds the
clean total/rate. Verified national figures (year 2024) to sanity-check against:

- **Fall enrollment `ef` / `efa` — key `eflevel`.** Codes nest: `10` All students
  (19.7M = true total) = `20` Undergraduate (16.4M) + `50` Graduate (3.3M); `20` =
  `30` degree/cert-seeking (14.1M) + `40` non-degree (2.4M); and so on. `SUM(eftotal)`
  over ALL `eflevel` rows = 81.0M ≈ **4.1× overcount**. Total enrollment ⇒ filter
  `eflevel=10`.
- **12-month enrollment `effy` — key `effylev`.** `1` All students (25.3M) = `2`
  Undergraduate (21.2M) + `4` Graduate (4.1M). The family ALSO carries detail rows
  at `effylev=-2` (broken out by `lstudy`); a blanket `SUM(efytotlt)` ≈ 155M (~6×).
  12-month headcount ⇒ filter `effylev=1`.
- **Graduation rates `gr` — key `grtype` is NOT additive.** `grtype` holds
  overlapping cohort *definitions* (revised cohort, adjusted = revised − exclusions,
  completers-within-150%, transfer-outs) and sub-cohorts, keyed further by
  `chrtstat` / `cohort` / `section`. Never `SUM(grtotlt)` across `grtype`. Prefer
  derived `drvgr` for rates; computing by hand, use the adjusted-cohort code as the
  denominator and completers-within-150% as the numerator.
- **Finance `f_f1a` / `f_f2` / `f_f3` — three DISJOINT forms, DIFFERENT columns.**
  Each institution files exactly one form by control (public-GASB / FASB /
  for-profit; pairwise `unitid` overlap = 0), and there is **no shared total
  column**: total revenues = `f1d01` (f_f1a) vs `f2d16` (f_f2) vs `f3d09` (f_f3). A
  sector-wide or national finance total must **UNION all three**, each with its own
  variable — querying only `f_f1a` returns $500B and silently drops ~39% of the
  ~$813B national total. Prefer derived `drvf` for common per-institution indicators.
- **Staff `s_oc` / `eap` — occupation key nests, plus `ftpt` / `facstat`.**
  `s_oc.occupcat=100` and `eap.eapcat=10000` (both "All staff") sit above nested
  occupation subtotals (200 ⊃ 210 ⊃ 211–215…); `ftpt` also nests (`1` All staff =
  `2` Full-time + `3` Part-time; `4` Graduate assistants is separate). Total staff
  headcount ⇒ `occupcat=100 AND ftpt=1` (= 4.0M, year 2024); never `SUM(hrtotlt)`
  across `occupcat` or `ftpt`. Prefer derived `drvhr`.

---

## 3. Discovery (look anything up from within the DB)

```sql
-- Columns of a family (actual unified columns):
SELECT name FROM pragma_table_info('c_a');

-- Human-readable titles/descriptions for a table's variables (use latest year):
SELECT varname, vartitle, longdescription FROM vartable
WHERE tablename='C2024_A' ORDER BY varorder;

-- Code → label for a categorical variable:
SELECT DISTINCT codevalue, valuelabel FROM valuesets
WHERE varname='AWLEVEL' AND year=2025 ORDER BY CAST(codevalue AS INT);

-- Find a variable by keyword (e.g. tuition):
SELECT DISTINCT tablename, varname, vartitle FROM vartable
WHERE vartitle LIKE '%tuition%' AND year=2025;

-- Find a table by topic:
SELECT DISTINCT tablename, tabletitle FROM tables
WHERE tabletitle LIKE '%enrollment%' AND year=2025;
```
Note: `vartable`/`valuesets`/`tables` key on the **year-specific** name
(`C2024_A`, `HD2024`). Map family → latest physical name via `_family_map`
(`SELECT src_table, year FROM _family_map WHERE family='c_a'`) — the 2024/`year=2025`
row is the current definition.

---

## 4. Family catalog

Grouped by IPEDS survey. `nyr` = years present (most are all 5: 2021-2025).
Families prefixed `drv*` are IPEDS-precomputed **derived** variables (rates,
totals) — often the quickest source for common indicators.

**Completions** (awards/degrees) — *July→June award year*
- `c_a` — awards by **6-digit CIP × award level × race/ethnicity × gender** (the main table)
- `c_b` — students receiving awards, by race/ethnicity × gender (one row/institution)
- `c_c` — awards by award level × gender × race × age
- `cdep` — # programs offered (and via distance ed) by award level
- `drvc` — derived completions indicators

**Institutional Characteristics / Directory**
- `hd` — **directory** (name, location, control, sector, level, size…) — the dimension table
- `ic` — educational offerings, organization, admissions, services
- `ic_ay` — student charges, academic-year programs (tuition/fees) *(2021-2024)*
- `ic_py` — student charges by (vocational) program *(2021-2024)*
- `ic_pccampuses` — branch campus locations *(2022-2024)*
- `flags` — response status per survey component; `drvic` — derived cost of attendance; `customcgids` — custom comparison groups; `icmission` — mission statement text

**Fall Enrollment** — *Fall snapshot*
- `ef` — by gender × attendance × level; `efa` — adds race/ethnicity. ⚠️ `eflevel` nests totals — filter one level, e.g. `eflevel=10` for grand total (see §2)
- `efa_dist` — distance-education status; `efb` — by age; `efc` — residence/migration of first-time freshmen
- `efcp` — by major field × race × gender *(2021,2023,2025)*; `efd` — entering class, retention, student-faculty ratio; `drvef` — derived

**12-month Enrollment** — *unduplicated over the year*
- `effy` — 12-month unduplicated headcount (⚠️ `effylev=1` = total; don't blanket-`SUM`, see §2); `effy_dist` — by distance ed; `effy_hs` — dual-enrolled HS students *(2024-2025)*
- `efia` — 12-month instructional activity (contact/credit hours → FTE); `drvef12` — derived

**Graduation Rates**
- `gr` — 150% graduation rates (4-yr cohort 2017 / 2-yr cohort 2020) — ⚠️ `grtype` not additive, prefer `drvgr` (see §2); `gr200` — 200% rates
- `gr_l2` — less-than-2-year institutions; `gr_pell_ssl` — Pell/Stafford recipients; `gr_gender` — gender-unknown revisions *(2023-2024)*; `drvgr` — derived rates

**Outcome Measures**
- `om` — award/enrollment at 4/6/8 years for entering cohorts (all undergrads, incl. part-time/non-first-time); `drvom` — derived rates

**Admissions and Test Scores**
- `adm` — applications, admits, enrollees, SAT/ACT ranges. ⚠️ **non-open-admission reporters only** (~1/3 of institutions — NOT the universe); men+women ≠ total (gender-unknown component), but `enrlft`+`enrlpt` = `enrlt`; `drvadm` — selectivity & yield

**Student Financial Aid**
- `sfa` — student financial aid, combined *(2024-25 only — ⚠️ empty for earlier years; use `sfa_p1`/`sfa_p2` there)*; `sfa_p1`,`sfa_p2` — parts 1 & 2 of the same survey *(2021-2024, before the merge)*. ⚠️ measure suffix = metric: `_n` count · `_p` percent · `_t` total $ · `_a` average $ (see §7)
- `sfav` — military/veterans benefits; look in all of these for aid amounts/recipients

**Cost** *(new 2024-25 only)*
- `cost1` — total cost of attendance detail; `cost2_financialaid` — aid detail; `cost2_netprice` — average net price by income band; `drvcost` — derived

**Finance** — *fiscal year (lags ~1 yr)*
- `f_f1a` — public (GASB); `f_f2` — private-nonprofit / public-FASB; `f_f3` — private for-profit (institutions appear in exactly one by control; ⚠️ different total columns per form — UNION all three for a sector/national total, see §2); `drvf` — derived finance

**Human Resources** — *Fall*
- `eap` — staff by occupation, faculty/tenure status; `s_is` — full-time instructional by rank/tenure/race/gender; `s_sis` — instructional by rank/tenure; `s_oc` — all staff by occupation/race/gender (⚠️ `occupcat`/`ftpt` nest — filter the All-staff codes, don't `SUM`, see §2); `s_nh` — new hires
- `sal_is` — salaries, full-time instructional; `sal_nis` — salaries, noninstructional; `drvhr` — derived

**Academic Libraries**
- `al` — library collections, expenditures, staff (degree-granting); `drval` — derived indicators

---

## 5. Deep dive: `c_a` (Completions — the main table)

Grain: one row per `unitid × cipcode × majornum × awlevel`. Columns: the keys
below + the 30 race/gender count columns from §2.

| column     | meaning                                             |
|------------|-----------------------------------------------------|
| `unitid`   | institution                                         |
| `cipcode`  | 6-digit program code (TEXT, 2020 CIP)               |
| `majornum` | 1 = first major, 2 = second major                   |
| `awlevel`  | award level (see codes below)                       |
| `ctotalt`  | **total awards** (all races, both sexes) — the usual measure |
| `ctotalm` / `ctotalw` | total men / women                        |
| `C<race><T/M/W>` | counts by race/ethnicity × sex (§2)           |

### Award levels (`awlevel`)
```
1  Certificate < 1 year            12  Degrees total
2  Certificate 1–2 years           13  Certificates below baccalaureate total
3  Associate's degree              14  Certificates above baccalaureate total
4  Certificate 2–4 years           15  Degrees/certificates total
5  Bachelor's degree               17  Doctor's – research/scholarship
6  Postbaccalaureate certificate   18  Doctor's – professional practice
7  Master's degree                 19  Doctor's – other
8  Post-master's certificate       20  Certificate < 12 weeks
                                    21  Certificate 12 weeks–1 year
```
(1–8, 17–21 are mutually exclusive real levels; 12–15 are rollup totals — don't
sum a real level with a rollup.)

---

## 6. Deep dive: `hd` (Directory — the dimension table)

Key columns: `instnm` (name), `city`, `stabbr` (state, 2-letter), `zip`,
`countynm`, `control`, `sector`, `iclevel`, `hloffer` (highest offering),
`instsize`, `locale` (urbanicity), `obereg` (region), `cbsa`, `latitude`,
`longitud`, `webaddr`.

Because an institution's name/attributes can change year to year, use the
**`institutions_current`** view (latest directory row per `unitid`) for clean
current labels, or join the **year-matched** `hd` (`ON hd.unitid=x.unitid AND
hd.year=x.year`) when you want the attribute as it was that year.

### Common HD codes
```
control:  1 Public   2 Private nonprofit   3 Private for-profit
iclevel:  1 4-year+  2 2-but-<4-year       3 <2-year
sector:   1 Public 4yr+   2 PrivNP 4yr+   3 PrivFP 4yr+   4 Public 2yr
          5 PrivNP 2yr   6 PrivFP 2yr    7 Public <2yr   8 PrivNP <2yr
          9 PrivFP <2yr   0 Administrative unit
```
Look up any other code with the *Discovery* valuesets query.

---

## 7. Query patterns & gotchas

1. **"Recent N years" = constant bound, never a join.**
   ✅ `WHERE year > (SELECT MAX(year)-3 FROM _years)`
   ❌ `JOIN (SELECT DISTINCT year FROM c_a ORDER BY year DESC LIMIT 3)` — this
   flips SQLite's plan into repeated full scans of the 8M-row `c_a` and hangs.
2. **String-match code columns** (they're TEXT with leading zeros):
   `cipcode='01.0000'`, `stabbr='CA'`. Numeric codes (`awlevel`, `control`,
   `sector`) are numeric: `awlevel=3`.
3. **Rank per year** with `RANK() OVER (PARTITION BY year ORDER BY … DESC)`.
4. Use `institutions_current` for names; it's small and fast.
5. Prefer the `drv*` derived tables when a question asks for a rate/indicator
   IPEDS already computes (grad rate, admit yield, net price, FTE).
6. `_years`, `_family_map`, `_column_presence` (which columns exist in which
   years — schema drift) are helper tables, not survey data.
7. **Financial-aid `sfa*` suffixes encode the metric:** `_N` = number of
   recipients, `_P` = percent of the cohort, `_T` = **total dollars**, `_A` =
   **average dollars** per recipient (e.g. `pgrnt_n` is a headcount, `pgrnt_t` is a
   dollar total). Headline aid variables describe the **full-time first-time**
   undergraduate cohort; other cohorts use the `udg*`/`undg*` prefixes. The combined
   `sfa` table exists only from 2024-25 on — use `sfa_p1`/`sfa_p2` for earlier years.
8. **Not every survey is the full universe.** `adm` covers only non-open-admission
   reporters (~1/3 of institutions) — don't treat its counts as national totals or
   assume a missing institution has zero. When a table has men/women AND total
   columns, the total may exceed men+women (a gender-unknown/unreported bucket) —
   read the total column, don't reconstruct it.

---

## 8. Worked examples (natural language → SQL)

**"Top 20 institutions granting Associate's degrees in Registered Nursing, by
year, over the last 3 years."**
```sql
WITH grads AS (
  SELECT year, unitid, SUM(ctotalt) AS awards
  FROM c_a
  WHERE cipcode='51.3801' AND awlevel=3 AND majornum=1
    AND year > (SELECT MAX(year)-3 FROM _years)
  GROUP BY year, unitid
),
ranked AS (SELECT *, RANK() OVER (PARTITION BY year ORDER BY awards DESC) rk FROM grads)
SELECT r.year, r.rk, ic.instnm, ic.stabbr, r.awards
FROM ranked r JOIN institutions_current ic USING (unitid)
WHERE r.rk<=20 ORDER BY r.year DESC, r.rk;
```

**"How many bachelor's degrees in Computer Science (11.0701) did California
public universities award in the most recent year?"** (→ 7,679)
```sql
SELECT SUM(c.ctotalt) AS cs_bachelors
FROM c_a c
JOIN hd h ON h.unitid=c.unitid AND h.year=c.year
WHERE c.cipcode='11.0701' AND c.awlevel=5 AND c.majornum=1
  AND c.year=(SELECT MAX(year) FROM _years)
  AND h.stabbr='CA' AND h.control=1;
```

**"Total associate's degrees awarded nationally per year, all programs."**
Use the grand-total CIP row `'99'` (or `length(cipcode)=7`) — **not** a sum over
all cipcodes, which would overcount ~4× (see §2).
```sql
SELECT year, SUM(ctotalt) AS associates
FROM c_a WHERE awlevel=3 AND majornum=1 AND cipcode='99'
GROUP BY year ORDER BY year;
```

**"Total fall enrollment nationally, most recent year."** Use the `eflevel=10`
total line — never `SUM` across `eflevel` (≈4× overcount, see §2).
```sql
SELECT year, SUM(eftotal) AS enrolled
FROM ef WHERE eflevel=10 AND year=(SELECT MAX(year) FROM _years)
GROUP BY year;
```

**"Total revenues of U.S. higher education, most recent fiscal year."** Sum all
three finance forms, each with its own total-revenue variable (see §2); querying
one form alone undercounts badly.
```sql
SELECT
    (SELECT SUM(f1d01) FROM f_f1a WHERE year=(SELECT MAX(year) FROM _years))  -- public / GASB
  + (SELECT SUM(f2d16) FROM f_f2  WHERE year=(SELECT MAX(year) FROM _years))  -- private-NP / FASB
  + (SELECT SUM(f3d09) FROM f_f3  WHERE year=(SELECT MAX(year) FROM _years))  -- for-profit
  AS total_revenues;
```

**"What variables are in the admissions table?"** → use the *Discovery* query
against `vartable` with `tablename='ADM2024'`.
