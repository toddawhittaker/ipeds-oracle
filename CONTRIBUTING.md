# Contributing

Developer guide for the IPEDS Query app. For the user-facing overview see
[README.md](README.md); for production deployment see [DEPLOY.md](DEPLOY.md); for
the data model and query conventions see [SCHEMA.md](SCHEMA.md).

## Stack

- **Backend** — Python 3.12, [FastAPI](https://fastapi.tiangolo.com/), an
  embedded tool‑calling agent over any OpenAI-compatible LLM provider
  (`LLM_BASE_URL`; [OpenRouter](https://openrouter.ai/) + DeepSeek by default).
  Local, CPU‑only embeddings via [fastembed](https://github.com/qdrant/fastembed)
  power skill retrieval and the semantic cache.
- **Data** — two SQLite databases: `ipeds.db` (the ~1.9 GB survey data, opened
  **read‑only + immutable**) and `app.db` (users, sessions, chats, learned
  skills, usage — the only thing that's written to).
- **Frontend** — React 18 + [Vite](https://vitejs.dev/), Recharts for charts,
  react‑markdown for answers.
- **Tests** — plain‑script backend suites in `eval/` + [Playwright](https://playwright.dev/)
  end‑to‑end specs in `web/e2e/`.

## Repo layout

```
app/                FastAPI backend
  main.py             app + static serving + startup
  config.py           pydantic-settings (env-driven config)
  llm.py              the tool-calling agent loop
  llmhttp.py          shared OpenAI-compatible transport (llm.py/guard.py/critic.py)
  prompt.py           system prompt (distilled from SCHEMA.md)
  tools/              run_sql (sandboxed), schema/discovery, skills
  routers/            auth, chat (stream/history/CSV), admin
  auth.py, security.py, mailer.py, ratelimit.py
  skills.py           skill library + semantic cache (fastembed)
  importer.py         background "load a new year" job (upload + NCES integrate)
  nces.py             fetch IPEDS .accdb releases from nces.ed.gov (SSRF-hardened)
  db.py               schema + PRAGMA user_version migrations
  logbuffer.py        in-memory log ring buffer (admin Logs view)
web/                React + Vite front end
  src/                Chat, Admin, Chart, Markdown, Login, …
  e2e/                Playwright specs (network-mocked)
eval/                backend test suites + the NL→SQL accuracy harness
scripts/            build_ipeds_db.py, backups, CI fixture builder
data/               source IPEDS{YYYY}{YY}.accdb files (gitignored, large)
docs/               official IPEDS Excel table documentation
.github/workflows/  CI (lint · backend · e2e · image) + manual NL→SQL eval
.claude/agents/     the specialist agent team (see below)
```

## Local development

Requires Python 3.12, Node 20+, and `mdbtools` (`sudo apt-get install mdbtools`,
only needed to build/rebuild `ipeds.db`).

```bash
# Backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
cp .env.example .env && $EDITOR .env      # at minimum LLM_API_KEY, ADMIN_EMAILS
.venv/bin/uvicorn app.main:app --reload   # API on http://localhost:8000

# Frontend (separate terminal)
cd web && npm install
npm run dev                               # UI on http://localhost:5173 (proxies /api → :8000)
```

You need a built `ipeds.db` at the repo root for real queries (see
[Working with the database](#working-with-the-database)). In dev with no
`RESEND_API_KEY`, magic‑link emails are **logged to the console** instead of
sent, so sign‑in works locally — copy the `…/verify?token=` link from the
uvicorn log and open it (it lands on a "Sign in as …?" confirmation page).

Config is env‑driven via `pydantic-settings`; every setting lives in
[`.env.example`](.env.example). The default model is `deepseek/deepseek-v4-flash`
escalating to `deepseek/deepseek-v4-pro`; `LLM_MAX_TOOL_ITERS` caps the agent's
tool rounds.

### Running two sessions at once (git worktrees)

Two dev/agent sessions in **one clone share a single working tree** — a
`git checkout` in one silently switches the other's branch mid-edit, and their
dev servers collide on port 8000. Give each session its own **git worktree**
(separate directory + branch, same `.git`):

```bash
scripts/worktree-add.sh feat/my-branch      # ../ipeds-my-branch, port hint 8100
```

The script symlinks the big shared artifacts (`.venv`, `web/node_modules`,
`.env`, the 2 GB read‑only `ipeds.db`) and **copies** the small stateful DBs
(`app.db`, `logs.db`) so each session's writes stay isolated. It refuses to leave
any symlink that isn't gitignored — **PR #48 clobbered `main` by committing a
symlinked `.venv`/`node_modules` that slipped past a trailing‑slash `.gitignore`
pattern, so never `git add -A` in a worktree.** Run each worktree's server on a
**distinct port** (the script prints the command); remove it when the branch
merges: `git worktree remove ../ipeds-my-branch`.

## Tests

The backend suites are dependency‑light plain scripts (they `sys.exit(1)` on
failure) and need **no** API key — most build a tiny throwaway `app.db` and a
fixture `ipeds.db`.

```bash
# Backend suites (any/all)
.venv/bin/python eval/test_sql_guards.py          # SQL sandbox + timeout watchdog
.venv/bin/python eval/test_backend.py             # auth, admin, skills, cache, CSV
.venv/bin/python eval/test_security.py            # path traversal, de-auth, IDOR, …
.venv/bin/python eval/test_agent_loop.py          # tool-loop synthesis fallback
# also: test_sql_guards_hardening, test_rate_limit, test_migrations,
#       test_result_isolation, test_backup, test_logbuffer, test_mailer, test_guard,
#       test_estimate (disk/time estimator contract, shared with web/src/estimate.js)

# End-to-end UI (network-mocked; no key, no ipeds.db needed)
cd web && npm run test:e2e

# Full NL→SQL accuracy (needs LLM_API_KEY + a real ipeds.db)
.venv/bin/python eval/eval_nl2sql.py
```

`eval_nl2sql.py` is the **model‑swap regression gate** — it checks known answers
(e.g. CA public CS bachelor's = 7,679). Run it before changing the model.

**Coverage standard: every `app/` module stays ≥ 80%** (per-module, not just the
total) — enforced in CI (and the pre-push gate) by `scripts/coverage_check.sh`,
which runs every `eval/test_*.py` under coverage.py and fails if any module drops
below the floor. Every behavior change ships with unit tests. Measure locally:

```bash
scripts/coverage_check.sh                                           # the gate (>=80% or fail)
.venv/bin/coverage report --sort=cover                              # per-module breakdown
```

**Before pushing, run the whole gate:** `scripts/run_ci_local.sh` reproduces all
three CI jobs locally (it's also wired as a `.githooks/pre-push` hook via
`git config core.hooksPath .githooks`). Bypass with `git push --no-verify`; skip
just the slow e2e job with `SKIP_E2E=1`. This is the real merge gate — branch
protection isn't available on this repo's plan, so a red CI check can otherwise
land on `main`.

> A real production `.env` bleeds into the suites two ways. With
> `COOKIE_SECURE=true` the auth‑dependent suites can't hold the session cookie
> over http; with a real `EMAIL_DOMAIN`, `test_backend.py`'s out‑of‑domain
> `stranger@x.com` is refused an access request and the suite fails. Run them
> with both neutralized:
> `COOKIE_SECURE=false EMAIL_DOMAIN= .venv/bin/python eval/test_backend.py`.
> CI has no `.env`, so it just works there — which is exactly why a bleed like
> this only ever breaks the local gate. `scripts/run_ci_local.sh` blanks these
> for you; add any new behavior‑changing setting to that list.

## Lint & format

```bash
.venv/bin/ruff check app scripts eval   # backend lint + import order (matches CI scope; config in pyproject.toml)
cd web && npm run lint             # ESLint (real-defect rules; formatting delegated to Prettier)
cd web && npm run format           # Prettier (write) — optional; existing files aren't mass-reformatted
```

## CI & the contribution workflow

`.github/workflows/ci.yml` runs on every PR and push to `main`, with four jobs:
**lint** (ruff + ESLint), **backend** (all the `eval/test_*` suites against a
fixture DB), **e2e** (Playwright, network‑mocked), and **image** (builds the
Docker image, boots it, and curls `/api/health` as a smoke test). A separate
`nl2sql-eval.yml` is `workflow_dispatch`‑only (it needs an API key + the real DB).

The **image** job gates on the three test jobs, so a broken build or a boot
failure never reaches the registry. It publishes to GHCR only on pushes, not on
PRs: a push to `main` moves `:edge` + `:sha-<short>`, and a `v*` release tag
publishes `:vX.Y.Z` + `:latest`. The VPS pulls those — see DEPLOY.md. (The four
test/lint jobs are still the *merge* gate; publishing is a downstream effect of
landing on `main`.)

Workflow:

1. Branch off `main` (`feat/…`, `fix/…`, `chore/…`, `docs/…`).
2. Keep PRs focused; don't split a single file across PRs.
3. Add or update tests for behavior changes — the **test‑engineer** agent owns
   test files (see below); new behavior is written test‑first where practical.
4. Open a PR; it merges only when lint · backend · e2e · image are green.
5. End commit messages with the `Co-Authored-By:` trailer.

## The agent team

`.claude/agents/` defines a set of specialist [Claude Code](https://claude.com/claude-code)
subagents used to build and review this project: a **project‑manager**
orchestrator plus **architect**, **implementer**, **test‑engineer** (the only
one that writes tests), **code‑reviewer**, **security‑reviewer**,
**a11y‑reviewer**, **ui‑ux**, and **debugger**. They encode the conventions
above; read their `.md` files for the rubrics each applies.

## Working with the database

`ipeds.db` is built from the Access files in `data/` and is **rebuildable** (so
it's gitignored). `app.db` holds the irreplaceable state and is backed up
separately (see [DEPLOY.md](DEPLOY.md)).

```bash
python3 scripts/build_ipeds_db.py             # build ipeds.db from data/*.accdb
python3 scripts/build_ipeds_db.py --dry-run   # just print the table → family map
```

Each physical Access table (e.g. `C2024_A`, `HD2024`) is grouped into a
**family** by stripping the year, and all years are stacked into one table with
`survey_year`, `year` (ending year — use for sorting/filtering), and `src_table`
provenance columns. Metadata lives alongside the data: `valuesets` (code →
label), `vartable` (data dictionary), `tables` (catalog), plus convenience views
like `institutions_current` and `_years`. **[SCHEMA.md](SCHEMA.md) is the full
reference** — read it before writing queries or touching the loader.

Two rules that will bite you if ignored (both detailed in SCHEMA.md):

- **"Recent N years" is a constant bound**, never a join:
  `WHERE year > (SELECT MAX(year)-N FROM _years)`. A join to a distinct‑year
  subquery makes SQLite full‑scan the 8M‑row `c_a` and effectively hang.
- **Never mix CIP / award‑level aggregation levels in one `SUM`.** In `c_a`,
  `cipcode` exists at 2‑/4‑/6‑digit plus a `'99'` grand‑total row that each sum
  to the same total — match an exact 6‑digit code, or use `'99'` for totals.

**A fresh deploy with no `ipeds.db` yet is a supported first-run state**, not an
error: `app/tools/sql.py`'s `ipeds_years()`/`has_ipeds_data()` probe the file
non-raisingly (missing/0-byte/garbage/no-`_years` all yield `[]`/`False`).
`GET /api/auth/me` exposes `has_data`; the chat-stream no-data guard in
`app/routers/chat.py` returns a friendly notice (admin-aware wording, no
conversation created, no agent run) instead of a raw SQL error; and the SPA
routes an admin with no data straight to Admin → Imports on load.

### Adding a new IPEDS year

The easiest path: in the running app, go to **Admin → Imports** and pick the
year(s) from the live NCES catalog (a card grid — Final/Provisional/already
integrated/unavailable, per year). Selecting one or more years and clicking
**Integrate selected (N)** fetches each `.accdb` straight from `nces.ed.gov`
into a transient work dir, then rebuilds the **full union** of every
already-integrated year plus the newly-picked ones into a staging DB, runs
integrity + magnitude checks, and atomically swaps only on success — same
pipeline as a manual upload, just with NCES as the source and always a full
rebuild (never an incremental merge). The work dir is deleted afterward,
success or failure.

Alternatively (no network access, or a file you already have): drop
`IPEDS{YYYY}{YY}.accdb` into `data/` and rerun `scripts/build_ipeds_db.py`, or
use the manual upload fallback (a collapsed `<details>` under the year catalog
in the same Imports tab) — same staging-DB + integrity-checks + atomic-swap
pipeline, just for one file instead of a union.

**`app/nces.py`** is the fetch layer: every URL it requests is built ONLY from
a fixed host (`nces.ed.gov`) + a fixed template + a validated integer year (the
SSRF choke point) — never from caller-supplied strings — and a redirect that
resolves off that host is rejected. `GET /api/admin/import/catalog` merges
`nces.probe_catalog()` (one entry per start year 2004…this year+1, Final
falling back to Provisional, cached ~1h in-process, each carrying the HEAD
response's declared `zip_bytes`) with `importer._years()` (which ending years
are already integrated) and `year_provenance` (which release each integrated
year was actually integrated AS) to mark each year
integrated/update/final/provisional/unknown + selectable. **"update"**: a year
integrated from a **Provisional** release, where NCES now offers **Final** for
it, is offered as a re-selectable "update" (still `integrated: true`, but
`selectable: true`) — re-integrating it re-runs the full union rebuild and
overwrites its `year_provenance` row with the better release. A year with no
provenance row at all (pre-dates this feature) or a NULL release (a manual
upload) is just plain `"integrated"`, never `"update"`. `POST
/api/admin/import/integrate {years:[...]}` validates each year (in range,
available, not a plain already-integrated year — an "update" year IS
accepted), takes the same single-flight import lock as manual upload, and
runs `importer.run_integrate()` in a background thread. Both endpoints derive
status/selectability through the same `_derive_status()` helper in
`app/routers/admin.py` so they can't drift apart.

**Disk-headroom preflight (`app/estimate.py`).** Before `run_integrate` fetches
anything, it estimates the run's peak disk footprint (download + extracted
`.accdb` + rebuilt staging DB, for the **whole union** being rebuilt — not just
the newly-picked years) via the pure `estimate.estimate_integrate()` function,
pads it by `NCES_DISK_SAFETY_FACTOR`, and refuses the job (failing it with a
`"Not enough disk: need ~X, have ~Y free"` message, before touching the
network or the live db) if `shutil.disk_usage` on the `ipeds.db` volume can't
cover it. The same estimator (mirrored, key-for-key in camelCase, by
`web/src/estimate.js` — cross-language agreement is asserted by
`web/e2e/estimate.spec.js` against the shared fixture
`eval/fixtures/estimate_cases.json`) drives a live **disk meter** on the
Imports tab: as an admin checks years, the client re-estimates against just
the checked years' `zip_bytes` (a UX preview, not the server's authoritative
check) and disables "Integrate selected" once the estimate exceeds
`GET /import/catalog`'s `disk.free_bytes`. `estimate.disk_and_calibration()` is
the impure counterpart both `admin.py`'s catalog endpoint and `importer.py`'s
refusal call to gather the live facts (current `ipeds.db` size/year-count,
`shutil.disk_usage`) plus the calibration knobs from `Settings` — all 8 are
listed below.

**Progress + concurrency.** Downloads (and the year-catalog's HEAD probes) run
concurrently — `NCES_DOWNLOAD_CONCURRENCY` / `NCES_PROBE_CONCURRENCY` workers
(default 5 each) via `concurrent.futures.ThreadPoolExecutor` — and each
`download_zip` transfer is bounded by a per-transfer wall-clock
`NCES_DOWNLOAD_DEADLINE_SECONDS` deadline (checked against `time.monotonic()`)
on top of the existing byte caps. `run_integrate` writes structured per-year
progress to `import_jobs.progress` (a JSON blob:
`{overall:{phase,message}, years:{"<start_year>":{step,downloaded_bytes,
total_bytes,pct,...}}}`) as each year moves through
queued→downloading→extracting→fetched (or fails), and `build_check_swap`
updates `overall.phase` through building→checking→swapping→done/failed — the
Imports tab polls this alongside the job's `status`/`log`/`report` and renders
one progress row per year (the raw percent is deliberately kept OUT of the
`role="status"` live region; only the overall phase message is announced).

Relevant config knobs (`.env.example`): `NCES_WORK_DIR` (scratch dir for
fetched `.accdb`s), `NCES_HTTP_TIMEOUT_SECONDS`, `NCES_ZIP_MAX_MB` (per-year
compressed download cap), `NCES_ACCDB_MAX_MB` (per-year uncompressed extract
cap — zip-bomb guard), `NCES_TOTAL_MAX_MB` (ceiling across one integrate run's
whole union), and the 8 disk/time estimator knobs: `NCES_ACCDB_EXPAND_FACTOR`,
`NCES_EST_BANDWIDTH_MBPS`, `NCES_EST_BUILD_SECONDS_PER_YEAR`,
`NCES_DEFAULT_PER_YEAR_DB_MB`, `NCES_DOWNLOAD_DEADLINE_SECONDS`,
`NCES_DISK_SAFETY_FACTOR`, `NCES_PROBE_CONCURRENCY`,
`NCES_DOWNLOAD_CONCURRENCY`. `eval/test_nces.py` exercises the fetch layer
entirely against `httpx.MockTransport` (no socket, no real NCES);
`eval/test_importer.py` and `eval/test_admin_router.py` monkeypatch
`nces.fetch_year` / `nces.probe_catalog` / `importer._years` /
`importer.shutil.disk_usage` / `admin.shutil.disk_usage` as bare module
attributes (never `from ... import`) so tests can substitute fakes without
touching the real network, filesystem, or loader.

### Removing an integrated year (the trashcan)

Each already-integrated (or "update") year card on **Admin → Imports** shows a
`.year-remove` trashcan; clicking it (after a confirm dialog) calls `DELETE
/api/admin/import/year/{start_year}`, which — after the same single-flight
`_import_lock` and a not-integrated/only-remaining-year 400 check as the
router does — spawns `importer.run_deintegrate()` in a background thread.
`run_deintegrate` is a fully **offline** de-integration: it copies live
`ipeds.db` to a staging file (never mutating live in place), `DELETE`s the
removed ending year's rows from every base table that carries a `year` column
(every family table plus `_family_map`/`_years`/`valuesets`/`vartable`/
`tables`), strips that year's survey_year token out of `_column_presence`'s
CSV `years` field (dropping any row whose CSV becomes empty), `VACUUM`s to
reclaim the space, and only then runs **`deintegrate_checks`** — a separate
check function from `integrity_checks`, since `integrity_checks`' >20%-family-
shrink rule exists to catch an accidental loss on *import* and would falsely
fail a deliberate year removal. `deintegrate_checks` instead confirms the
removed year is truly gone, no *other* year was lost, and every surviving
year's per-family row counts are byte-identical to live. On success it
activates staging through the same swap tail `build_check_swap` uses
(`importer._activate_staging` — atomic swap, `data_version` bump, semantic-
cache invalidation) and deletes the removed year's `year_provenance` row. A
disk-headroom preflight (same `importer.shutil.disk_usage` bare-module-attr
convention, ~2x the live db size padded by `NCES_DISK_SAFETY_FACTOR`, to cover
the copy + `VACUUM`'s own temp rebuild) refuses before ever copying anything.

### Rebuild progress bar

`scripts/build_ipeds_db.py` emits machine-readable `##PROGRESS##
tables_total=N` (after planning) and `##PROGRESS## tables_done=k` (after each
table load) lines alongside its normal human-readable prints.
`build_check_swap`'s stdout-streaming loop parses these into
`import_jobs.progress["rebuild"] = {tables_total, tables_done, pct}` (via
`importer._update_rebuild_progress`, throttled to once per integer-pct
change) and keeps marker lines OUT of the human-readable job log. The Imports
tab renders a determinate `[data-testid="rebuild-progress"]` bar
(`role="progressbar"`) whenever `progress.rebuild` is present — i.e. during a
manual upload or NCES integrate rebuild (both go through
`build_check_swap`'s loader subprocess). A year removal (above) never invokes
the loader, so it has no rebuild bar of its own — its own phases show up via
`progress.overall`/the job log instead.
