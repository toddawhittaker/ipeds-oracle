# IPEDS project

Two things live in this repo, and most sessions are one or the other:

1. A **data-analysis assistant** that answers natural-language questions about
   U.S. colleges/universities against `ipeds.db` (IPEDS = the U.S. Dept. of
   Education's census of postsecondary institutions).
2. A **private web app** (FastAPI + React) that puts that assistant in front of an
   institution's approved colleagues. **This is the active software project** —
   most code work is here.

Work out which you're doing and read the matching half below.

## Layout
- `ipeds.db` — **the** dataset: SQLite, every IPEDS survey table stacked across
  **whichever collection years the deployment loaded** (each institution picks its
  own via Admin → Imports; `SELECT year FROM _years` is the authoritative list —
  never assume a range). Opened **read-only** by the app.
- `SCHEMA.md` — **read before writing any query.** Data model, conventions,
  family catalog, code references, query patterns, worked NL→SQL examples.
- `app/` — FastAPI backend. `web/` — Vite/React SPA. `eval/` — test suites.
- `app.db` — app state (users, sessions, conversations, usage). `logs.db` —
  persistent server logs. Both separate from the read-only `ipeds.db`.
- `scripts/build_ipeds_db.py` — repeatable loader that builds `ipeds.db` from the
  `data/*.accdb` files (`--dry-run` prints the table→family mapping).
- `CONTRIBUTING.md` — **dev handbook** (stack, local run, tests, lint, CI, agent
  team). `DEPLOY.md` — VPS/Docker deploy. `docs/` — official IPEDS Excel docs.
- `brand/` — logo source masters (icon + wordmark) and the ImageMagick commands
  that regenerate the web favicons + `web/src/assets/wordmark*.png` from them.

---

# A) Answering a natural-language data question

When a new conversation opens and the user hasn't asked something specific, greet
them and offer a few concrete examples to prime them, e.g.:
- "Top 20 institutions awarding Associate's degrees in Registered Nursing
  (CIP 51.3801) over the last 3 years."
- "How many Computer Science (CIP 11.0701) bachelor's degrees did California
  public universities award last year?"
- "National total of Associate's degrees per year, all programs."
- "Which states awarded the most Master's degrees in Education?"

(If their first message is already a data question, skip the greeting.)

**To answer one:**
1. **Load `SCHEMA.md`** for the model + relevant family/columns. The DB is
   self-describing — use the *Discovery* queries in §3 (`tables`, `vartable`,
   `valuesets`) to look up any table/variable/code rather than guessing.
2. Write SQL and run it: `sqlite3 -header -column ipeds.db "…"`.
3. **Sanity-check magnitudes** against reality (e.g. ~1M associate's/yr
   nationally). A number 2–4× off usually means an aggregation-level mistake.

## Critical query gotchas (details in SCHEMA.md)
- **"Recent N years" = a constant bound**, never a join:
  `WHERE year > (SELECT MAX(year)-3 FROM _years)`. A `JOIN (SELECT DISTINCT
  year …)` makes SQLite full-scan the 8M-row `c_a` and effectively hang.
- **Never mix CIP/award-level aggregation levels in a SUM.** In `c_a`, cipcode
  exists at 2-/4-/6-digit + a `'99'` grand-total row, each summing to the same
  total. Match an exact 6-digit code, or use `'99'`/`length(cipcode)=7` for
  totals — never `LIKE '51.%'`.
- Text code columns keep leading zeros (`cipcode='01.0000'`, `stabbr='CA'`);
  numeric codes are numeric (`awlevel=3`, `control=1`).
- Use the `institutions_current` view for clean current institution names.
- `year` = **ending** year of the collection (2024-25 → 2025).

## Operational notes
- Wrap ad-hoc CLI queries in `timeout 30 …` so a bad plan can't hang a shell.
  **Never** poll with `until [ -s outfile ]` — a zero-row/hanging query never
  fills the file → infinite loop. If a query hangs, find the holder with
  `fuser ipeds.db` and `kill -9` it (a stuck `sqlite3` locks the DB).
- Tools (apt): `mdbtools` (reads `.accdb`), `sqlite3` CLI.
- Rebuild/extend: drop a new year's `.accdb` into `data/`, then
  `python3 scripts/build_ipeds_db.py`.

---

# B) Developing the web app

**Architecture:** FastAPI backend (`app/`: config, db, auth, security, mailer,
llm, prompt, guard, critic, skills, seeds, importer, nces, logbuffer, ratelimit, tools/* —
incl. `tools/sqllint.py`, a deterministic pre-flight check that flags IPEDS
aggregation foot-guns (CIP rollup/second-major double counts, DISTINCT-year
full-scan) in model SQL and feeds the warning back so the agent self-corrects —
routers/*) +
React SPA (`web/`, SSE-streamed chat). SQLite everywhere: `ipeds.db` (read-only
query target), `app.db` (state, with a `PRAGMA user_version` migration runner),
`logs.db` (persistent admin logs). Admin → Imports is a live **NCES year
catalog**: `app/nces.py` probes `nces.ed.gov` (SSRF-hardened — URLs are built
only from a fixed host + template + a validated year) for which start years have
a Final/Provisional release, and lets an admin multi-select years to fetch +
integrate; each run is a **full rebuild of the union** of already-integrated and
newly-picked years (never an incremental merge) through the same staging-DB +
integrity-checks + atomic-swap pipeline as a manual upload. Fetched `.accdb`
files land in a transient `NCES_WORK_DIR` scratch dir that's deleted after every
run, success or failure — never a permanent store. An already-integrated year
can also be removed (the "trashcan"): `importer.run_deintegrate` does a fully
**offline** copy-live→staging + `DELETE` that year's rows everywhere + `VACUUM`
+ its own `deintegrate_checks` (deliberately not `integrity_checks`, whose
shrink-detector would falsely fail an intentional removal) + the same
atomic-swap tail as a rebuild, never touching the network or mutating live in
place (unlike a rebuild, it never invokes the loader subprocess). A rebuild
(manual upload or NCES integrate) streams `scripts/build_ipeds_db.py`'s
`##PROGRESS##` markers into a determinate rebuild-progress bar on the Imports
tab. LLM = DeepSeek via any **OpenAI-compatible** provider (`LLM_BASE_URL`,
**OpenRouter** by default, through the shared `app/llmhttp.py` transport;
`v4-flash` default → escalate `v4-pro`) in a tool-calling agent loop, fronted by
a topical **guardrail** and backstopped by a deterministic SQL **linter** +
a post-answer **critic** (both catch IPEDS aggregation errors; the critic can
force one revision round). Auth = passwordless **magic link**, manual allowlist,
email via **Resend**; the allowlist is the sole authority on sign-in, while
optional `EMAIL_DOMAIN` keeps *access requests* to the institution's own domain
(and feeds the login form's hint via unauthenticated `GET /api/auth/config`).
Self-learning = a library of **lessons** — each a short
generalized **headline** + a longer generalized **description** (collapsible in
the admin UI) + a commented SQL worked example — retrieved as guidance and
**emitted by the critic** (the sole lesson source: it phrases a caught mistake
as a headline+description in one call, reused as both the revision feedback and
the stored lesson) when it catches a mistake (lessons start unverified → admin
approves; deduped on save; embedding key = headline+description, never the
question; `SKILLS_ENABLED=0/1` gates the on/off eval A/B) + semantic answer
cache.
**Full details live in `CONTRIBUTING.md` and `DEPLOY.md` — read them, don't guess.**

## How we work (operating rules — follow these)

**Coding workflow — hybrid.** Route *substantial* features (multi-file, needs
design + tests + review) through the `.claude/agents/` team: `project-manager`
orchestrates `architect` → `test-engineer` (writes failing tests first) →
`implementer` → `security`/`a11y`/`code` reviewers. Do *small, well-scoped*
changes inline. **State which path you're taking.** The `test-engineer` is the
**sole owner of test files**; the `implementer` must not edit tests.

**Testing standard — non-negotiable.** Every behavior change ships with unit
tests, and **every `app/` module stays ≥ 80%** line coverage (per-module, not just
the total) — enforced by `scripts/coverage_check.sh` in CI and the pre-push gate.
Tests are dependency-light scripts in `eval/` (`sys.exit(1)` on failure, no API
key needed). New low-coverage code is not "done" until it's tested.

**Run the full gate before pushing.** `scripts/run_ci_local.sh` reproduces all of
CI (ruff `app scripts eval` + ESLint; the `eval/` backend suites against a
fixture DB; Playwright e2e). A `.githooks/pre-push` hook runs it automatically
(bypass: `git push --no-verify`; skip e2e: `SKIP_E2E=1`). This is the *only*
merge gate — GitHub branch protection isn't available on this repo's plan, so a
red CI check can otherwise land on `main`.

**Ship via branch → PR → merge on green.** Never commit straight to `main`.
Branch (`feat/…`, `fix/…`, `chore/…`, `docs/…`), keep PRs focused (one item),
open a PR, watch `gh pr checks <n> --watch`, merge only when lint · backend · e2e
· image are all green. End commit messages with the `Co-Authored-By:` trailer.

**Two sessions → use a worktree.** If a second dev/agent session runs in this
repo, they share one working tree — a `git checkout` in one moves the other's
branch mid-edit and their servers collide on port 8000. Isolate each with a git
worktree: `scripts/worktree-add.sh <branch>` (symlinks `.venv`/`node_modules`/
`.env`/`ipeds.db`, copies `app.db`/`logs.db`, runs the server on a distinct
port). Before any git write op, `git branch --show-current` + `git status` to see
whose branch is loaded; **never `git add -A` in a worktree** (PR #48 committed a
symlinked `.venv` and clobbered `main`). See `CONTRIBUTING.md` → *Running two
sessions at once*.

**Release/deploy (CI/CD).** CI's **image** job builds + smoke-tests the Docker
image and publishes to GHCR: a `main` push moves `:edge`/`:sha-<short>`; a **`v*`
git tag** publishes `:vX.Y.Z` + `:latest`. Production is **pull-on-the-box** —
the VPS runs `scripts/deploy.sh <tag>` (no inbound SSH). Details in `DEPLOY.md`.

**Test-env gotcha.** A production `.env` (`COOKIE_SECURE=true`, real keys) bleeds
into tests — run auth suites with `COOKIE_SECURE=false`, and blank
`LLM_API_KEY`/`RESEND_API_KEY` to match CI's key-free environment
(`run_ci_local.sh` already does this).

**Keep the docs synced.** When a change alters architecture, workflow, config, or
commands, update `CLAUDE.md` (and `CONTRIBUTING.md`/`DEPLOY.md`) in the *same*
PR. These files must always reflect the current state of the project.
