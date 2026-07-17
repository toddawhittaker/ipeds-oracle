# IPEDS project

A **private FastAPI + React web app** that answers natural-language questions
about U.S. colleges/universities (IPEDS = the U.S. Dept. of Education's census of
postsecondary institutions) for an institution's approved colleagues. A
DeepSeek-backed agent turns each question into SQL against the read-only IPEDS
dataset (`ipeds.db`) and streams back an answer. The app is the work;
`CONTRIBUTING.md` (dev handbook) and `DEPLOY.md` (deploy) are the deeper guides.

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

## The dataset (`ipeds.db`)

The app's agent queries `ipeds.db`; you'll also query it directly — to verify an
aggregation, derive an eval's expected answer, or debug the agent's SQL.

- **`SCHEMA.md` is authoritative — read it before writing or verifying any query.**
  It's injected into every agent prompt. The DB is self-describing: use its
  *Discovery* queries (§3: `tables`, `vartable`, `valuesets`) to look up any
  table/variable/code rather than guessing.
- Inspect it with `sqlite3 -header -column ipeds.db "…"`, and **sanity-check
  magnitudes** against reality (~1M associate's/yr nationally) — a number 2–4× off
  usually means an aggregation-level mistake.

### Critical query gotchas (details in `SCHEMA.md`)
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

### Operational notes
- Wrap ad-hoc CLI queries in `timeout 30 …` so a bad plan can't hang a shell.
  **Never** poll with `until [ -s outfile ]` — a zero-row/hanging query never
  fills the file → infinite loop. If a query hangs, find the holder with
  `fuser ipeds.db` and `kill -9` it (a stuck `sqlite3` locks the DB).
- Tools (apt): `mdbtools` (reads `.accdb`), `sqlite3` CLI.
- Rebuild/extend: drop a new year's `.accdb` into `data/`, then
  `python3 scripts/build_ipeds_db.py`.

---

# Developing the app

## Architecture

### Stack & data stores
- **Backend** — FastAPI (`app/`: `config`, `db`, `auth`, `security`, `mailer`,
  `llm`, `prompt`, `guard`, `critic`, `skills`, `seeds`, `importer`, `nces`,
  `logbuffer`, `ratelimit`, `tools/*`, `routers/*`).
- **Frontend** — a Vite/React SPA (`web/`) with SSE-streamed chat, **client-side
  routed** (react-router-dom): `/`, `/chat/:id`, `/admin` → `/admin/users`,
  `/admin/:tab`, `/verify`, catch-all → `/`. FastAPI's SPA catch-all serves
  `index.html` for all of them, so a hard refresh / deep link never 404s.
- **Three SQLite DBs, all separate:** `ipeds.db` (read-only query target — the
  dataset above), `app.db` (state, with a `PRAGMA user_version` migration runner),
  `logs.db` (persistent admin logs).

### The agent loop
LLM = **DeepSeek** via any OpenAI-compatible provider (`LLM_BASE_URL`, **OpenRouter**
by default, through the shared `app/llmhttp.py` transport; `v4-flash` default →
escalate to `v4-pro`), run as a tool-calling agent loop wrapped in three guards:
- a topical **guardrail** in front (off-topic questions never reach the DB);
- a deterministic SQL **linter** (`app/tools/sqllint.py`) — a pre-flight check that
  flags IPEDS aggregation foot-guns (CIP-rollup / second-major double counts,
  DISTINCT-year full-scans) in the model's SQL and feeds the warning back so the
  agent self-corrects;
- a post-answer **critic** that can force one revision round.

### Self-learning & cache
- **Lessons** — a short generalized **headline** + a longer generalized
  **description** (collapsible in the admin UI) + a commented SQL worked example.
  Retrieved as guidance at query time, and **emitted by the critic** — the *sole*
  lesson source: when it catches a mistake it phrases it as a headline+description
  in one call, reused as both the revision feedback and the stored lesson. Lessons
  start **unverified → an admin approves**; deduped on save; the embedding key is
  **headline+description, never the question**. `SKILLS_ENABLED=0/1` gates the
  on/off eval A/B.
- A **semantic answer cache** short-circuits repeat questions.

### Auth & access control
- Passwordless **magic link**, manual **allowlist**, email via **Resend**. The
  allowlist is the **sole authority on sign-in**.
- Optional `EMAIL_DOMAIN` keeps *access requests* to the institution's own domain
  (and feeds the login form's hint via unauthenticated `GET /api/auth/config`) — it
  does **not** gate sign-in.
- An admin can **deny** a request: it blocks that address **and every `+tag`/case
  variant**, matched on a canonical form in `access_requests.canon_email`
  (lowercased, `+tag` stripped, **dots left alone** — they can be a different real
  person). A blocked address can file no new request (no row, no admin email) and
  gets the **same neutral response** as every other path.
- **No enumeration oracle:** every branch's outbound send is scheduled via
  `BackgroundTasks`, never inline, so denial leaks nothing by response body **or**
  by wall-clock (a synchronous Resend call on only some branches was a measured
  400×+ timing oracle).
- A denial is **reversible**. The Allowlist tab lists every active block ("Blocked
  from requesting access", grouped **canonically** since a block spans `+tag`
  variants — deliberately unlike the pending list above it, grouped by the **raw**
  address since Approve is exact). Its undo control
  (`DELETE /api/admin/access-requests/{email}/denial`) DELETEs the denied rows
  outright, returning the address to a genuine *never-requested* state — **grants
  no access, sends no email**. **Allowlisting** a denied address also clears the
  block (its `denied` rows convert to `approved`, canonically, so offboarding a
  variant later can't resurrect it), but is the stronger action: it grants full
  access **and** emails a welcome link — not always what undoing a mistaken denial
  calls for.

### Admin → Imports (dataset management)
- A live **NCES year catalog**: `app/nces.py` probes `nces.ed.gov` (**SSRF-hardened**
  — URLs are built only from a fixed host + template + a validated year) for which
  start years have a Final/Provisional release; an admin multi-selects years to
  fetch + integrate.
- Each run is a **full rebuild of the union** of already-integrated and
  newly-picked years (never an incremental merge), through the same **staging-DB +
  integrity-checks + atomic-swap** pipeline as a manual upload. Fetched `.accdb`
  files land in a transient `NCES_WORK_DIR` scratch dir **deleted after every run**,
  success or failure — never a permanent store.
- An integrated year can be **removed** (the "trashcan"): `importer.run_deintegrate`
  runs fully **offline** — copy live→staging, `DELETE` that year's rows everywhere,
  `VACUUM`, its own **`deintegrate_checks`** (deliberately *not* `integrity_checks`,
  whose shrink-detector would falsely fail an intentional removal), then the same
  atomic-swap tail. It never touches the network or mutates live in place, and
  (unlike a rebuild) never invokes the loader subprocess.
- A rebuild (manual upload or NCES integrate) streams `scripts/build_ipeds_db.py`'s
  `##PROGRESS##` markers into a determinate rebuild-progress bar on the Imports tab.

### Admin → Usage (privacy)
`GET /api/admin/usage` returns **only aggregates** (totals / series / top_users)
and **deliberately never verbatim question text**. `usage_log.question` is still
written, but echoing it back would be an attributable privacy leak (the
caller-controlled `since`/`until` narrows the window; `top_users` names the user).
A sentinel test in `eval/test_admin_router.py` pins this.

**Full details live in `CONTRIBUTING.md` and `DEPLOY.md` — read them, don't guess.**

## How we work (operating rules — follow these)

**Coding workflow — hybrid.** The routing test is **design uncertainty OR large
blast radius**, *not* "touches multiple files." Route through the
`.claude/agents/` team — `project-manager` orchestrates `architect` →
`test-engineer` (writes failing tests first) → `implementer` →
`security`/`a11y`/`code` reviewers — only when the design is genuinely uncertain
or the change reaches far. A well-specified, low-ambiguity change goes **inline
with a review pass at the end**, even if it spans a few files; **follow-on fixes
to a shipped feature default to inline.** The chain's overhead (stalls, dropped
inter-agent messages, ceremony over trivia like a singular/plural string) costs
more than the specialization saves on small work. **State which path you're
taking.** The `test-engineer`-is-**sole-owner-of-test-files** /
`implementer`-must-not-edit-tests rule is **team-path only**; on inline work,
whoever writes the code writes its tests.

**Testing standard — non-negotiable, but a floor met with real tests.** Keep
test-first for behavior that can realistically regress (ownership/authz scoping,
persistence invariants, security contracts, aggregation correctness); fix
presentation trivia (strings, labels, singular/plural, cosmetic shape) directly.
Every new test must **name the specific regression it catches** — one that only
re-echoes a constant or a UI string a function away is noise and doesn't ship.
**Every `app/` module stays ≥ 80%** line coverage (per-module, not just the
total) — enforced by `scripts/coverage_check.sh` in CI and the pre-push gate —
but that floor is met with tests that **guard real behavior**, never padded with
assertions on constants. Tests are dependency-light scripts in `eval/`
(`sys.exit(1)` on failure, no API key needed). New low-coverage code is not
"done" until it's tested.

**Test pyramid — pick the lowest tier that actually catches the regression.**
*Pure logic* — functions and leaf modules with real input→output behavior — is
unit-tested with **vitest** (`web/`, jsdom, no browser; co-located
`web/src/*.test.js`, table-driven). *Genuine browser truth* —
routing/navigation, focus management, aria-live/AT announcements, back/forward,
SSE-driven DOM — stays in **Playwright** (`web/e2e/`). jsdom's focus and history
models are **not** the browser's, so component tests that lean on routing,
portals, or focus belong in Playwright, not vitest. Don't boot a browser to
check a pure function; don't unit-test a navigation truth jsdom will fake and
get wrong. When a pure function is currently pinned through an e2e assertion,
**move it down** to vitest and thin the now-redundant e2e logic check — keep the
browser *flow* (focus, the aria-live announcement firing) around it. **JS
coverage is gated:** `web/vitest.config.js` enforces a per-file ≥80% line floor
over an explicit **allowlist** of the pure-logic modules under test — the JS
analogue of `coverage_check.sh`'s per-`app/`-module rule. Add a module to that
list when (and only when) it gets real unit tests, so JS logic never silently
escapes a gate. Browser-tested components (`Chat.jsx`, `Admin.jsx`, …) are
deliberately not in the floor — Playwright covers them.

**Run the full gate before pushing.** `scripts/run_ci_local.sh` reproduces all of
CI (ruff `app scripts eval` + ESLint; the `web/` **vitest** unit tests; the
`eval/` backend suites against a fixture DB; Playwright e2e). A
`.githooks/pre-push` hook runs it automatically (bypass: `git push --no-verify`;
skip e2e: `SKIP_E2E=1`). This is the *only* merge gate — GitHub branch protection
isn't available on this repo's plan, so a red CI check can otherwise land on
`main`.

**Ship via branch → PR → merge on green.** Never commit straight to `main`.
Branch (`feat/…`, `fix/…`, `chore/…`, `docs/…`), keep PRs focused (one item),
open a PR, then **watch CI without blocking**: run `gh pr checks <n> --watch` as a
background task (`run_in_background`) and keep working — the harness re-invokes you
when it settles. Merge only when lint · unit · backend · e2e · image are all
green. (The pre-push hook already ran the local gate, so the CI watch mainly
re-confirms and covers the CI-only **image** job.) End commit messages with the
`Co-Authored-By:` trailer.

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

**Test-env gotcha.** A production `.env` (`COOKIE_SECURE=true`, real keys,
`EMAIL_DOMAIN=…`) bleeds into tests — run auth suites with `COOKIE_SECURE=false`,
and blank `LLM_API_KEY`/`RESEND_API_KEY`/`EMAIL_DOMAIN` to match CI's key-free
environment. **`scripts/ci_env.sh` is the single list of those blanks** — sourced
by both `run_ci_local.sh` and `coverage_check.sh`. **Any new setting that changes
behavior has to be blanked in `ci_env.sh`, in the PR that adds it.** CI has no
`.env`, so a bleed fails only on the developer's box, which is also the only
place the merge gate runs. (The list used to be duplicated per script and drifted
silently — `coverage_check.sh` was missing `EMAIL_DOMAIN`, which no gate could
catch, since `run_ci_local.sh` exported it before calling that script.)

**Keep the docs — and the agent team — synced.** When a change alters
architecture, workflow, config, or commands, update `CLAUDE.md` (and
`CONTRIBUTING.md`/`DEPLOY.md`) in the *same* PR. **A major architecture or
infrastructure change — a new test tier, a new gate, a removed/renamed feature, a
changed workflow rule — must also trigger a sweep of `.claude/agents/`.** The
specialist definitions reference the tiers, features, and rules and go stale
silently (the vitest tier landed in #71 while the team still described the removed
👍/👎 feedback until the #72 sweep). Fold the sweep into the same PR when small,
else ship it as an immediate focused follow-up. These files must always reflect
the current state of the project.
