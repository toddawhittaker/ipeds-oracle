# IPEDS project

A **private FastAPI + React web app** that answers natural-language questions
about U.S. colleges/universities (IPEDS = the U.S. Dept. of Education's census of
postsecondary institutions) for an institution's approved colleagues. A
DeepSeek-backed agent turns each question into SQL against the read-only IPEDS
dataset (`ipeds.db`) and streams back an answer. The app is the work;
`CONTRIBUTING.md` (dev handbook) and `docs/DEPLOY.md` (deploy) are the deeper guides.

## Layout
- `ipeds.db` — **the** dataset: SQLite, every IPEDS survey table stacked across
  **whichever collection years the deployment loaded** (each institution picks its
  own via Admin → Imports; `SELECT year FROM _years` is the authoritative list —
  never assume a range). Opened **read-only** by the app.
- `docs/SCHEMA.md` — **read before writing any query.** Data model, conventions,
  family catalog, code references, query patterns, worked NL→SQL examples.
- `backend/app/` — FastAPI backend. `frontend/` — Vite/React SPA. `backend/tests/` — test suites.
- `app.db` — app state (users, sessions, conversations, usage). `logs.db` —
  persistent server logs. Both separate from the read-only `ipeds.db`.
- `scripts/build_ipeds_db.py` — repeatable loader that builds `ipeds.db` from the
  `data/*.accdb` files (`--dry-run` prints the table→family mapping).
- `CONTRIBUTING.md` — **dev handbook** (stack, local run, tests, lint, CI, agent
  team). `docs/` — `SCHEMA.md` (data model + query guide) and `DEPLOY.md` (VPS/Docker deploy).
- `brand/` — logo source masters (icon + wordmark) and the ImageMagick commands
  that regenerate the web favicons + `frontend/src/assets/wordmark*.png` from them.

## The dataset (`ipeds.db`)

The app's agent queries `ipeds.db`; you'll also query it directly — to verify an
aggregation, derive an eval's expected answer, or debug the agent's SQL.

- **`docs/SCHEMA.md` is authoritative — read it before writing or verifying any query.**
  It's injected into every agent prompt. The DB is self-describing: use its
  *Discovery* queries (§3: `tables`, `vartable`, `valuesets`) to look up any
  table/variable/code rather than guessing.
