# Contributing

Developer guide for the IPEDS Oracle app. For the user-facing overview and
self-hosting see [README.md](README.md); for the data model and query conventions
see [SCHEMA.md](docs/SCHEMA.md).

## Stack

- **Backend** — Python 3.12, [FastAPI](https://fastapi.tiangolo.com/), an
  embedded tool‑calling agent over any OpenAI-compatible LLM provider
  (`LLM_BASE_URL`, [OpenRouter](https://openrouter.ai/) by default; you choose
  the model).
  Local, CPU‑only embeddings via [fastembed](https://github.com/qdrant/fastembed)
  power skill retrieval and the semantic cache.
- **Data** — three SQLite databases, all separate: `ipeds.db` (the ~1.9 GB survey
  data, opened **read‑only**), `app.db` (users, sessions, chats, learned skills,
  usage — the irreplaceable state, with a `PRAGMA user_version` migration runner),
  and `logs.db` (persistent server logs behind the admin Logs tab).
- **Frontend** — React 19 + [Vite](https://vitejs.dev/), [React Router](https://reactrouter.com/)
  (declarative `react-router` v8) for routing, Recharts for charts, react‑markdown
  for answers.
- **Tests** — plain‑script backend suites in `backend/tests/`, [vitest](https://vitest.dev/)
  unit tests for pure JS logic in `frontend/src/*.test.js`, and
  [Playwright](https://playwright.dev/) end‑to‑end specs in `frontend/e2e/`.

## Repo layout

```
backend/              the Python side (all Python tooling runs from here)
  app/                FastAPI backend
    main.py           app + static serving + startup
    config.py         pydantic-settings (env-driven config)
    llm.py            the tool-calling agent loop
    llmhttp.py        shared OpenAI-compatible transport (llm.py/guard.py/critic.py)
    prompt.py         system prompt (distilled from docs/SCHEMA.md)
    guard.py          topical guardrail in FRONT of the agent (off-topic never hits the DB)
    critic.py         post-answer review that can force one revision round
    feedback.py       distills a user's corrective feedback into a lesson
    version.py        cached, fail-open "is a newer release out?" check against GitHub
    csrf.py           \
    secheaders.py      | three pure-ASGI layers, outermost first:
    bodylimit.py      /  security headers -> CSRF origin check -> request-body cap
    grounding.py      figure + table grounding: can the answer's hero number and
                      its results-table cells be reproduced from the query results
                      (this turn's + the recent conversation window)? observe-only;
                      feeds Admin -> Usage "Grounded figures" / "Grounded cells"
    tools/            run_sql (sandboxed), schema/discovery, skills
    routers/          auth, chat (stream/history/rename/CSV), admin
    auth.py, security.py, mailer.py, ratelimit.py
    skills.py         skill library + semantic cache (fastembed)
    importer.py       background "load a new year" job (upload + NCES integrate)
    nces.py           fetch IPEDS .accdb releases from nces.ed.gov (SSRF-hardened)
    db.py             schema + PRAGMA user_version migrations (forward-only; a
                      too-new app.db REFUSES to boot rather than write damage)
    logbuffer.py      persistent server logs (logs.db) + access-log redaction
  tests/              backend test suites + the NL→SQL accuracy harness
  pyproject.toml      ruff config; requirements.txt / -dev.txt / .lock
frontend/             React + Vite front end
  src/                Chat, Admin, Chart, Markdown, Login, … — client-side
                      routed (react-router); route table in App.jsx
                      ("/", "/chat/:id", "/admin", "/admin/:tab",
                      "/admin/:tab/:sub", "/verify", catch-all -> "/");
                      co-located *.test.js are vitest units. App-wide UI
                      services are mounted once at the root: Toast.jsx
                      (useToast) for transient result toasts; ConfirmModal.jsx
                      (useConfirm) — the SINGLE confirmation mechanism
    admin/            the five Admin pages (Admin.jsx itself is a ~110-line
                      SHELL) + the pure admin/format.js helpers
    inflight.js       the app's one module-level store: turns still streaming,
                      kept outside React because Chat UNMOUNTS on /admin
  e2e/                Playwright specs (network-mocked)
docs/               SCHEMA.md (data model + query guide), DATASET.md,
                    ARCHITECTURE.md, AGENT_LOOP.md, AUTH_AND_SECURITY.md,
                    ADMIN.md (how the system works — CLAUDE.md is process only),
                    TESTING.md (the tiers, the gates, the traps), RELEASING.md,
                    USER_GUIDE.md, ADMIN_GUIDE.md, AI_ASSISTED_ENGINEERING.md,
                    images/
scripts/            build_ipeds_db.py, backups, CI fixture builder, run_ci_local.sh
data/               source IPEDS{YYYY}{YY}.accdb (gitignored; online-only via NCES now)
.github/workflows/  CI (lint · secrets · sast · backend · unit · e2e · image) +
                    CodeQL + manual NL→SQL eval
.claude/agents/     the specialist agent team (see below)
```

## Local development

Requires Python 3.12, Node 22+ (React Router v8's floor), and `mdbtools` (`sudo apt-get install mdbtools`,
only needed to build/rebuild `ipeds.db`).

```bash
# Backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
cp .env.example .env && $EDITOR .env      # at minimum LLM_API_KEY, ADMIN_EMAILS
.venv/bin/uvicorn app.main:app --reload   # API on http://localhost:8000

# Frontend (separate terminal)
cd frontend && npm install
npm run dev                               # UI on http://localhost:5173 (proxies /api → :8000)
```

You need a built `ipeds.db` at the repo root for real queries (see
[Working with the database](#working-with-the-database)). In dev with no
`RESEND_API_KEY`, magic‑link emails are **logged to the console** instead of
sent, so sign‑in works locally — copy the `…/verify#token=` link from the
uvicorn log and open it (it lands on a "Sign in as …?" confirmation page).
The token is in the URL **fragment** so it is never sent to the server and so
never reaches an access log; the console line comes from the mail logger, which
is deliberately exempt from redaction precisely so local sign‑in keeps working.

For a quick **single‑port build to click around** (the SPA built and served from
`:8000`, no Vite dev proxy), the repo‑root **`Makefile`** wraps it: `make up`
(LLM key, no Resend — sign‑in links go to `server.log`), `make full` (also sends
real email), `make down` to stop. Details in `.claude/skills/interactive-testing`.

Config is env‑driven via `pydantic-settings`; every setting lives in
[`.env.example`](.env.example) — enforced by `backend/tests/test_env_example.py`,
which diffs the file against `config.Settings` in both directions, so a new
setting can't ship undocumented and a removed one can't linger.

`MODEL_DEFAULT` ships with **no default and must be set** — the app is
provider-agnostic, and a shipped default would both brand it with one vendor and
silently route a self-hoster's traffic to a model they never picked. Use whatever
ID your `LLM_BASE_URL` serves; it needs tool-calling support (a model that can't
call tools falls back to the fence path — see `STRUCTURED_EMISSION_ENABLED`).
`MODEL_ESCALATION` is optional (blank = never escalate) and names a stronger model
the agent reaches for after repeated tool failures. Setting `LLM_API_KEY` without
`MODEL_DEFAULT` logs a CRITICAL at boot (`main._missing_model_warning`) rather than
failing opaquely on the first question. `LLM_MAX_TOOL_ITERS` caps the agent's tool
rounds.

### Running two sessions at once (git worktrees)

Two dev/agent sessions in **one clone share a single working tree** — a
`git checkout` in one silently switches the other's branch mid-edit, and their
dev servers collide on port 8000. Give each session its own **git worktree**
(separate directory + branch, same `.git`):

```bash
scripts/worktree-add.sh feat/my-branch      # ../ipeds-my-branch, port hint 8100
```

The script symlinks the big shared artifacts (`.venv`, `frontend/node_modules`,
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

> **Changing `grounding.py`? Measure both ways, and check the probe first.**
> Passing tests are not enough: every route in that module trades recall against
> the risk of "verifying" a number that isn't in the data. Measure **recall on
> real answers AND precision on fabricated ones**, on the retained corpus, and
> put both numbers in the PR body.
>
> The probe is the part that goes wrong. Four have lied here:
> - **zeros left unperturbed** — no multiplicative factor moves `0`, and the cell
>   then grounds against any result holding a zero. Reported 10.95% fabricated
>   cells against a true 1.87%. Perturb `0` **additively**.
> - **one shared scale factor** — preserves every ratio, share and `pct_change`,
>   so those routes reproduce exactly on "fabricated" data. Use a factor **per
>   number**.
> - **a constant-increment fixture** — 1,194 "candidate" values that were really
>   two, reporting a clean 0.0% for a route that actually verifies 49% of
>   fabricated figures at the row cap.
> - **a wrong-database join** — matching by content across two DBs where the same
>   id is a different row.
>
> Assert the probe actually changed something, and treat a result that is *too
> clean* or that contradicts something already known as a broken probe, not a
> finding. When a route breakdown is available (instrument `_match_at_row` /
> `_match_in_column` to record which op matched), read it before believing an
> aggregate — that turned "we have an 11% problem" into "the probe skipped
> zeros" in ten minutes.

```bash
# Every backend suite, from ONE list (a glob — adding a suite is adding the file)
scripts/run_backend_suites.sh

# Or run one directly while iterating
.venv/bin/python backend/tests/test_sql_guards.py          # SQL sandbox + timeout watchdog
.venv/bin/python backend/tests/test_backend.py             # auth, admin, skills, cache, CSV
.venv/bin/python backend/tests/test_security.py            # path traversal, de-auth, IDOR, …
.venv/bin/python backend/tests/test_grounding.py           # figure + table reproduction

# Web unit tests — the FAST pure-logic tier (vitest + jsdom, no browser)
cd frontend && npm run test:unit          # runs with the JS coverage floor
cd frontend && npm run test:unit:watch    # watch mode for iterating

# End-to-end UI (network-mocked; no key, no ipeds.db needed)
cd frontend && npm run test:e2e                     # dev server: fast start, for iterating
cd frontend && E2E_PREVIEW=1 npm run test:e2e       # static build: ~3.4x faster over a FULL run

# Full NL→SQL accuracy (needs LLM_API_KEY + a real ipeds.db)
.venv/bin/python backend/tests/eval_nl2sql.py
```

> **The backend suite list is a glob on purpose.** It replaced a hand-kept array
> in `run_ci_local.sh` *plus* ~30 hand-written steps in `ci.yml`, which had
> already drifted: `test_grounding.py` and `test_version.py` were in neither, so
> a grounding regression surfaced as a bare non-zero exit from a step labelled
> "Coverage gate". Add a suite by adding the file.

**Which e2e server to use.** `E2E_PREVIEW=1` builds the app and serves the static
bundle — measurably faster across the whole suite (107s → 31s), because
`npm run dev` re-transforms modules per route on every `page.goto`. The dev
server stays the default for iterating (instant start, and `reuseExistingServer`
keeps a warm one between runs). **Reuse is deliberately off in preview mode**: a
lingering preview server serves whatever was built when it started, so reusing
one runs the suite against stale source and reports a false green.

**Test pyramid.** Pure input→output logic goes in **vitest** (`frontend/src/*.test.js`,
table-driven, no browser). Genuine browser truth — routing, focus, aria-live/AT,
back/forward, SSE-driven DOM — stays in **Playwright** (`frontend/e2e/`); jsdom's focus
and history models aren't the browser's. Pick the lowest tier that can actually
catch the regression.

**The axe gate** (`frontend/e2e/a11y.spec.js`) fails on `critical` **and
`serious`**, and scans the app as it actually renders: a full answer with its
disclosures open, a mid-stream answer, and all seven admin paths, in **both
themes** (19 scans: 7 paths x 2 themes, plus Login, Chat, Chat-in-dark, an
answer, and a mid-stream answer). It used to see only Login and the *empty* Chat state — the
two least-populated screens in the product — which is how two whole classes of
defect shipped past a green suite. Two fixture rules the scans depend on: mock
admin lists with **content, not empty arrays** (an empty table renders none of
the elements whose contrast could be wrong), and the answer fixture must carry a
**chart**. Note also that axe files a one-character element as `incomplete`
rather than a violation, so a count badge's contrast needs a direct
computed-style assertion — the gate alone won't catch it.

**Opening a `HelpPopover` in an e2e spec: use `focus()`, never a bare `click()`.**
It opens on hover *and* focus while its `onClick` toggles, so a `click()`
(mouseenter → focus → click) races React's commit and can toggle the popover
straight back shut — a flake that appeared roughly one loaded run in four while
passing 100/100 under `--repeat-each=25`. `focus()` opens it unconditionally and
is the keyboard route anyway. (`csv-import.spec.js`'s awaited
`focus()`-then-`click()` deliberately tests the touch-tap swallow and is correct.)

More generally, **when a flake has a candidate mechanism, force the bad branch
instead of counting runs.** Repetition could not settle that one; a throwaway
spec that hovered, awaited the popover visible, then clicked failed 5/5 while the
fix passed 5/5 — seconds, and conclusive.

`eval_nl2sql.py` is the **model‑swap regression gate** — it checks known answers
(e.g. CA public CS bachelor's = 7,679). Run it before changing the model.

**Coverage standard: every `backend/app/` module stays ≥ 80%** (per-module, not just the
total) — enforced in CI (and the pre-push gate) by `scripts/coverage_check.sh`,
which runs every `backend/tests/test_*.py` under coverage.py and fails if any module drops
below the floor. Every behavior change ships with unit tests. Measure locally:

```bash
scripts/coverage_check.sh                                           # the gate (>=80% or fail)
.venv/bin/coverage report --sort=cover                              # per-module breakdown
```

The **JS side** has its own floor: `frontend/vitest.config.js` gates a per-file ≥ 80%
line coverage over the pure-logic modules under test — `npm run test:unit` fails if
one dips. That set is **derived from the filesystem**: any `src/foo.js` with a
co-located `src/foo.test.js` is gated, so writing the test is the whole opt-in.
(It used to be a hand-kept array, which drifted the usual way — a module could get
tests and stay silently ungated, with no failure and no signal.) Browser-tested
components stay out of the floor: they have no `*.test.js`, and Playwright covers
them.

**Before pushing, run the whole gate:** `scripts/run_ci_local.sh` reproduces
every CI job locally (it's also wired as a `.githooks/pre-push` hook via
`git config core.hooksPath .githooks`). Bypass with `git push --no-verify`; skip
just the slow e2e job with `SKIP_E2E=1`. A **deletion-only push**
(`git push origin --delete <branch>`, e.g. pruning a merged branch) skips the gate
automatically — it uploads no code, so there is nothing to test; a push that mixes
a deletion with real commits still runs it in full (`backend/tests/test_pre_push_hook.py`
pins that split). It's a fast pre-check — the
**authoritative gate is GitHub CI**: `main` is **branch-protected**, so every
change lands through a PR with all checks (secrets · lint · unit · backend · e2e ·
image) green before it can merge; direct and force pushes to `main` are blocked.
The **secrets** check runs `gitleaks` over full history (defense-in-depth under
GitHub's native push protection); the local gate runs it too when `gitleaks` is
on your `PATH`.

> **Adding a step to `.github/workflows/ci.yml` means adding it to
> `scripts/run_ci_local.sh` in the SAME PR.** These are two hand-maintained lists
> of the same thing, and they have already drifted once: #220 added the
> *"types are in sync"* typecheck to the CI Lint job only, so for two PRs the
> local gate passed while CI failed — the exact failure this script exists to
> prevent. Same shape as the backend suite lists that `run_backend_suites.sh`
> now globs away.

> A real production `.env` bleeds into the suites two ways. With
> `COOKIE_SECURE=true` the auth‑dependent suites can't hold the session cookie
> over http; with a real `EMAIL_DOMAIN`, `test_backend.py`'s out‑of‑domain
> `stranger@x.com` is refused an access request and the suite fails. Run them
> with both neutralized:
> `COOKIE_SECURE=false EMAIL_DOMAIN= .venv/bin/python backend/tests/test_backend.py`.
> CI has no `.env`, so it just works there — which is exactly why a bleed like
> this only ever breaks the local gate. `scripts/ci_env.sh` blanks these for you
> and is sourced by both `scripts/run_ci_local.sh` and `scripts/coverage_check.sh`
> — **add any new behavior‑changing setting to `ci_env.sh`**, which is the one
> list. Keeping a per‑script copy is what let `coverage_check.sh` drift without
> `EMAIL_DOMAIN`: nothing could catch it, because the pre‑push gate exported the
> blank before calling it and CI has no `.env` to bleed. It only failed when run
> directly on a dev box, where it looked like a real test failure.

### Screenshots for the guides

`docs/images/*.png` is regenerated by one command:

```bash
scripts/docs-shots.sh          # needs ImageMagick; ~30s
```

It drives the real app through `frontend/e2e/docs.capture.js` (a capture spec,
not a test — `playwright.config.js` ignores `*.capture.js` so CI never runs it)
and stitches the light/dark pairs. **Run it whenever a change is visible** —
layout, navigation, a new badge or mark — then look at the shots that moved.

Two constraints it exists to hold. It renders against the **e2e mock harness**,
never a live deployment: admin screens photographed against real data would
publish real users' email addresses, and a live LLM answer is nondeterministic,
so a re-shoot would silently reword the docs. And it **fails if the page is
showing the error boundary at the moment of capture** — a readiness assertion
only proves the page was alive when it ran, and a late-arriving fetch once
crashed the Users page a few hundred milliseconds after that check, publishing
two "Something went wrong" cards into the guides.

The first set was made with a throwaway spec and went stale invisibly: every
image still showed the pre-redesign top bar weeks after Admin and the theme
toggle moved into the avatar menu. No test can catch that, and it is the first
thing a new user sees.

## Frontend UI conventions

**Confirmations use `useConfirm()`, never `window.confirm`.** `ConfirmModal.jsx`
(mounted once at the app root, inside `ToastProvider`) is the single, app-styled
confirmation mechanism — an accessible `role="alertdialog"`/`dialog` over a dimmed,
`inert` background with a focus trap, neutral/warning/danger variants, and async
processing built in. Feature code calls `confirm({ variant, title, body,
confirmLabel, onConfirm, onSuccess, successToast, errorToast, … })` and supplies
only the content, severity, action callback, and result messages; the component
owns overlay/dimming, focus (Cancel is focused first — a destructive action is
never auto-focused), dismissal (Escape/overlay/Cancel, disabled while
processing), the loading state, the in-modal error + retry on failure, and
returning focus to the opener on cancel. `onConfirm` runs the mutation (throw →
in-modal error + `errorToast`, modal stays open); `onSuccess` runs after the modal
closes and owns any post-reload focus move (the [focus-restore-vs-reload race]).
No feature may fall back to a browser-native dialog. Reversible actions (undo a
denial, delete a fresh unreviewed lesson) deliberately skip confirmation. The
component's browser behavior is pinned in `frontend/e2e/confirm-modal.spec.js`.

**Admin tables use `<DataTable>`, never a hand-rolled table.** `DataTable.jsx` is
the single reusable admin table — search, sortable `aria-sort` headers, page-size
select (10/25/50/100), Prev/Next + range label, a debounced `aria-live` status,
filler rows (constant height), and focus management (a `forwardRef` imperative
handle: `focusSearch()`, `focusRowAction(rowKey)`). Feature code passes a `columns`
config, a `rowKey`, a `renderActions(row)` slot, and a **pure pipeline `config`**
(`{ fields, comparators, tiebreak, nouns }`). The pipeline itself — filter → sort →
paginate → range label — lives in `datatable.js` and is unit-tested in
`datatable.test.js` (vitest); the Users list config is `userlist.js`'s `USER_CONFIG`.
The component's browser truth is covered by Playwright (`users-table.spec.js`,
`deny-access-request.spec.js`, `undo-denial.spec.js`). Add a new admin table as a
config over `<DataTable>`, not a copy.

**Bulk row-selection is an opt-in `<DataTable>` feature, not a fork.** Passing
`selectable` (plus `selectionId`, `selectionMode`, `selectedIds`, `rowSelectable`,
`rowSelectLabel`, `onToggleRow`, `onTogglePage`, `renderSelectionBar`,
`onSearchChange`) turns on a checkbox column (first column, tri-state page
header) and a `renderSelectionBar` slot above the table; every existing
`<DataTable>` usage that omits these props renders byte-for-byte as before. The
Allowlist tab's three tables (Users, Pending requests, Blocked users) each hold
their own `useTableSelection()` hook instance (`selection = { mode:
"explicit"|"all", selectedIds }` — `"all"` mode's `selectedIds` holds the
*excluded* ids, for "select all matching" across a search-narrowed set) and
render `<BulkBar>` (`frontend/src/BulkBar.jsx`) as a **contextual** action
toolbar — it renders `null` unless ≥1 row is selected (the standard
Gmail/Linear pattern, not a persistent bar of disabled buttons), pairs a live
"N selected" count + Clear with stable-verb action buttons (destructive ones
split past a divider) and the "select all N matching" escalation banner. The pure
tri-state/count/eligibility/copy logic lives in `selection.js` (vitest,
`selection.test.js`) — everything else (checkbox tri-state incl.
`indeterminate`, the search-clears-selection flow, the confirm → processing →
toast → refresh flow) is Playwright-covered (`admin-bulk-actions.spec.js`). The
three bulk endpoints (`POST /api/admin/allowlist/bulk-action`,
`POST /api/admin/access-requests/bulk`, `POST /api/admin/access-requests/denial/bulk`)
reuse the exact same mutation helpers the single-row endpoints call
(`backend/app/routers/admin.py`'s `_set_admin`/`_remove_user`/`_approve_allowlist`/
`_deny_group`/`_clear_denial_group`), recompute eligibility per record, and are
capped at `BULK_MAX_ITEMS`.

## Design system sync (claude.ai/design)

`/design-sync` publishes this UI to a **claude.ai/design** project so that Claude
Design builds screens out of the real components instead of generic ones. The
project is `IPEDS Oracle Design System`; its id is pinned in
`.design-sync/config.json`, so a re-sync finds it without asking.

Committed inputs live in `.design-sync/`: `config.json`, `conventions.md` (the
usage guide prepended to the generated README, which becomes the design agent's
system prompt), `previews/<Name>.tsx` (one authored preview per component),
`groups/<Name>.md` (a 3-line frontmatter stub that assigns the card's group), and
`NOTES.md`. **Read `NOTES.md` first** — it carries the gotchas and the re-sync
risk list. Generated output (`ds-bundle/`) and the staged converter (`.ds-sync/`)
are gitignored.

Re-run from the **repo root** (`cfg.entry` is resolved against the working
directory, not the package):

```bash
node .ds-sync/package-build.mjs   --config .design-sync/config.json \
  --node-modules ./frontend/node_modules --out ./ds-bundle
node .ds-sync/package-validate.mjs ./ds-bundle
```

Two things about this repo make the sync non-standard, and **both fail silently**:

**1. There is no library build, so `frontend/ds-entry.js` is load-bearing.**
`frontend/package.json` is `private` with no `main`/`module`/`exports`, so the
converter falls back to synthesizing an entry with `export * from "<each src
file>"` — and `export *` does **not** re-export a module's `default`. Nearly every
component here is `export default function X`, so that fallback put only the icons
and the two providers on `window.IpedsOracle`: 18 components were missing from the
bundle while still getting preview cards, and the build exited 0. The tell is a log
line reading `bundle export list: N` well below the component count. When a
component becomes reusable, add it in **two** places — a named export in
`frontend/ds-entry.js` and a pin in `cfg.componentSrcMap`. Those two lists are the
design system's public surface. (`frontend/ds-preview-env.js` is the other
sync-only file: it supplies a Router so `UserMenu`'s `<Link>` can render in a card.
Neither file is imported by the app.)

**2. There is no TypeScript, so the prop contracts are DERIVED from JSDoc.**
`react/prop-types` is off and nothing else declares a prop, so props are annotated
as JSDoc on the components themselves and `tsc --emitDeclarationOnly`
(`frontend/tsconfig.json`) emits `frontend/types/`, which the converter reads.
`cfg.dtsPropsFor` no longer exists — a prop is declared in exactly one place, next
to the code it describes.

- `npm run types` regenerates; `cfg.buildCmd` runs it before every sync build.
- **`frontend/types/` is committed**, so renaming a prop shows up as a contract
  diff in the same PR rather than silently desyncing the published API.
- `npm run typecheck` re-emits to a scratch dir and diffs — **CI fails if they
  drift**. That gate is what makes this trustworthy; confirm it still bites by
  editing a file under `frontend/types/` and re-running.
- `checkJs` is deliberately **off**. We emit declarations; we do not type-check
  the app. Turning that on is a separate and much larger project.
- Pin **`typescript@^5`**: `npm i typescript` now installs 7.x, whose Node API
  dropped `createSourceFile`, and the sync's own `.d.ts` parse gate then
  misreports itself as *"skipped — typescript not in node_modules"*.

**Two annotation rules, both learned the hard way:**

**Write prop sub-shapes INLINE, never as a named `@typedef`.** The converter fully
resolves types into the published contract but prints a type alias *by name* — so a
named typedef emits as a reference the published `.d.ts` never defines, and the
design agent sees an unresolvable type. That is worse than no contract, because it
looks authoritative. **`[DTS_PARSE]` does not catch it** (undefined names parse
fine); it was caught by reading the output. Four components hit this on the first
pass: `DataTable`, `Chart`, `ChartModal`, `BulkBar`.

**Per-prop doc comments are truncated at 120 characters** downstream, so lead with
the actionable half of a warning. (The old hand-written path passed bodies through
verbatim and did not truncate — this is a real constraint the derived path adds.)

Previews are graded from real screenshots (`ds-bundle/_screenshots/review/`), and
that is what catches a wrong contract — the first draft of `DataTable`'s
`config`/`rowKey` types rendered a blank table. Do not grade a card you have not
looked at. `[FONT_MISSING]` is expected and accepted: `--serif`/`--mono` are
system-font stacks with real fallbacks, and this app deliberately ships no
webfonts (it keeps the CSP's `script-src 'self'` untouched).

## Lint

```bash
.venv/bin/ruff check --config backend/pyproject.toml backend/app backend/tests scripts   # backend lint + import order (matches CI scope)
cd frontend && npm run lint             # ESLint — real-defect rules only
```

**No tool formats the frontend, and that is deliberate.** Prettier used to be
installed but nothing ever ran it: no CI job, no gate step, and `format:check`
had never passed — it disagreed with 144 of the 169 files in its own globs,
because this codebase keeps a compact hand-written style (several short
statements to a line, one-line `try`/`catch`) that Prettier expands. Adopting it
meant reformatting 85% of the frontend; it was dropped instead. Match the style
of the file you are editing.

## Dependencies

Two lockfiles, and **nothing installs the loose files**: CI and the Dockerfile
install `backend/requirements.lock`, and `npm ci` installs
`frontend/package-lock.json`. So a version you *declared* is not necessarily the
version anything *ran*.

```bash
# Backend — regenerate the lock in the SAME PR that moves a floor in requirements.txt
pip-compile --generate-hashes --output-file=backend/requirements.lock backend/requirements.txt

# Frontend — check advisories before AND after any lockfile change
cd frontend && npm audit
```

Five things that have each caused a real defect:

- **A raised floor with a stale lock is invisible.** Dependabot cannot run
  `pip-compile`, so it bumps `requirements.txt` alone and every check goes green
  having exercised the version that did *not* change.
  `backend/tests/test_requirements_lock.py` now fails on that, in both
  directions.
- **`npm audit` is run by no CI job.** A vulnerable transitive is invisible to a
  green suite, and dependabot titles do not say "security". Of three
  routine-looking npm PRs folded into #276, two cleared advisories (one HIGH, one
  MODERATE) and a third HIGH was present that none of them referenced. The
  frontend audits at **zero** today — a useful baseline only if it is checked.
- **`npm install` will not move a transitive that already satisfies its parent's
  range.** One package stayed on its vulnerable version through a full
  `npm install --package-lock-only`; it needed `npm update <pkg>`. The resulting
  lockfile looks the same either way.
- **`frontend/package.json` has one `overrides` entry, and it is load-bearing.**
  `eslint-plugin-react` has not published since April 2025 and still peers at
  `eslint@^9.7`, so `npm ci` fails outright on ESLint 10 with `ERESOLVE`. The
  override (`"eslint-plugin-react": { "eslint": "$eslint" }`) accepts our ESLint
  instead. The plugin itself works — its `lib/util/eslint.js` falls back to the
  `sourceCode.*` APIs — with one exception, which `eslint.config.js` documents:
  React version auto-detection calls the removed `context.getFilename()`, so the
  version is pinned rather than `"detect"`. Drop both the moment the plugin
  ships a release that peers at ESLint 10.
- **`ci.yml`'s Playwright container tag must move with `@playwright/test`.**
  A mismatched pair fails at browser launch — except across a patch bump, which
  shares a browser build and passes, hiding the drift until a bump that does not.

Because `main` is `strict: true`, each merge puts every other PR behind and
forces a fresh run. When several dependabot PRs rewrite the **same** lockfile,
combine them into one PR rather than merging serially.

## CI & the contribution workflow

`.github/workflows/ci.yml` runs on every PR and push to `main`, with eight jobs:
**lint** (ruff + ESLint), **secrets** (gitleaks over full history), **sast**
(semgrep — `p/python` · `p/security-audit` · `p/javascript` plus repo-local
`.semgrep/` rules), **deps** (pip-audit over `backend/requirements.lock`),
**backend** (all the `backend/tests/test_*` suites against a
fixture DB), **unit** (vitest — the fast pure-logic tier, with the JS coverage
floor), **e2e** (Playwright, network‑mocked), and **image** (builds the Docker
image, boots it, and curls `/api/health` as a smoke test). A separate **CodeQL**
workflow (`codeql.yml`, `security-extended`) runs the cross-file taint analysis
that semgrep OSS can't. `nl2sql-eval.yml` is `workflow_dispatch`‑only (it needs an
API key + the real DB).

Every PR and every `main` push **builds and smoke-tests** the image (so a broken
build or a boot failure can't merge), but publishing to GHCR happens **only on a
`v*` release tag**, which pushes `:X.Y.Z` + `:X.Y` + `:latest` (the leading `v` is
stripped). No rolling `:edge`/`:sha-<short>` images are published — release tags
are the only artifacts, so self-hosters pin a version or track `:latest` (see the
README's **Self-hosting** section). Cut a release with an annotated `git tag vX.Y.Z`
+ `git push --no-verify <remote> vX.Y.Z` (the tag sits on an already-merged, already
green commit, so the pre-push hook's full local gate is redundant), then
`gh release create`.

Workflow:

1. Branch off `main` (`feat/…`, `fix/…`, `chore/…`, `docs/…`).
2. Keep PRs focused; don't split a single file across PRs.
3. Add or update tests for behavior changes — the **test‑engineer** agent owns
   test files (see below); new behavior is written test‑first where practical.
4. Open a PR; watch CI **in the background** (`gh pr checks <n> --watch`, so you
   keep working) and merge only when secrets · lint · unit · backend · e2e · image
   are green.
5. End commit messages with the `Co-Authored-By:` trailer.

## The agent team

`.claude/agents/` defines a set of specialist [Claude Code](https://claude.com/claude-code)
subagents used to build and review this project: a **project‑manager**
orchestrator plus **architect**, **implementer**, **test‑engineer** (the only
one that writes tests), **code‑reviewer**, **security‑reviewer**,
**a11y‑reviewer**, **ui‑ux**, and **debugger**. They encode the conventions
above; read their `.md` files for the rubrics each applies.

**Use them selectively.** The routing test is design uncertainty or large blast
radius, not "touches multiple files" — `CLAUDE.md` → *Choosing the path* is the
governing statement. The chain's overhead (stalls, dropped inter‑agent messages,
ceremony over trivia like a singular/plural string) costs more than the
specialization saves on small work, so a well‑specified change goes inline with a
review pass at the end, and follow‑on fixes to a shipped feature default to
inline. The test‑engineer‑owns‑tests rule is team‑path only; inline, whoever
writes the code writes its tests.

**Keep them current.** A major architecture or infrastructure change — a new test
tier, a new gate, a removed/renamed feature, a changed workflow rule — must sweep
`.claude/agents/` in the same PR (or an immediate follow‑up). The definitions
reference the tiers, features, and rules and go stale silently otherwise.

## Working with the database

`ipeds.db` is built from the Access files in `data/` and is **rebuildable** (so
it's gitignored). `app.db` holds the irreplaceable state and is backed up
separately (`scripts/backup_app_db.py` — see the README's **Self-hosting** section).

```bash
python3 scripts/build_ipeds_db.py             # build ipeds.db from data/*.accdb
python3 scripts/build_ipeds_db.py --dry-run   # just print the table → family map
```

Each physical Access table (e.g. `C2024_A`, `HD2024`) is grouped into a
**family** by stripping the year, and all years are stacked into one table with
`survey_year`, `year` (ending year — use for sorting/filtering), and `src_table`
provenance columns. Metadata lives alongside the data: `valuesets` (code →
label), `vartable` (data dictionary), `tables` (catalog), plus convenience views
like `institutions_current` and `_years`. **[SCHEMA.md](docs/SCHEMA.md) is the full
reference** — read it before writing queries or touching the loader.

Two rules that will bite you if ignored (both detailed in SCHEMA.md):

- **"Recent N years" is a constant bound**, never a join:
  `WHERE year > (SELECT MAX(year)-N FROM _years)`. A join to a distinct‑year
  subquery makes SQLite full‑scan the 8M‑row `c_a` and effectively hang.
- **Never mix CIP / award‑level aggregation levels in one `SUM`.** In `c_a`,
  `cipcode` exists at 2‑/4‑/6‑digit plus a `'99'` grand‑total row that each sum
  to the same total — match an exact 6‑digit code, or use `'99'` for totals.

**A fresh deploy with no `ipeds.db` yet is a supported first-run state**, not an
error: `backend/app/tools/sql.py`'s `ipeds_years()`/`has_ipeds_data()` probe the file
non-raisingly (missing/0-byte/garbage/no-`_years` all yield `[]`/`False`).
`GET /api/auth/me` exposes `has_data`; the chat-stream no-data guard in
`backend/app/routers/chat.py` returns a friendly notice (admin-aware wording, no
conversation created, no agent run) instead of a raw SQL error; and the SPA
routes an admin with no data straight to Admin → Imports on load — a one-shot
`navigate("/admin/imports", { replace: true })` that fires only when the admin
LANDED on bare `/` (a deep link to `/chat/:id` or another `/admin/:tab` is
never yanked), and never re-fires on a later `refreshMe()` once the import
completes.

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
pipeline, just for one file instead of a union. The streamed upload-dir copy
is likewise deleted afterward, success or failure (mirroring the NCES work
dir above). What survives is the `data/` copy the loader actually builds
from — on success it stays as the permanent source for every future rebuild,
and on failure it is reverted to whatever was there before.

**`backend/app/nces.py`** is the fetch layer: every URL it requests is built ONLY from
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
`backend/app/routers/admin.py` so they can't drift apart.

**Disk-headroom preflight (`backend/app/estimate.py`).** Before `run_integrate` fetches
anything, it estimates the run's peak disk footprint (download + extracted
`.accdb` + rebuilt staging DB, for the **whole union** being rebuilt — not just
the newly-picked years) via the pure `estimate.estimate_integrate()` function,
pads it by `NCES_DISK_SAFETY_FACTOR`, and refuses the job (failing it with a
`"Not enough disk: need ~X, have ~Y free"` message, before touching the
network or the live db) if `shutil.disk_usage` on the `ipeds.db` volume can't
cover it. The same estimator (mirrored, key-for-key in camelCase, by
`frontend/src/estimate.js` — cross-language agreement is asserted by the vitest unit
test `frontend/src/estimate.test.js` against the shared fixture
`backend/tests/fixtures/estimate_cases.json`) drives a live **disk meter** on the
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
`NCES_DOWNLOAD_CONCURRENCY`. `backend/tests/test_nces.py` exercises the fetch layer
entirely against `httpx.MockTransport` (no socket, no real NCES);
`backend/tests/test_importer.py` and `backend/tests/test_admin_router.py` monkeypatch
`nces.fetch_year` / `nces.probe_catalog` / `importer._years` /
`importer.shutil.disk_usage` / `admin.shutil.disk_usage` as bare module
attributes (never `from ... import`) so tests can substitute fakes without
touching the real network, filesystem, or loader.

### Removing an integrated year (the trashcan)

Each already-integrated (or "update") year card on **Admin → Imports** shows a
`.year-remove` trashcan; clicking it (after the `useConfirm()` confirmation modal)
calls `DELETE /api/admin/import/year/{start_year}`, which — after the same single-flight
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
