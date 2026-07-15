# Contributing

Developer guide for the IPEDS Query app. For the user-facing overview see
[README.md](README.md); for production deployment see [DEPLOY.md](DEPLOY.md); for
the data model and query conventions see [SCHEMA.md](SCHEMA.md).

## Stack

- **Backend** — Python 3.12, [FastAPI](https://fastapi.tiangolo.com/), an
  embedded tool‑calling agent over [OpenRouter](https://openrouter.ai/)
  (DeepSeek by default). Local, CPU‑only embeddings via
  [fastembed](https://github.com/qdrant/fastembed) power skill retrieval and the
  semantic cache.
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
  llm.py              the tool-calling agent loop (OpenRouter)
  prompt.py           system prompt (distilled from SCHEMA.md)
  tools/              run_sql (sandboxed), schema/discovery, skills
  routers/            auth, chat (stream/history/CSV), admin
  auth.py, security.py, mailer.py, ratelimit.py
  skills.py           skill library + semantic cache (fastembed)
  importer.py         background "load a new year" job
  db.py               schema + PRAGMA user_version migrations
  logbuffer.py        in-memory log ring buffer (admin Logs view)
web/                React + Vite front end
  src/                Chat, Admin, Chart, Markdown, Login, …
  e2e/                Playwright specs (network-mocked)
eval/                backend test suites + the NL→SQL accuracy harness
scripts/            build_ipeds_db.py, backups, CI fixture builder
data/               source IPEDS{YYYY}{YY}.accdb files (gitignored, large)
docs/               official IPEDS Excel table documentation
.github/workflows/  CI (lint · backend · e2e) + manual NL→SQL eval
.claude/agents/     the specialist agent team (see below)
```

## Local development

Requires Python 3.12, Node 20+, and `mdbtools` (`sudo apt-get install mdbtools`,
only needed to build/rebuild `ipeds.db`).

```bash
# Backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
cp .env.example .env && $EDITOR .env      # at minimum OPENROUTER_API_KEY, ADMIN_EMAILS
.venv/bin/uvicorn app.main:app --reload   # API on http://localhost:8000

# Frontend (separate terminal)
cd web && npm install
npm run dev                               # UI on http://localhost:5173 (proxies /api → :8000)
```

You need a built `ipeds.db` at the repo root for real queries (see
[Working with the database](#working-with-the-database)). In dev with no
`RESEND_API_KEY`, magic‑link emails are **logged to the console** instead of
sent, so sign‑in works locally — copy the `…/api/auth/verify?token=` link from
the uvicorn log.

Config is env‑driven via `pydantic-settings`; every setting lives in
[`.env.example`](.env.example). The default model is `deepseek/deepseek-v4-flash`
escalating to `deepseek/deepseek-v4-pro`; `LLM_MAX_TOOL_ITERS` caps the agent's
tool rounds.

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
#       test_result_isolation, test_backup, test_logbuffer

# End-to-end UI (network-mocked; no key, no ipeds.db needed)
cd web && npm run test:e2e

# Full NL→SQL accuracy (needs OPENROUTER_API_KEY + a real ipeds.db)
.venv/bin/python eval/eval_nl2sql.py
```

`eval_nl2sql.py` is the **model‑swap regression gate** — it checks known answers
(e.g. CA public CS bachelor's = 7,679). Run it before changing the model.

> If a real production `.env` (with `COOKIE_SECURE=true`) is present, the
> auth‑dependent suites can't hold the session cookie over http — run them with
> `COOKIE_SECURE=false .venv/bin/python eval/test_backend.py`. CI has no `.env`,
> so it just works there.

## Lint & format

```bash
.venv/bin/ruff check app/          # backend lint + import order (config in pyproject.toml)
cd web && npm run lint             # ESLint (real-defect rules; formatting delegated to Prettier)
cd web && npm run format           # Prettier (write) — optional; existing files aren't mass-reformatted
```

## CI & the contribution workflow

`.github/workflows/ci.yml` runs on every PR and push to `main`, with three jobs:
**lint** (ruff + ESLint), **backend** (all the `eval/test_*` suites against a
fixture DB), and **e2e** (Playwright, network‑mocked). A separate
`nl2sql-eval.yml` is `workflow_dispatch`‑only (it needs an API key + the real DB).

Workflow:

1. Branch off `main` (`feat/…`, `fix/…`, `chore/…`, `docs/…`).
2. Keep PRs focused; don't split a single file across PRs.
3. Add or update tests for behavior changes — the **test‑engineer** agent owns
   test files (see below); new behavior is written test‑first where practical.
4. Open a PR; it merges only when all three CI jobs are green.
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

### Adding a new IPEDS year

Drop the new `IPEDS{YYYY}{YY}.accdb` into `data/` and rerun
`scripts/build_ipeds_db.py`, **or** upload it in the running app under
**Admin → Imports** (which builds to a staging DB, runs integrity + magnitude
checks, and atomically swaps only on success).