- Inspect it with `sqlite3 -header -column ipeds.db "…"`, and **sanity-check
  magnitudes** against reality (~1M associate's/yr nationally) — a number 2–4× off
  usually means an aggregation-level mistake.

### Critical query gotchas (details in `docs/SCHEMA.md`)
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
- **Backend** — FastAPI (`backend/app/`: `config`, `db`, `auth`, `security`, `mailer`,
  `llm`, `prompt`, `guard`, `critic`, `skills`, `seeds`, `importer`, `nces`,
  `logbuffer`, `ratelimit`, `tools/*`, `routers/*`).
- **Frontend** — a Vite/React SPA (`frontend/`) with SSE-streamed chat, **client-side
  routed** (react-router-dom): `/`, `/chat/:id`, `/admin` → `/admin/users/current`,
  `/admin/:tab`, `/admin/:tab/:sub`, `/verify`, catch-all → `/`. FastAPI's SPA
  catch-all serves `index.html` for all of them, so a hard refresh / deep link
  never 404s. **Admin → Users is a tabbed section** (`Allowlist` in `Admin.jsx`):
  three path sub-tabs — **Current users** (default) / **Pending requests** /
  **Blocked users** — at `/admin/users/<sub>`; bare `/admin/users` or an invalid
  sub redirects to the remembered-or-`current` tab (session memory in
  `sessionStorage`), and legacy `/admin/pending`·`/admin/blocked`·`/admin/allowlist`
  aliases redirect into the matching sub-tab (`AdminRoute`). It's a real ARIA
  tablist (`role=tablist/tab/tabpanel`, roving tabindex, ←/→/Home/End with
  automatic activation) with a per-tab **count badge** reflecting *all* records in
  that category (never the filtered view); the Pending badge gets an accent
  **"attention"** tone only while requests await — never an error tone
  (`usertabs.js`, vitest-pinned; `pendingBadgeTone`). All three DataTables stay
  **mounted** with inactive panels `hidden`, so each table's own search/sort/page
  state *and* its lifted selection **survive a tab switch**, resetting only when
  the admin leaves the Users section — the spec's persistence contract, with no
  new state plumbing. Pinned in `frontend/e2e/admin-users-tabs.spec.js`.
  Chat interaction contracts (all Playwright-pinned in
  `frontend/e2e/chat-interactions.spec.js`): **Stop generating is
  abandon-and-drain, never a network abort** — it bumps the existing
  `turnToken` so the view detaches while the request drains and the server
  still persists the answer (an aborted mid-turn request is the known
  server-side data-loss path — see the open chat.py pre-`gen()` backlog item;
  don't "optimize" Stop into an AbortController until that's fixed).
  Auto-scroll **follows only while the viewer is near the bottom** (scrolled
  up = never yanked; a "Jump to latest" pill is the way back). Conversation
  switches show a skeleton, never the empty-state prompt. A printable key
  typed with nothing editable focused redirects into the composer
  (`typeahead.js`, vitest-pinned). Conversations can be **renamed inline**
  (`PATCH /api/chat/conversations/{id}` — metadata-only by contract: it must
  never touch `updated_at`, or renaming an old chat would reorder the
  recency-sorted sidebar). An answer's **Thinking / SQL traces are
  mutually-exclusive disclosure toggles** whose panel opens **full-width below**
  the actions row (never as an inline `<details>` inside the flex row, which
  widened its own cell and shoved the copy buttons around); opening one closes
  the other. The **Thinking trace is persisted** (migration 12,
  `messages.thinking` — a JSON list of `{kind,text}` items built server-side in
  `chat.py`'s stream loop via `_trace_item`, mirroring the frontend's live
  `addThought` 1:1) so it **survives a reload/reopen just like `sql_log`**, not
  only the live in-session turn. **All SQL anywhere in the UI** renders through
  `SqlBlock.jsx` (the chat Thinking trace + SQL dropdown, the Admin → Skills
  worked example, and any ```sql fence in an answer) — pretty-printed with
  `sql-formatter` (a one-line query becomes a readable indented block, wrapping
  instead of scrolling; `format={false}` highlights-only for author-written
  fences) and syntax-highlighted with `react-syntax-highlighter` (`PrismLight`,
  SQL grammar only) run with `useInlineStyles={false}` so it emits Prism token
  **class names** that `styles.css` colors per light/dark theme — no inline
  styles, so it needs no CSP `style-src` exception of its own. SQL **inside the
  Thinking trace** is height-capped to a ~9–10 line scroll window (`.thought-sql`
  needs `flex:none` or the flex-column trace squishes a tall query to one line —
  the recurring "single line SQL" bug); the standalone **SQL dropdown stays fully
  expanded** (the user's deliberate "show me the whole query" view).
- **Three SQLite DBs, all separate:** `ipeds.db` (read-only query target — the
  dataset above), `app.db` (state, with a `PRAGMA user_version` migration runner),
  `logs.db` (persistent admin logs).

### The agent loop
LLM = **DeepSeek** via any OpenAI-compatible provider (`LLM_BASE_URL`, **OpenRouter**
by default, through the shared `backend/app/llmhttp.py` transport; `v4-flash` default →
escalate to `v4-pro`), run as a tool-calling agent loop wrapped in three guards:
- a topical **guardrail** in front (off-topic questions never reach the DB);
- a deterministic SQL **linter** (`backend/app/tools/sqllint.py`) — a pre-flight check that
  flags IPEDS aggregation foot-guns (CIP-rollup / second-major double counts,
  DISTINCT-year full-scans) in the model's SQL and feeds the warning back so the
  agent self-corrects;
- a post-answer **critic** that can force one revision round. The revision only
  ships if the model **re-queried AND changed the answer AND its prose carries no
  reviewer-directed meta** (`_leaks_review_meta` in `llm.py` matches
  "reviewer"/"the review"); otherwise the clean pre-critique draft is re-emitted,
  `critic_revised=False`. This closes the observed leak where a *confirm*-by-
  requery rebuttal (same number, new "the reviewer's concern…" prose) slipped
  past the requeried-and-changed gate — see `backend/tests/test_critic.py`.
- The **signature "figure"** — a typeset hero statistic (mono caption · big serif
  number · ochre rule · mono source) rendered ABOVE an answer when one clear number
  answers the question. Prompt INSTRUCTIONS **step 6** turns a single-number answer
  into a short **BRIEF**: (a) the ```figure fence, (b) a 1–2 sentence synopsis, (c) a
  recent-years breakdown table (constant-bound `year > (SELECT MAX(year)-5 …)`), and
  (d) a ```chart trend — reusing the existing table + chart rendering, so the reader
  gets the story behind the number, not just one point. Omitted for
  rankings/lists/multi-row comparisons/trends with no single hero number. The model emits a
  ```figure `{value,unit?,label,source?}` fence; **`llm.py`'s `_extract_figure`
  parses it out server-side, ALWAYS strips every figure fence from the prose (so raw
  JSON never reaches the user, even on a parse error), and — only for valid JSON with
  value+label — sets `AgentResult.figure` and yields a `{"type":"figure",…}` SSE
  event**. Parsed AFTER the critic's revert settles `answer`, so the figure always
  matches the winning prose. Persisted in `messages.figure` (migration 13) and the
  answer cache `query_cache.figure` (migration 14) so it survives reload AND a
  cache-hit repeat — mirroring `sql_log`/`thinking`. Frontend: a structured `figure`
  message field (not scraped) → `Figure.jsx` (pure `figure.js` normalizer, vitest)
  renders it as a sibling BEFORE `<Markdown>` in the assistant bubble — above the
  prose and OUTSIDE the `.md` copy surface — reusing the Reading-Room `.figure`/
  `.fig-rule`/`.field-label` device (the same primitive the Login "door" uses).
  (`_extract_figure` accepts BOTH the ```figure fence AND an HTML `<figure>` tag —
  some models emit the latter.) The brief applies on **follow-up turns too** (never
  code-gated; a prompt line makes the model reliably do it). A single-number brief's
  **table + trend chart render side by side** (`briefdata.js` pairs one-table +
  one-chart → `Markdown.jsx` passes the chart into the table component and suppresses
  the standalone fence; drops the redundant "Chart this"; `.brief-figrow` wraps to
  stacked when narrow).
- **The analyst layer** on top of the brief:
  - **Trend line + %-change** — `Chart.jsx` overlays a least-squares fit (a computed
    `__trend` `<Line>`, dashed ochre, injected into `chartChildren()` so it flows to
    the PNG export too; kept out of `keys` → no label/legend) and a **delta badge**
    (`▲/▼ X%` over the range, `--ok`/`--danger`) for a single-series line time-series.
    All client-side from the numeric chart data (`trendstats.js`, vitest) — accurate,
    no model dependency; a "Trend" toggle (default on).
  - **Richer narrative + rank/share** — prompt step 6(b): direction/magnitude,
    peak/trough years, provisional-year flags, and (when meaningful) the figure's rank
    among peers or share of a national total (the model runs one extra query).
  - **"You might also ask" drill-down chips** — the model MAY emit a ```followups
    fence (step 7, a JSON array); `_extract_suggestions` parses+strips it (mirrors
    the figure) → `{"type":"suggestions",…}` event → `messages.suggestions` (migration
    15) + `query_cache.suggestions` (16). `Suggestions.jsx` (pure `suggestions.js`,
    vitest) renders chips below the actions row; clicking one `submit()`s it as a
    follow-up turn (which gets its own brief) — an exploration loop.

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
- **Approval mints no token.** Only a user's OWN `POST /api/auth/request` mints +
  emails a real one-time sign-in link (`auth.py` `mint_login_link` + `send_magic_link`).
  Admin **approve / manual-add / CSV-import** just add the allowlist row and email a
  **"you're approved — request your sign-in link"** notice (`send_access_approved`,
  no link; the button points at the login page). This keeps a `login_tokens` write
  out of the approval transaction, and — combined with the send happening only after
  commit+close — is why `_approve_allowlist` no longer carries a minted link out to
  the mailer. CSV-import sends its notices via `BackgroundTasks` (a roster can be
  hundreds). The admin toast still classifies delivery
  (`emailed`/`failed`/`logged_to_console`/`already_allowlisted`). The **admin
  access-request notification** (`send_access_request`) deep-links straight to
  `/admin/users/pending` and carries no "Reason" line (nothing ever set one). All
  three emails share one **Outlook-safe HTML shell** in `mailer.py` (`_email_document`
  + a VML bulletproof `_button`: doctype/head, 600px `role=presentation` tables,
  Arial not `system-ui`) in the app's teal palette — `mailer.py` is E501-exempt in
  `pyproject.toml` because the templates are legitimately long.
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
  400×+ timing oracle). A residual sub-ms DB-local timing difference (denied/unknown
  skip the INSERT the allowlisted/pending branches do) is **accepted** — it doesn't
  isolate the sensitive states, and equalizing it would violate "store nothing on
  deny"; see `auth.request_login`'s docstring.
- **Per-IP rate-limit is spoof-resistant:** `POST /api/auth/request` is capped
  per-email and per-IP (`ratelimit.py`), but `X-Forwarded-For` is client-settable.
  `client_ip` trusts it only `TRUSTED_PROXY_COUNT` hops **from the right** (Caddy
  appends the real peer); `0` (dev/CI default) ignores XFF and uses the socket peer.
  Set it to **`1`** in production behind the single Caddy hop (pinned in
  `compose.yaml`); combine with `EMAIL_DOMAIN` to close the access-request-spam surface.
- **CSRF defense in depth:** the session cookie is `HttpOnly`+`Secure`+`SameSite=Lax`;
  on top of that a pure-ASGI `CSRFMiddleware` (`csrf.py`) refuses any state-changing
  request whose `Origin` matches neither the request `Host` nor `APP_PUBLIC_URL`.
  Origin-less/non-browser requests pass (SameSite still covers browsers); it's raw
  ASGI so it never buffers the chat SSE stream. In the **dev posture only** (insecure
  cookies) it also accepts loopback origins so the Vite dev-proxy (`changeOrigin`)
  works — production (Secure cookies) enforces strict same-origin.
- **Security headers on every response:** a pure-ASGI `SecurityHeadersMiddleware`
  (`secheaders.py`, outermost so it stamps even the CSRF 403) sets a restrictive
  **CSP** (`script-src 'self'`, no `unsafe-inline`/`unsafe-eval`; `img-src 'self'
  data:` for chart export; `frame-ancestors 'none'`), plus `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`. The CSP is the
  **second line of defense** under the LLM-markdown render surface — that surface is
  safe today only because react-markdown emits no raw HTML (**no `rehype-raw`, default
  URL sanitizer intact — keep it that way**; a DOM-XSS review confirmed it clean).
- A denied row records **both** `created_at` (when the request was filed →
  "Requested") **and** `denied_at` (when it was rejected → "Denied", added in
  migration 11) — kept separate so the admin Blocked-users table shows each; a
  pre-migration denial has a NULL `denied_at` (rendered "—").
- A denial is **reversible**. The Allowlist tab lists every active block (the
  "Blocked users" table, grouped **canonically** since a block spans `+tag`
  variants — deliberately unlike the pending list above it, grouped by the **raw**
  address since Approve is exact). Its undo control
  (`DELETE /api/admin/access-requests/{email}/denial`) DELETEs the denied rows
  outright, returning the address to a genuine *never-requested* state — **grants
  no access, sends no email**. **Allowlisting** a denied address also clears the
  block (its `denied` rows convert to `approved`, canonically, so offboarding a
  variant later can't resurrect it), but is the stronger action: it grants full
  access **and** emails a welcome link — not always what undoing a mistaken denial
  calls for.
- **Bulk row-selection + actions** on all three Allowlist tables (Users,
  Pending requests, Blocked users): checkbox column + tri-state page-header
  checkbox + "select all matching" (client-side only — every list is fetched
  unpaginated, so there's nothing to select on an unloaded page). Three
  endpoints — `POST /api/admin/allowlist/bulk-action` (promote/demote/delete),
  `POST /api/admin/access-requests/bulk` (approve/reject),
  `POST /api/admin/access-requests/denial/bulk` (unblock) — each
  transactional (one connection, one commit), capped at `BULK_MAX_ITEMS`
  (1000) records, and **recomputing eligibility per record** server-side
  (never trusting the browser's stale list); a demote/delete batch that
  includes the caller's own email 400s the *whole* batch before any write. An
  id posted to the wrong endpoint (e.g. a denied row's id sent to the
  pending-only bulk endpoint) is recognized as no-longer-eligible and
  skipped, never mutated — the cross-table safety net. Every mutation goes
  through the same helpers the single-row endpoints already called
  (`_set_admin`, `_remove_user`, `_approve_allowlist`, `_deny_group`,
  `_clear_denial_group`), so the single- and bulk-paths can never drift.
  After an action commits the UI **keeps the whole selection** (rows still in
  the table stay checked — `selection.js`'s `retainedSelectionAfterBulk`):
  promote/demote leave every acted row in place so nothing unchecks;
  delete/approve/reject/unblock drop only the ids the server actually processed
  (those rows are gone) while keeping any it skipped/failed, and freeze an
  "all matching" selection to concrete ids so a later-polled row isn't
  silently pre-selected.
  Frontend: `selection.js` (pure counting/copy logic — tri-state derivation,
  eligibility partitioning, every confirm/toast string — vitest-covered),
  `useTableSelection.js` (the per-table selection-state hook; `Allowlist`
  holds three independent instances so selecting on one table never touches
  another), `BulkBar.jsx` (the **contextual** action toolbar rendered through
  `DataTable`'s opt-in `selectable`/`renderSelectionBar` props — following the
  standard Gmail/Linear pattern it appears **only while ≥1 row is selected**
  (never a persistent strip of disabled buttons), anchors a live "N selected"
  count + Clear on the left, shows **stable-verb** action buttons on the right
  (the count lives in the confirm dialog, not the label) with any **destructive**
  action split off past a divider in the `--danger` color, and carries the
  "select all N matching" banner once a full page is selected across more than
  one page; every existing `DataTable` usage that doesn't pass `selectable`
  renders unchanged).

### Admin → Imports (dataset management)
- A live **NCES year catalog**: `backend/app/nces.py` probes `nces.ed.gov` (**SSRF-hardened**
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
A sentinel test in `backend/tests/test_admin_router.py` pins this.

**Full details live in `CONTRIBUTING.md` and `docs/DEPLOY.md` — read them, don't guess.**

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
**Every `backend/app/` module stays ≥ 80%** line coverage (per-module, not just the
total) — enforced by `scripts/coverage_check.sh` in CI and the pre-push gate —
but that floor is met with tests that **guard real behavior**, never padded with
assertions on constants. Tests are dependency-light scripts in `backend/tests/`
(`sys.exit(1)` on failure, no API key needed). New low-coverage code is not
"done" until it's tested.

**Test pyramid — pick the lowest tier that actually catches the regression.**
*Pure logic* — functions and leaf modules with real input→output behavior — is
unit-tested with **vitest** (`frontend/`, jsdom, no browser; co-located
`frontend/src/*.test.js`, table-driven). *Genuine browser truth* —
routing/navigation, focus management, aria-live/AT announcements, back/forward,
SSE-driven DOM — stays in **Playwright** (`frontend/e2e/`). jsdom's focus and history
models are **not** the browser's, so component tests that lean on routing,
portals, or focus belong in Playwright, not vitest. Don't boot a browser to
check a pure function; don't unit-test a navigation truth jsdom will fake and
get wrong. When a pure function is currently pinned through an e2e assertion,
**move it down** to vitest and thin the now-redundant e2e logic check — keep the
browser *flow* (focus, the aria-live announcement firing) around it. **JS
coverage is gated:** `frontend/vitest.config.js` enforces a per-file ≥80% line floor
over an explicit **allowlist** of the pure-logic modules under test — the JS
analogue of `coverage_check.sh`'s per-`backend/app/`-module rule. Add a module to that
list when (and only when) it gets real unit tests, so JS logic never silently
escapes a gate. Browser-tested components (`Chat.jsx`, `Admin.jsx`, …) are
deliberately not in the floor — Playwright covers them.

**Run the full gate before pushing.** `scripts/run_ci_local.sh` reproduces all of
CI (ruff over `backend/app backend/tests scripts` + ESLint; the `frontend/` **vitest** unit tests; the
`backend/tests/` backend suites against a fixture DB; Playwright e2e). A
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
the VPS runs `scripts/deploy.sh <tag>` (no inbound SSH). Details in `docs/DEPLOY.md`.

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
`CONTRIBUTING.md`/`docs/DEPLOY.md`) in the *same* PR. **A major architecture or
infrastructure change — a new test tier, a new gate, a removed/renamed feature, a
changed workflow rule — must also trigger a sweep of `.claude/agents/`.** The
specialist definitions reference the tiers, features, and rules and go stale
silently (the vitest tier landed in #71 while the team still described the removed
👍/👎 feedback until the #72 sweep). Fold the sweep into the same PR when small,
else ship it as an immediate focused follow-up. These files must always reflect
the current state of the project.
