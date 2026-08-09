# IPEDS project

A **private FastAPI + React web app** that answers natural-language questions
about U.S. colleges/universities (IPEDS = the U.S. Dept. of Education's census of
postsecondary institutions) for an institution's approved colleagues. An
LLM-backed agent turns each question into SQL against the read-only IPEDS
dataset (`ipeds.db`) and streams back an answer. The app is the work;
`CONTRIBUTING.md` (dev handbook) and the README's **Self-hosting** section are the deeper guides.

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
  `data/*.accdb` files (`--dry-run` prints the table→family mapping). It **checks
  `mdb-export`'s exit status** and aborts: a failed extraction used to be
  indistinguishable from an empty table, so a whole survey family could load ZERO
  rows with the build still reporting success — and on a FIRST build there is no
  prior dataset for `integrity_checks`' shrink detector to catch it against. An
  ABANDONED stream is exempt (`header_only` probes a table's shape and breaks,
  killing `mdb-export` with SIGPIPE); only a fully-drained one is judged.
- `CONTRIBUTING.md` — **dev handbook** (stack, local run, tests, lint, CI, agent
  team). `docs/` — `SCHEMA.md` (data model + query guide). Self-hosting lives in the README.
- `brand/` — the **IPEDS Oracle** identity: `icon.svg` (the Column mark — vector
  master) + the ImageMagick recipe that regenerates the favicons from it. The
  header/login **wordmark is inline SVG** (`frontend/src/Wordmark.jsx`, drawn from
  the theme tokens so light/dark comes from one source — mono "IPEDS" · ochre rule ·
  serif "Oracle" · Column), NOT a PNG pair. Product name = `PRODUCT_NAME` in
  `config.py` (feeds the API title + every email); the wordmark's accessible name is
  "IPEDS Oracle".
- `.design-sync/` — inputs for the **claude.ai/design** sync (`/design-sync`), which
  publishes the UI as a design system so Claude Design builds screens out of the real
  components. Committed: `config.json`, `conventions.md` (prepended to the generated
  README → the design agent's system prompt), `previews/*.tsx`, `groups/*.md`,
  `NOTES.md`. **Read `NOTES.md` before re-running.** The non-standard bit: there is
  **no library build**, so `frontend/ds-entry.js` must name every export (the
  converter's `export * from …` fallback drops `default` exports — that once left 18
  components out of the bundle while still emitting their cards). `frontend/ds-*.js`
  are sync-only — the app is still built from `index.html`/`main.jsx`.
- **Prop contracts are DERIVED, never hand-written.** The repo has no TypeScript, so
  props are declared as **JSDoc on the components** and `tsc --emitDeclarationOnly`
  (`frontend/tsconfig.json`, `npm run types`) emits **`frontend/types/` — committed**,
  so a prop rename shows up as a contract diff in the same PR. `npm run typecheck`
  re-emits and diffs; CI fails if they drift. Two rules when annotating: write prop
  sub-shapes **INLINE, never as a named `@typedef`** — the converter resolves types
  into the published contract and prints an alias *by name*, so a named typedef emits
  as a reference the published `.d.ts` never defines (the parse gate does NOT catch
  it); and per-prop docs are truncated at **120 chars** downstream, so lead with the
  actionable half of a warning. Details in `CONTRIBUTING.md` → *Design system sync*.

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
  routed** (react-router): `/`, `/chat/:id`, `/admin` → `/admin/users/current`,
  `/admin/:tab`, `/admin/:tab/:sub`, `/verify`, catch-all → `/`. FastAPI's SPA
  catch-all serves `index.html` for all of them, so a hard refresh / deep link
  never 404s. **`Admin.jsx` is a ~110-line SHELL** — route params (`AdminRoute`),
  `ADMIN_TABS`, the alias/redirect rules, and the tab chrome. The five pages live
  in **`src/admin/`** (`Allowlist` · `Imports` · `Usage` · `Skills` · `Logs`),
  props-only, plus the pure `admin/format.js` (`humanBytes`/`humanSeconds`/
  `canonEmailForDisplay`/`fmtDateTime`/`fmtApprovalDate`/`money`/`ruleName`,
  vitest-pinned — they were unreachable by the fast tier while trapped in a
  component file). Sub-tab session memory (`rememberedSubTab`/`rememberSubTab`)
  lives in `usertabs.js` next to `resolveSubTab`, NOT in the shell: `Allowlist`
  writes it too, so keeping it in `Admin.jsx` would make a child import from its
  own parent — a module cycle for two lines of `sessionStorage`.
  **Adding a subdirectory under `src/` required fixing the vitest coverage
  derivation**: `readdirSync("src")` is NOT recursive, so `src/admin/format.test.js`
  was *collected* by the `src/**` include glob yet its module never entered
  `coverage.include` — running fully outside the 80% floor while looking tested.
  Proven both ways: a 38%-covered `format.js` exits 1 with a named error under the
  recursive walk, and exits 0 with zero mentions under the old one. Same drift
  shape #207 killed, re-entering through a directory.
  **Admin → Users is a tabbed section** (`Allowlist` in `src/admin/Allowlist.jsx`):
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
  **Every timestamp cell in these tables is `cell-trunc` (nowrap + ellipsis) in a
  fixed-width column** — `.col-active` and `.col-when` are both **210px**,
  because all four render the same `fmtDateTime`. The nowrap is the load-bearing
  half: a stamp is one atomic value, and letting it wrap makes row height depend
  on font metrics and locale, which is why this first showed up only inside the
  Playwright container. Width alone is a race a wider locale eventually wins; it
  exists so the ellipsis never shows in the ordinary case. Both halves are
  needed and were mutation-verified — at the old 168px, nowrap alone traded a
  wrapped cell for an **ellipsised** one (measured: 182px of content). The tests
  pin the MECHANISM (computed `white-space`; `scrollWidth` vs `clientWidth`),
  **not row height** the way the Current-users test does: Blocked's Email cell
  wraps deliberately (`.blocked-email`), so its rows are legitimately multi-line.
  Costs the Blocked table 84px of minimum width, which is why Blocked is the
  widest of the three and sets the `min-width` floor in the reflow rule below.
  **Every admin table has a scroll region of its own** (WCAG 1.4.10 Reflow).
  `html, body { overflow: hidden }`, so the page cannot scroll sideways and the
  nearest scroller was the whole `.admin` column — at 320px the only way to
  reach an Actions button was to scroll the entire page in two directions,
  taking the heading and section nav with it. `DataTable.jsx` wraps its
  `<table>` in **`.table-scroll` (`overflow-x: auto`)** and `.grid.data` carries
  **`min-width: 720px`** — the floor Blocked actually needs (556px of fixed
  columns before its Email gets anything). `width: 100%` still wins above that,
  so every desktop geometry above is byte-identical at 1280. Admin → Usage's
  **Top users** is not a `DataTable` and sets no column widths, but an email
  address is one unbreakable token (measured 526px), so it gets the same wrapper
  and no `min-width`. Deliberately **NOT `tabIndex={0} role="region"`**: the
  `Markdown.jsx` precedent for that is justified by "its rows hold no focusable
  children", and these rows have a sort button in every header and action
  buttons in every row, so both extremes are already keyboard-reachable and
  focusing one scrolls it into view — a tab stop before each table would be
  noise, and a region sharing the table's `aria-label` announces the name twice.
  **This was only shippable once the Actions tooltip stopped hanging outside the
  table**: `.tip::after` is absolutely positioned and centred on its button, and
  an abspos descendant counts toward an ancestor's scrollable overflow, so the
  Users table reported `scrollWidth` 976 against a rendered 958 — an
  `overflow-x: auto` wrapper would have put a permanent 18px scrollbar under
  every admin table. The tip is now anchored to its button's **right** edge
  (left overflow does not enter `scrollWidth` in LTR). Note `src/DataTable.jsx`
  is used by exactly the three `Allowlist.jsx` tables — `Markdown.jsx` has a
  same-named LOCAL component that is a different thing entirely. Pinned in
  `frontend/e2e/admin-table-reflow.spec.js` + the Actions-column describe in
  `admin-users-tabs.spec.js`, both viewports load-bearing (at 320 the region
  must scroll; at 1280 it must not).
  The Current-users table's **"Last active"** column is **DERIVED read-side** in
  `admin.list_allowlist`, not stored: the latest of `users.last_login`, the
  user's newest `conversations.updated_at`, and their newest
  `usage_log.created_at`, as two pre-aggregated `LEFT JOIN`s (one scan each for
  the whole page, not a correlated subquery per row). No migration, no write on
  the request path, and it reads **retroactively** over history already in
  `app.db`. Two traps, both pinned in `test_admin_router.py`: SQLite's **scalar
  `max()` returns NULL if ANY argument is NULL**, so each arm is `COALESCE`d to
  0 first (otherwise a user who never signed in reports as never *active*,
  despite having conversations) — and the 0 is `NULLIF`d back out, or a
  never-active user renders as 1 Jan 1970 and sorts as the OLDEST activity
  instead of grouping with the nulls. `usage_log` is in the max even though
  `_persist` writes it in the same transaction as the `conversations` bump
  (normally redundant) because **deleting a conversation leaves the usage rows**,
  and without that arm a user who tidies their chat list reads as never active.
  Stated non-goal: it is **not a "last page hit"** — a sign-in-and-browse session
  shows only the sign-in, since nothing on that path writes. Tracking that needs
  a stamp written from `auth._user_from_request`, deliberately not done: the 30s
  admin attention poll alone would keep any open tab looking active, so the
  column would mean "had a tab open" for admins and "used the app" for everyone
  else. `userlist.js`'s comparator reads `last_active`, **never `last_login`**
  (they differ for anyone whose latest activity was a question), or the sort
  contradicts the date in the same cell — vitest-pinned.
  **Admin "attention" indicators** surface where work is waiting: a total badge
  on the top-bar **user-badge avatar** (live on every page, Chat included — see
  the shell paragraph below) and a
  per-area count on the Admin section nav — only the three areas with an
  actionable backlog: **Users** (pending access requests), **Skills** (unverified
  lessons), **Logs** (problems since this admin last viewed Logs);
  imports/usage never badge. One lightweight `GET /api/admin/attention` →
  `{users,skills,logs}` (keys = `ADMIN_TABS` names, so a section's count is just
  `counts[tab]`) is **fetched from the Shell** (`App.jsx`), not per-tab, so the
  avatar total works before you ever open Admin; it polls every 30s AND
  **re-fetches on tab focus/visibility** (a backgrounded tab throttles
  `setInterval`, so without this a change made while you're away wouldn't surface
  until a much-delayed tick — the "polling doesn't update, only a refresh does"
  bug). Badge text goes through the capped `formatBadge` (`attention.js`, vitest:
  `""` at 0, the number to 99, then `"99+"`), reusing the same accent
  `.usertab-badge`/`tab-badge` pill as the Pending sub-tab (a queue is work
  waiting, never a red failure — even log problems). The **Logs badge is
  acknowledgeable**: `POST /api/admin/logs/seen` advances a **per-admin**
  `admin_log_seen.seen_ts` (migration 17) so the badge clears when you open Logs
  (marked on mount AND unmount) and re-counts only later problems; the count is
  `logbuffer.count_problems(since)` (WARNING/ERROR/CRITICAL, `ts > seen_ts`) over
  the separate `logs.db`, via `get_handler()`. Approve/reject/verify and the
  Users-tab reload also ping `refreshAttention()` so a badge drops the instant you
  act, not on the next poll. Pinned in `frontend/e2e/admin-attention.spec.js` +
  `backend/tests/test_admin_router.py`.
  **The top bar holds exactly two things**: the **wordmark** (a `<Link to="/">`,
  the way home) on the left and a **user-badge menu** (`UserMenu.jsx`) on the
  right — nothing else. The badge is a round **avatar** showing initials derived
  from the signed-in email (`initials.js`, pure/vitest: `first.last@…`→`"TW"`, a
  `+tag` is stripped, else the first letter). It's a real **menu button**
  (`aria-haspopup="menu"`, `role=menu`/`menuitem`, ↑/↓/Home/End roving,
  Escape-closes-and-restores-focus, click-outside) whose items are **Admin** (only
  when `is_admin` — `navigate("/admin")`, carrying the attention count badge),
  **About**, the **light/dark toggle** (inline `IconSun`/`IconMoon` from
  `icons.jsx`, replacing the old ☀️/🌙 emoji; flips `data-theme` on `<html>` +
  `localStorage`, and is the one item that keeps the menu **open** on activation),
  and **Sign out**. The signed-in email is surfaced as the menu's header (it left
  the bar). Since Admin no longer has its own top-bar link, **admin attention rides
  the avatar** (the capped `formatBadge` count as a corner pill + in the button's
  aria-label) AND the Admin menu item. **About** (`AboutModal.jsx`) is an
  informational dialog — deliberately NOT `useConfirm` (that's confirm/cancel
  shaped); it reuses the `.modal-*` CSS + the `ConfirmModal` a11y pattern
  (focus-in, Escape/overlay/Close, return-focus-to-opener, background `inert`) and
  links to the GitHub repo. It also links the **end-user + admin guides**
  (`docs/USER_GUIDE.md`/`docs/ADMIN_GUIDE.md`, hosted on GitHub with screenshots) —
  the **Admin guide link is gated to `isAdmin`** (passed from `App.jsx`). It also
  shows the **running version + an "update available" note**: `App.jsx` fetches
  `GET /api/version` once signed in (→ `{current, latest, update_available}`) and
  passes it to About (version line) AND to `Admin.jsx` (a **warn-toned update
  banner** shown ONLY when a newer release exists). An available update is itself
  "work waiting" (update the deployment), so it ALSO adds +1 to the admin **avatar
  attention badge** via `avatarBadgeTotal(attention, isAdmin && update_available)`
  (`attention.js`, vitest-pinned) — the badge stays accent (the generic
  "something's waiting" pill); only the yellow banner signals the update. The
  banner is **NON-dismissible on purpose** — like a pending user or a log problem,
  it persists until you ACT on it (update the deployment → `update_available` goes
  false → banner AND badge clear together), so the badge always maps to a visible
  item in Admin. The running version is
  `config.app_version` (env `APP_VERSION`, baked from the git tag by the Dockerfile
  `ARG`/`ENV` ← CI `build-args`; `"dev"` locally). `backend/app/version.py` does the
  read-only "newer release?" check against GitHub (`config.GITHUB_REPO`), **cached
  ~6h + fails open**, off via `UPDATE_CHECK_ENABLED=false`. It is **negative-cached
  too** (`checked_at` + `_NEG_CACHE_TTL`, 15 min): `at` was written only on
  success and the freshness guard requires `latest is not None`, so a cache that
  had never succeeded could never look fresh — an egress-blocked deploy or a
  tripped GitHub limit (60/hr unauthenticated, fetched per sign-in per worker)
  re-issued a **3s blocking** request on every `/api/version`, forever. Pinned in
  `frontend/e2e/user-menu.spec.js` + `admin-update-banner.spec.js` +
  `backend/tests/test_version.py` + `initials.test.js`.
  Chat interaction contracts (all Playwright-pinned in
  `frontend/e2e/chat-interactions.spec.js`):
  **A RUNNING TURN SURVIVES NAVIGATION, VISIBLY** (`frontend/src/inflight.js`).
  A turn lives only in the browser until it finishes — `_persist` writes both
  message rows in ONE transaction at the END — so mid-flight the server has no
  question, no progress, nothing to fetch. Meanwhile navigating clears
  `messages` and bumps `turnToken` (both deliberate, below), and **nothing ever
  refetched the open thread**, so leaving a running question and coming back
  showed the thread as it was BEFORE you asked and stayed that way *indefinitely*
  — even once the answer was on disk. `inflight.js` is a **module-level
  registry** (the app's first; `useSyncExternalStore`) holding just the question
  text + bookkeeping: enough to draw a **question + "Still working on your
  question…" placeholder**, schedule **exactly one** reload when the turn lands,
  and arm a **`beforeunload` guard**. It is module-level because **Chat UNMOUNTS
  on `/admin`** — the very navigation the feature exists for — so React state
  cannot carry it. Four things that look incidental and are not: the placeholder
  renders **OUTSIDE `messages.map`** (`i` is load-bearing in six places
  including the `trace-${i}` DOM id, so staying out of the loop makes collision
  *structurally* impossible) and carries **no `.msg-actions`** (Edit/Rerun index
  into `messages`); an entry carries **two booleans** — `live` (stream open →
  arms the unload guard, **stays true through Stop**, whose note *promises* the
  answer will be saved) and `show` (drawn → **false after Stop**, or the finished
  answer would replace the stopped note, the same yank scroll containment
  prevents); `settleTurn(rendered:true)` **must not** bump the reload counter or
  a turn refetches the conversation it just created (`midstream-nav`'s
  `conv7.calls === 0`); and the counter is **monotonic**, since it is a
  `useEffect` dep that could otherwise oscillate into a refetch loop.
  **The stopped note tells the reader which state they are in, and offers the
  only check that works.** It used to say "reopen it in a moment to check",
  which nothing in the app could do: `settleTurn` deliberately schedules no
  reload for a stopped turn (the no-yank above), and re-clicking the
  conversation you are already in is not a route change — so the only thing that
  actually refetched was a page reload, which **kills the very turn the note
  promises will be saved**. `inflight.reloadNow(convId)` bumps that same
  monotonic counter on an explicit click (one refetch mechanism, not two;
  `settleTurn` is untouched), and `Chat.jsx`'s `StoppedNote` renders the **Check
  now** button. The `isTurnLive(key)` gate is the load-bearing half: while the
  drained stream is still open the answer is not on disk, so a fetch then
  returns the thread as it stood BEFORE the question and replaces the note with
  it — **the reader's own question vanishing**, which is worse than waiting. So
  the note reads "still being written" with no button, then "has been saved"
  with one. Also withheld when `convId` is null (stopped before the
  `conversation` event) and while `busy` (a LATER turn is streaming, and its
  finalize writes positionally into `messages`, which a refetch would move under
  it). Pinned by the `refetches on an explicit check` / `reports whether a
  stopped turn's stream is still open` cases in `inflight.test.js` and by
  `the stopped note waits for the answer` in
  `frontend/e2e/chat-interactions.spec.js`; all three guards were
  mutation-verified.
  `clearForConversation` runs in the loader's `.then`/`.catch` so the placeholder
  dies in the **same commit** the real rows arrive (no flicker) but **keeps live
  entries** — and **only when the fetch returned something**. An empty fetch has
  nothing in it to replace the placeholder WITH: returning mid-flight issues a
  fetch that correctly comes back `[]` (the turn hasn't persisted yet), and if
  that lands after the turn settles, an unconditional clear deleted the entry and
  committed empty messages together — **the placeholder was replaced by the
  "What would you like to know" GREETING, on a `/chat/:id` URL, with the answer
  already on disk** (found live, one hour after shipping). A settled entry
  surviving an empty fetch is safe: the next fetch carrying the answer clears it,
  and "settled but genuinely empty" can't really occur — a turn that persisted
  nothing either had its NEW conversation removed by `_delete_if_empty` (so the
  load 404s into the `.catch`) or left the earlier messages in place. The unload guard keys on the registry, **not `busy`** — `busy` is
  cleared by the render-time reset the instant the route changes, so it is false
  in exactly the situation the guard is for. Ceiling, stated: **a refresh still
  loses the turn** (the request is torn down, the generator cancelled, and
  `_delete_if_empty` removes a new chat's row) — surviving that needs the turn to
  outlive its request. Pinned in `inflight.test.js` +
  `frontend/e2e/inflight-pending.spec.js`, the latter using a new
  **`mockStreamChatDripped`** (patches `window.fetch` for the stream route and
  enqueues into a `ReadableStream` on timers) because `mockStreamChat` fulfils
  the whole body at once — with it, a brand-new chat's id never arrives until the
  turn is over, so turn 1 was untestable *by construction*.
  **Two testing traps this cost, both of which made a test pass with the bug
  present:** Playwright matchers **auto-retry**, so `toHaveCount(1)` against a
  1.5 s stream simply waits the turn out and goes green having never seen the
  duplicate — assert mid-stream counts **synchronously** (`expect(await
  …count()).toBe(n)`); and a fixture that never contains the answer cannot reveal
  a yank, so the Stop test must re-mock the conversation **with** the answer.
  **Stop generating is
  abandon-and-drain, never a network abort** — it bumps the existing
  `turnToken` so the view detaches while the request drains and the server
  still persists the answer. (Historically an aborted mid-turn request was
  ALSO a server-side data-loss path, but that's now closed: an interrupted
  turn is a no-op — `chat.py` creates a new conversation INSIDE the stream
  generator and reverses the empty row in `finally` via `_delete_if_empty`,
  and folds an edit/rerun's `DELETE FROM messages` into `_persist`'s
  transaction via `delete_from_id` so it commits atomically with the
  replacement. So a real AbortController Stop would no longer corrupt state;
  abandon-and-drain is now a deliberate choice — it still PERSISTS the answer,
  which a network abort would discard — not a workaround. Pinned by
  `test_interrupted_new_turn_leaves_no_phantom_conversation` +
  `test_interrupted_edit_turn_keeps_the_old_exchange_intact` in
  `backend/tests/test_chat_router.py`.)
  **An abandoned turn still applies its server message IDS** — and nothing else.
  `stopGenerating` bumps `turnToken`, so `isMine()` is false and the finalize
  (the ONLY place `msgId`/`userMsgId` were written) was skipped, leaving the
  stopped user message with no `id`. Rerun then sent `edit_message_id: null`,
  `_persist` skipped its DELETE and **APPENDED** — so the DB silently grew a
  duplicate of the question the user was replacing, while the client had already
  `slice`d it away. Silent because a stopped turn is the LAST turn, so
  `laterTurnsLost` is 0 and no confirmation fires. The ids are targeted by a
  **per-turn `_turn` key** stamped on the two messages a turn appends
  (client-only, never sent or persisted) — NOT positionally, and NOT by
  `turnToken`. Both alternatives are wrong for a specific reason: the finalize
  writes `c.length-1`/`c.length-2`, which by then may belong to a conversation
  the user has since opened; and **`submit()` never bumps `turnToken`**, so a
  turn started AFTER a stop captures the same value — a token-equality gate is
  true for both and leaks the stale ids onto the new turn. The `findIndex` lookup
  IS the scope check: navigation, "+ New chat", an edit/rerun slice, and a
  refetch all leave no `_turn` to match, so the write self-cancels with no
  separate "still the right conversation?" test to get wrong. Content /
  `pending` / `stopped` are deliberately NOT written — the user chose to stop,
  and pulling the finished answer in under them is the same yank the scroll
  containment exists to prevent. Pinned by the three cases in the
  `stop generating` describe of `frontend/e2e/chat-interactions.spec.js`, each
  proven load-bearing: **without** the fix the duplicate case fails; under a
  naive **positional** fix the other two fail.
  **Edit/Rerun is destructive beyond the turn it touches** — it drops that turn
  AND every later one, client-side (`slice(0, i)`) and server-side (`_persist`'s
  `DELETE … id>=?`), permanently, with no tombstone or undo. Re-asking the LAST
  turn is the ordinary refine and stays **modal-free** (that path also carries
  the assistant-side "Try again" button); anything earlier routes through
  `useConfirm` naming the count. The safe-case predicate is pure and
  vitest-pinned (`turns.js`: `messages` is a strictly alternating user/assistant
  array, so a turn is the pair `[i, i+1]` and the last turn is
  `i === length - 2`). `confirm()` is NOT awaitable and `onConfirm` must not
  return `submit()`'s promise, or the modal sits spinning for the whole streamed
  answer. Pinned in `turns.test.js` + the `destructive edit/rerun confirmation`
  describe in `frontend/e2e/chat-interactions.spec.js`.
  Auto-scroll **follows only while the viewer is near the bottom** (scrolled
  up = never yanked; a "Jump to latest" pill is the way back). Conversation
  switches show a skeleton, never the empty-state prompt. **Both empty states
  belong to the NO-CONVERSATION route** (`routeId === null`) — they are the
  "you haven't asked anything yet" screen, so rendering one on a `/chat/:id`
  URL is the index page impersonating someone's conversation. Found live: the
  in-flight placeholder was replaced by the greeting + six example chips on a
  `/chat/:id` whose answer was already saved. `messages` being momentarily
  empty on a conversation route is a TRANSIENT to ride out (loader in flight,
  or a turn not yet persisted), never a cue to offer a fresh start — so the
  worst case is now a blank thread, not a wrong one. Keyed on `routeId`, NOT
  `convId`: convId is also null for a malformed id, where the notice is the
  right thing to show. This also stops a failed load rendering "That
  conversation isn't available." AND the greeting together — a contradiction
  the skeleton already guarded with `!showNotice` and the empty states never
  did. A printable key
  typed with nothing editable focused redirects into the composer
  (`typeahead.js`, vitest-pinned). The **composer is Markdown-highlighting** but
  stays a real `<textarea>`: `MarkdownTextarea.jsx` layers a transparent textarea
  over a colored `<pre>` mirror (`mdhighlight.js`, a pure/vitest cosmetic lexer
  whose segments **concatenate back to the source exactly** — the composer's value
  is always the raw Markdown string, so undo/redo, plain paste, IME, and the
  character-level edits like `---`→`--` on Backspace all come free). Highlighting is
  **color-only** (dimmed markers, tinted structure) — never weight/size, which would
  shift glyph widths and drift the caret off the overlay. It does NOT render blocks
  (no HR/heading-size/hanging-indent while typing) by design. User bubbles already
  render the stored plain Markdown through the safe `Markdown.jsx` (unchanged).
  Pinned in `frontend/e2e/composer-markdown.spec.js` + `mdhighlight.test.js`.
  Conversations can be **renamed inline**
  (`PATCH /api/chat/conversations/{id}` — metadata-only by contract: it must
  never touch `updated_at`, or renaming an old chat would reorder the
  recency-sorted sidebar). An answer's **Thinking / SQL traces are
  mutually-exclusive disclosure toggles** whose panel opens **full-width below**
  the actions row (never as an inline `<details>` inside the flex row, which
  widened its own cell and shoved the copy buttons around); opening one closes
  the other. The two per-answer copy actions collapse into a **single Copy menu**
  (`CopyMenu.jsx`, UX-H3) — one `.link` menu button ("Copy ▾") opening
  `Copy Markdown` / `Copy rich HTML`, built on the same WAI-ARIA menu-button
  pattern as `UserMenu.jsx` (roving arrows, Home/End, Escape-closes-and-restores-
  focus, click-outside) and reusing the menu-panel CSS; the copy LOGIC stays in
  `Chat.jsx`'s `doCopy`, and the trigger flips to a "Copied!" check on success.
  Pinned in `frontend/e2e/chat-interactions.spec.js` (copy-menu describe). The
  **Thinking trace is persisted** (migration 12,
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
- **Migrations are forward-only AND refuse to go backwards.** The runner only
  ever tested `version > current`, so a `user_version` PAST our newest matched no
  branch, the loop did nothing, and the app then ran — and WROTE — against a
  schema it doesn't understand, silently. That happens on an ordinary rollback
  (pinning `IPEDS_TAG` back) or restoring a newer `app.db` into an older image,
  and `app.db` is the irreplaceable store. `_apply_migrations` now raises
  **`SchemaTooNewError`** (CRITICAL-logged, naming both versions) and `init_db`
  is deliberately un-caught in `lifespan`, so the app **refuses to start**.
  Pending migrations also take a **pre-migration snapshot** first
  (`_snapshot_before_migrating` → `app.db.pre-v<N>`, sqlite's online backup API,
  never fatal) so an upgrade is reversible — several shipped migrations are
  multi-statement `executescript` blocks that are not atomic, and a part-way
  failure otherwise bricks every later boot on "duplicate column name".
  **Scheduled backups are NOT a gap — they are a settled policy.** This
  paragraph used to call `scripts/backup_app_db.py` going uncalled a "recurring
  backup gap" needing the first background task in `lifespan`; that was the
  error. The README is the policy: the operator snapshots the bind-mounted
  volume or crons the script, and the app schedules nothing. The pre-migration
  snapshot above is an upgrade safety net, not a backup. One wrinkle the
  non-root container adds: the script's `--out-dir` defaults to a RELATIVE
  `backups`, which uid 10001 cannot create inside the container, so an
  in-container run needs `BACKUP_DIR=/data/backups`.

### The agent loop
LLM = **any OpenAI-compatible provider** (`LLM_BASE_URL`, **OpenRouter** by default,
through the shared `backend/app/llmhttp.py` transport). **`MODEL_DEFAULT` ships with
NO default and must be set** — the app is vendor-neutral on purpose, so a shipped
default would both brand it and silently route a self-hoster's traffic to a model
they never chose; `MODEL_ESCALATION` is optional (blank = never escalate) and is
reached for after repeated tool failures. A key with no model logs a CRITICAL at
boot (`main._missing_model_warning`). Run as a tool-calling agent loop wrapped in
three guards.

**Every tool call runs OFF the event loop** (`llm._dispatch`). `registry.dispatch`
→ `tools/sql.run_sql` is blocking `sqlite3` called from inside `stream_agent`, an
**async generator** — so run inline, one query holding the full 25 s
`sql_timeout_seconds` budget stalled the ENTIRE event loop: with one uvicorn
worker that is every other user's stream, the admin console and `/api/health`,
and even that turn's own already-queued `{"type":"sql"}` event couldn't flush.
`routers/chat.py` already threadpooled its blocking DB work; these two sites (the
main tool loop and the critic-correction round) were the oversight. Safe because
`run_sql` opens a FRESH connection per call and closes it in `finally`, with
`check_same_thread=False` already set, and the timeout watchdog is already its
own thread. **The one invariant: callers await these ONE AT A TIME** — the
per-request `result_sink` dict and `res.sql_log` are shared mutable state, and
sequential awaits are the whole reason there's no race, so never
`asyncio.gather` the tool calls. Trade-off, stated: SQL now shares Starlette's
default 40-worker threadpool with every sync route handler, so a burst can
saturate it — a higher ceiling, not the absence of one. Pinned by
`test_a_blocking_tool_call_does_not_stall_the_event_loop`, which FORCES the bad
branch (dispatch blocks on an Event only a concurrent asyncio task can set)
rather than timing a fast query: measured 2.39 s inline vs <1 s threadpooled.

The three guards:
- a topical **guardrail** in front (off-topic questions never reach the DB) —
  `guard.py`'s `_SYSTEM` explicitly whitelists **corrective feedback and a
  meta-critique of a prior answer's method/scope** (e.g. "you should have kept
  the bachelor's scope") as IN_SCOPE, alongside brief contextual follow-ups and a
  short answer-phrase reply to the assistant's own clarifying question (e.g.
  "bachelor's only") — load-bearing for both the clarify chips and the feedback
  distiller below, and the fix for a real regression where the gate refused a
  user's own corrective feedback as off-topic (`backend/tests/test_guard.py`);
- a deterministic SQL **linter** (`backend/app/tools/sqllint.py`) — a pre-flight check that
  flags IPEDS aggregation foot-guns (CIP-rollup / second-major double counts,
  DISTINCT-year full-scans) in the model's SQL and feeds the warning back so the
  agent self-corrects;
- a deterministic **figure-grounding check** (`backend/app/grounding.py`) — the
  answer's hero figure is the most prominent number on screen, and `_extract_figure`
  once validated only its JSON *shape*. The check reproduces the figure's value from
  the turn's **retained** `QueryResult`s — verbatim, at the figure's display
  rounding, or via the derivation menu prompt step 6(ii) asks for
  (`sum`/`mean`/`pct_change`/`diff`/`share`/`max`/`min`/`row_total`) — recording
  `exact`/`rounded`/`derived`/`ungrounded` (plus non-evidence
  `no_figure`/`unchecked`). Pure arithmetic (no DB/LLM/network), runs on every
  answer, no setting. **OBSERVE-ONLY — alters no answer, blocks nothing**; lands on
  `usage_log.figure_grounding` (migration 21) → **Grounded figures** on Admin →
  Usage (`groundedFigureRate`, vitest-pinned), whose denominator counts *only* turns
  with both a numeric figure and results to check (folding the no-figure majority in
  would peg it near 100% and destroy the signal). Aggregations are barred over
  **dimension** columns (`year`/`unitid`/`cipcode`/… — `_DIMENSION_COL_RE`): `year`
  is in nearly every IPEDS result, and a real +25.0% trend once "verified" as
  `share(year)` inside tolerance. **`row_total` is the SECOND op added after a LIVE
  false `ungrounded`** (the first was `diff`): every other op aggregates DOWN a
  column, so a figure totalling ACROSS one row of a PIVOTED result — the canonical
  by-award-level breakdown, and exactly what step 6(ii) invites for a peak-year
  hero stat — had no route and read as ungrounded despite being exactly
  reproducible (observed: `324,575 — peak national nursing degrees in 2022`, the
  row-wise sum of five award-level columns). Tried LAST (weakest route, never
  displacing a verbatim cell), needs ≥2 measure columns, excludes dimension/rank
  columns, and is **figure-only** — `check_table` grades hundreds of cells, so
  widening its match surface would inflate Grounded-cells with coincidental hits.
  A kernel that cannot reproduce a CORRECT number manufactures evidence of model
  error, the most damaging way this measurement can be wrong.
  **A TRUNCATED result may not supply a column aggregate.** `run_sql` cuts at
  `sql_row_cap_model` and tells the model not to total the page; when the model
  did it anyway, the kernel recomputed that same total from those same partial
  rows and called it `derived` — corroborating the error it exists to catch. The
  rule: a route may run over a truncated result **iff its value is invariant to
  appending the rows that were cut**. Truncation drops a SUFFIX, so a value at a
  known row index is invariant (verbatim cell, hedge bound, `row_total`, the
  row-wise ops, `prev_diff`/`prev_pct_change`) and stays allowed; anything
  reading the column's EXTENT (`sum`/`mean`/`share`/`pct_change`/`diff`, and a
  `_cross_scalars` total or complement sourced FROM a cut result) refuses. The
  gate is keyed **per RESULT, never per turn** — `sql.py`/`prompt.py` tell the
  model to fix a cut ranking with a separate `SELECT SUM(...)`, so an
  untruncated sibling in the same turn must stay fully checkable; a per-turn
  form is pinned against by `..._when_a_SIBLING_is_truncated`, which fails on
  the DERIVATION (`cross`/-1 instead of `sum`/1), not on the status — a bare
  "is it derived?" assertion does not discriminate. **`max`/`min` are named in
  the rule but cannot actually refuse**: `compute("max", …)` always returns a
  value that IS a cell, so the always-allowed verbatim route matches first. That
  is correct — grounding attests REPRODUCTION, not that the model's "this is the
  maximum" reading of the number is right. It needs **no migration**:
  `to_storage` carries `truncated` (emitted only when true, so an untruncated
  blob stays byte-identical and a legacy blob still reads False), and also sets
  it when its OWN `max_rows` cut rows — a blob that lost rows is exactly as
  unsound to aggregate over, whichever layer cut them. **NOT observe-only in
  effect**: the verdict itself still alters nothing, but two existing consumers
  act on `ungrounded` — `_maybe_retry_figure` SUPPRESSES a retry-recovered
  figure and `_s5_fabricated` can degrade a tool-exhausted answer — so widening
  what lands ungrounded feeds both, and steps Grounded figures / Grounded cells
  down on truncated turns by design. Retention is the foundation: `AgentResult.results`
  keeps every call's result (in call order), where `last_result` used to overwrite.
  **The persisted-results cap really is a cap now** (`_results_for_storage`,
  `routers/chat.py`). It drops the largest results first, but that loop was
  guarded by `len(blobs) > 1` — so it was a no-op for a single result and stopped
  the moment dropping left one, and **the survivor was never measured**.
  `RESULT_STORE_MAX_BYTES` (64 KB) therefore meant "at most one result may exceed
  it, unbounded": `to_storage` caps rows (200) but not WIDTH, and one value may
  reach `SQL_MAX_VALUE_BYTES` (1 MiB), so 200 rows of a wide `SELECT *` is
  comfortably megabytes — written **twice**, into `messages.results` AND
  `query_cache.results` (whose comment reasoned from "already capped by the
  caller", which is what stopped anyone looking). Measured 2,002,125 bytes stored
  against the 64,000 ceiling. The lone survivor is now **shrunk** — halve its
  rows until it fits — rather than dropped, since it is the turn's only evidence.
  **If not even one row fits, it stores NOTHING, and that direction is the
  point**: a blob with columns and zero rows reads to grounding as "checked, and
  nothing reproduced" — an `unmatched` verdict raising the ⚠ caution on a CORRECT
  answer — while NULL reads as `unchecked` and renders silently. My first fix
  returned the zero-row blob; the test caught it. Losing rows can only cost a
  match that would have been made (a false `ungrounded`), never manufacture a
  false ✓ — the same trade the 200-row cap already makes.
  **Grounding is CONVERSATION-scoped**: each turn's results are persisted
  (`messages.results`, migration 23, capped + backend-only) and the recent window is
  re-hydrated (`_load_prior_results`, same `before_id` semantics as `_load_history`
  — but a **~2× WIDER window**, a known open question: both LIMIT `HISTORY_TURNS`,
  yet history counts ALL messages (6 ≈ 3 turns) while prior-results counts only
  result-bearing assistant rows (6 ≈ 6 turns), so grounding can borrow results
  from turns whose prose the model never saw. Narrowing is defensible in
  principle but was **measured and could not be decided** — 8 of the 9 graded
  turns in the corpus were fed identical inputs, so "no change" proved nothing —
  and shrinking the pool can only produce a FALSE caution on a correct answer.
  Needs a corpus with several 6+ turn conversations; pinned meanwhile by
  `test_the_two_recent_windows_are_measured_in_different_units`)
  into `stream_agent(prior_results=…)`. A figure is checked against THIS turn's
  results FIRST, then the borrowed prior ones (`_ground_results`), so a follow-up
  that recites a number without re-querying grounds against the earlier turn that
  produced it, tagged **`ctx:`** in `figure_derivation` (composes with `retry:` →
  `retry:ctx:pct_change(q3.x)`). Prior results are borrowed for grounding only —
  **never re-persisted** as this turn's own and **never fed to the model** (we verify
  recitation, we don't prevent it) — and this relaxes `_figure_required` to fire on a
  no-SQL turn when prior results exist. Pinned in `backend/tests/test_grounding.py` +
  `test_agent_loop.py` + `test_chat_router.py`.
- a deterministic **table-grounding check** (`grounding.check_table`, same module,
  also **OBSERVE-ONLY**) — the results **table** is the model re-typing the query
  rows one-for-one, the densest block of numbers on screen. It parses the answer's
  GFM tables (`parse_markdown_tables`, header kept, skipping ```` ``` ````-fenced
  regions so a ```chart isn't read as a table) and grades the **MEASURE columns
  only** — `_is_measure_column` excludes a **rank ordinal** (a pure 1..N sequence,
  whatever the header) and any **dimension** column (`is_dimension`:
  rank/year/unitid/cipcode/id/…), so a model-added Rank column that was never in the
  DB can't drag the rate down. Each graded cell is reconciled **CONVERSATION-scoped,
  mirroring the figure**: against this turn's results borrowed with the recent window
  (`_ground_results`/`prior_results`, the same #166 infra), so a follow-up that
  RESHAPES an earlier table (transpose/regroup, no SQL of its own) is VERIFIED
  against the borrowed base rows, and a corrupted reshape is caught. Reconciliation
  uses the shared `_reconcile_value` kernel (verbatim / display-rounded / derivable)
  but with **`allow_dimension=False`**: a measure cell is verified only by a MEASURE
  result-column, never a code/dimension column it merely collides with (a small
  count "3" must not ground against an `awlevel` 3 — the figure path keeps
  `allow_dimension=True`, since a headline can legitimately BE a year/code).
  **A table row is ANCHORED to the result row it describes** (`_anchor_row`), and
  graded against that row alone. This replaced a column-wide search that was wrong
  in BOTH directions, and one mechanism fixed both:
  **(a) false negatives** — every op ran DOWN a column, so a row-wise `% change`
  column (`(2024-2021)/2021` for *that row*) had no route and a CORRECT table graded
  `partial`, or `unmatched` when such a column was its only measure. That
  measurement is why the reader-facing mark is positive-only.
  **(b) false positives** — measured on the retained corpus, scaling every number in
  eight real answers by 1.2–1.9× still left **24.0%** of cells "grounded"
  (2142/8920), 34% on the widest turn; 878 of those were plain `exact` hits on a
  `total_degrees` column holding **506 values across three results**, where "somewhere
  in the column" is nearly free. After anchoring: **0.63%** (56/8920), with real cells
  unchanged at 446/446.
  **Two cell FORMATS are handled, both found by driving live questions and
  reading the cautions** (neither was visible to review, and each turned a CORRECT
  answer into a warning): **(1) Markdown emphasis** — `parse_number` strips
  `**bold**`/`` `code` ``/`*italic*` (`_EMPHASIS_RE`). Without it such a cell failed
  to parse and was DROPPED — never counted, never checked; 7 of 14 numeric cells in
  one live answer escaped because the model bolded them, which is its own convention
  for the numbers that matter most, so the ✓ mark undercounted while sounding
  authoritative. **(2) Hedged cells** — `<0.1%`/`≥5` state a BOUND, so
  `parse_hedge`/`satisfies_hedge` test the INEQUALITY instead of the digits; reading
  `<0.1%` as the quantity `0.1` compared it against a true 0.0179% and called a
  correct hedge a miss. A bound is deliberately weaker evidence than an equality —
  that asymmetry is the honest reading of what the model claimed, not a loosened
  tolerance, and a bound nothing satisfies still fails.
  **CROSS-RESULT derivations** (`_cross_scalars`/`_match_cross_result`) close the
  last live gap: the model routinely takes rows from one query and the denominator
  from a second `SELECT SUM(...)`, so every share was one result's row over another
  result's scalar and nothing could reproduce it. Observed on an ordinary question
  — all eight unreproduced cells AND the hero figure were exact
  (`11,620/45,883 = 25.3%`, `45,883-30,568 = 15,315`). The ingredient is a TOTAL:
  every measure column's sum from any result, plus pairwise **complements** ("all
  others" is the other half of every share breakdown *and* the numerator of the
  next share). It is the WIDEST search in the module and runs absolutely LAST, with
  two precision guards that are **individually pinned because the aggregate probe
  cannot see them**: a share must be **written with a `%`** (the marker splits the
  two routes — unsplit, one answer offered 11 totals + 55 complements to every cell
  and fabricated grounds went **0.9% → 10.4%**), and a share must land **in
  (0,100]**. Applies to the FIGURE too, so Grounded figures moves — correctly: it
  was reporting a false `ungrounded` on a right answer.
  Anchoring scores (label matches, numeric matches) and returns the **GROUP** of
  rows tied at the best score — not a unique winner. A **PIVOTED** table row
  legitimately describes several result rows at once (one row per year, one
  column per category), so demanding uniqueness was backwards and the two halves
  compounded: the result actually holding all the numbers tied N ways and was
  REFUSED as ambiguous, a SUPERSEDED result matched one row and anchored
  UNIQUELY, and because *something* anchored the right result was never
  consulted. Measured live (conversation 23): a table whose every number was
  correct and present graded **5/15 `partial`** — a ⚠ on correct work, the one
  thing the caution must never do. Grouping is bounded by `_MAX_ANCHOR_GROUP`
  (12): a group spanning most of the result is the unrestricted column search
  under another name. **Measured both ways on the retained corpus: recall
  83.3% → 98.0%, fabricated-ground rate UNCHANGED at 1.33%** — two false
  cautions removed (msgs 108 and 100, the latter the long-tracked "pivot gap")
  with no precision cost; it is in fact TIGHTER for pivot rows, which used to
  fall through to the column-wide search. The regression test carries a DECOY
  superseded result on purpose — without it the case passes with the bug still
  present. Anchoring still needs a label or ≥2 numeric matches, and
  compares numbers by **IDENTITY, never `_close()`** — a relative tolerance made
  adjacent years indistinguishable (2023 is within 0.1% of 2021/2022/2024/2025), tying
  every row of a by-year result and DROPPING correct cells. An unanchorable row (a
  `Total` line, a reshape) falls back to the old unrestricted search, so those keep
  grounding as before. An anchored cell may use: its own row's cells; row-wise
  `sum`/`pct_change`/`diff`/`mean`/`share` (**the fix for (a)**); `prev_diff`/
  `prev_pct_change` against the PREVIOUS row (a "% vs prior year" column — a SECOND
  blind spot of the same class, found by probing the fix, which graded 3/6); and
  column `sum`/`mean`/`share`-at-this-row. **`max`/`min` are deliberately barred** —
  the row legitimately holding the column max grounds via its own cell, so they add no
  recall while re-admitting the likeliest real error (copying the top row's number
  down a column). Costs ~2× runtime (45→106 ms on the widest real turn), all of it
  off the LLM critical path. Records a per-turn
  status (`matched`/`partial`/`unmatched`/`no_table`/`unchecked` — the last means
  neither this turn nor the window retained anything) + numeric-cell counts on
  `usage_log.table_grounding`/`table_cells_checked`/`table_cells_matched`
  (**migration 25**; `no_table`/`unchecked` carry 0 counts so they self-exclude from
  the SUM-based rate) → a cell-level **Grounded cells** stat on Admin → Usage
  (`groundedTableRate`, vitest-pinned). Stamped in `llm.py`
  (`_stamp_table_grounding`) right after the figure stamp on BOTH terminators, on the
  FINAL settled answer. Pinned in `test_grounding.py` + `test_admin_router.py` +
  `test_migrations.py`.
  **The verdict is also shown to the READER** (the table's counterpart to the
  figure's ✓): status + counts persist on `messages.table_grounding`/
  `table_cells_checked`/`table_cells_matched` (**migration 33**) and ride the `done`
  SSE event, so `Chat.jsx`'s `TableTrust` renders one **answer-level** line —
  `✓ 40 values reproduced from the query result` — as a sibling AFTER `<Markdown>`,
  outside the `.md` copy surface (same rule as `<Figure>`). **ANSWER-scoped, not
  per-table**: `check_table` returns ONE verdict for every table in the answer, so
  attaching it to a particular table would mis-attribute it — which is also why it
  needs no single-table gate (unlike the truncation caption, whose flag maps to one
  query result). Wording rules in the pure `tabletruth.js` (`tableTrustNote`,
  vitest): state the **count, never "all"** (measure columns only were graded), and
  promise **reproduction, not correctness**. **TWO-SIDED since the reconciler was
  anchored:** `partial`/`unmatched` render a **⚠ caution** in `--warn`
  (`.table-trust.warn`, an inline `IconWarning` — the ⚠ codepoint renders as a colour
  emoji on some platforms) reading **`Check 13 of 22 values against the SQL or CSV`**.
  **It is phrased as an INSTRUCTION, not a verdict, and that is the whole design.**
  Every time it fired on real data it was a gap in the CHECKER, not a model error —
  bolded numbers, a `<0.1%` read as `0.1`, a cross-query share, a header mistaken for
  an ID: four correct answers flagged. A line claiming the numbers "could not be
  reproduced" reads as *don't trust these* and attacks work that was fine, and a
  warning that is usually wrong teaches people to ignore it — costing exactly the day
  it is finally right. An instruction survives being wrong: the reader looks, sees the
  numbers are fine, and has lost ten seconds. Both destinations are real controls on
  the same answer (the SQL disclosure below it, the CSV export on the table).
  **Don't reword it into a claim about the numbers unless the false-alarm rate has
  been measured at zero.** It also must not borrow the `--danger` treatment of a
  genuinely failed turn; the answer is still an answer.
  **BORROWED evidence says so.** Grounding is conversation-scoped, so a turn that
  reshapes an earlier table runs no SQL and is checked against THAT turn's rows —
  deliberate, and the only reason a transpose verifies at all. But the note read
  "reproduced from **the** query result" on an answer whose `sql_log` is `[]`,
  sending anyone who wanted to check to a SQL disclosure that isn't there (found
  live; it made a CORRECT ✓ look suspect). `hasSql` (from `m.sql_log`) now picks
  the source clause: "the **earlier** query result", and the caution points at
  "the **earlier answer's** SQL or CSV" — the destinations have to EXIST, and on
  a reshape the CSV button exports only the transcribed rows anyway (see
  `Markdown.jsx`'s `hasSql` gate). Same claim, different source; only the source
  clause changes. Pinned in `tabletruth.test.js` + a `table-grounding.spec.js`
  case that fails if the prop is dropped in `Chat.jsx` — the plumbing is the part
  that silently regresses. **`unchecked`/`no_table` stay SILENT** (nothing was
  compared, so neither tone applies), and so does any failure verdict whose counts
  are missing or contradict it: **`Number(null)` is `0` and finite**, so a
  pre-migration row's NULL counts read as "0 of N matched" and manufactured a caution
  against an answer nothing ever graded — the same trap `years.js` hit, caught here
  by a test, not by review. `table_cells_matched` had to be plumbed onto the live
  turn (the `done` event carried it; `Chat.jsx` dropped it). A
  cache hit shows NEITHER mark (it passes no grounding, like
  `figure_grounding`). Pinned in `frontend/e2e/table-grounding.spec.js` (incl. a
  direct contrast assertion — the axe scan never renders this element, and light
  theme clears AA by only ~0.07) + `tabletruth.test.js` + `test_chat_router.py`.
- a post-answer **critic** that can force one revision round. **It is given the
  actual result rows** (capped, via `QueryResult.to_markdown`, with a truncation
  flag) — without them it saw only the SQL *text* and the prose, so it could
  judge whether a query looked right but never whether the answer's numbers were
  in the data. The revision only
  ships if the model **re-queried AND changed the answer AND its prose carries no
  reviewer-directed meta** (`_leaks_review_meta` in `llm.py` matches
  "reviewer"/"the review"); otherwise the clean pre-critique draft is re-emitted,
  `critic_revised=False`. This closes the observed leak where a *confirm*-by-
  requery rebuttal (same number, new "the reviewer's concern…" prose) slipped
  past the requeried-and-changed gate — see `backend/tests/test_critic.py`.
  **Only SOME findings may become lessons, and the prompt never says which.**
  The REVISE reply carries a **`CATEGORY:`** from the closed seven-token set in
  **`backend/app/lessoncats.py`** (a dependency-free leaf module, `seeds.py`'s
  precedent — three modules need the enum and `skills.py` reaching it via
  `critic.py` would drag `httpx` into the skills import graph for a constant).
  Five data-modeling categories are LEARNABLE; **`UNGROUNDED_NUMBER`** and
  **`OTHER`** are not. The first IS the class Todd kept rejecting in production —
  "verify figures against the query result before emitting them" — which
  `grounding.py` already enforces deterministically per turn, so a lesson
  retrieved at query time cannot fix it. The second is excluded because it would
  otherwise be the **escape hatch**: a model whose `UNGROUNDED_NUMBER` findings
  are discarded would simply relabel `OTHER`, making the gate a one-hop detour
  rather than a fence. **No parseable category → no lesson** (fail closed); the
  cost is that a genuinely novel insight fitting no bullet is never learned,
  accepted because adding a bullet is a one-line change.
  **The gate is CATEGORICAL because similarity provably cannot do it** —
  measured with the app's own model: five phrasings of the rejected class sit at
  cosine **0.625–0.802** to each other while two genuinely different legitimate
  lessons sit at **0.673**, best separation **0.703 vs 0.681**, i.e. none. Don't
  re-derive that by trying an embedding filter; it's in `lessoncats.py`'s
  docstring.
  **THE REVISE STILL FIRES FOR EVERY CATEGORY** — only the *learning* is gated,
  and an `UNGROUNDED_NUMBER` finding must still force its revision round, the one
  thing `grounding.py` can't do alone (make the model re-query and fix the number
  before the user sees it). "The critic no longer handles X" is exactly the wrong
  summary to act on.
  `_SYSTEM`'s bullets are **assembled from `lessoncats.BULLETS`** so prompt and
  enum can't drift, and its old closing line ("…AND stored as a learned lesson")
  is **DELETED** — once categories gate storage that sentence invites relabeling.
  The prompt now never uses the word "lesson" nor reveals the learnable set,
  pinned by a **negative** test. Two bugs found on the way, both invisible to
  review: `_DESCRIPTION_RE` stopped only at a following `headline:`, so a
  DESCRIPTION-before-CATEGORY reply swallowed the literal `CATEGORY: X` into the
  stored description (surfaced through `test_feedback.py`, since `feedback.py`
  reuses `parse_verdict` — which keeps its **exact 3-tuple**, that suite passing
  untouched being the behaviour-neutrality signal); and the critic-lesson
  recording call was a bare `await` inside the SSE generator **after** the answer
  is persisted with no `try/except`, while its feedback sibling has always been
  guarded. `critic_category` must be set at **BOTH** `llm.py` critic call sites
  (main loop AND exhaustion path) — missing the second fails closed and silently.
  **The critic also runs on the TOOL-BUDGET-EXHAUSTED path** (S5): when the agent
  burns all `llm_max_tool_iters` and falls back to the tools-disabled "best effort"
  synthesis (the highest-risk path, once shipped with ZERO review), it now gets the
  same critic — and on a REVISE a **bounded correction round with tools RE-ENABLED**
  (`_CRITIC_CORRECTION_ITERS=3`, a capped exception fired only by a REVISE) so a
  flagged aggregation error can actually be re-queried and fixed. The SAME anti-leak
  gate applies (a rebuttal or confirm-only re-query reverts to the clean draft).
  The exhaustion path also carries a deterministic **GROUNDING GATE and a raised
  ceiling** (measured from a real fabrication — a whole 0/15-cell table invented at
  the old cap): **(1)** `llm_max_tool_iters` **defaults to 20** (`LLM_MAX_TOOL_ITERS`,
  was 12) — a genuine multi-table question needs ~15-17 rounds, and cutting off
  mid-progress is what forced the confabulation; higher only costs on hard turns,
  each reusing the cached prefix. **(2)** After the synthesis + critic + grounding
  stamps, `_s5_fabricated(res)` degrades the answer to an honest
  **`_EXHAUSTION_DEGRADE`** message (dropping any fabricated figure/chips) when its
  numbers are WHOLLY ungrounded (`table_grounding=unmatched`, or an `ungrounded`
  figure with no grounded table); a `partial`/`no_table`/`unchecked` answer is left
  alone. **S5-only** on purpose — the normal path keeps shipping first-pass
  ungrounded figures observe-only (#163); acting on the verdict is scoped to the
  highest-risk path (a sibling to `retry:suppressed`). **(3)** `_strip_tool_markup`
  scrubs leaked pseudo-XML tool-call markup (`<｜｜DSML｜｜tool_calls>…`, emitted by
  some model families instead of the API's tool_calls field) from BOTH
  terminators. Exhaustion is recorded on `usage_log.exhaustion` (**migration 27**:
  `answered`/`degraded`/NULL) → the **Exhausted** stat on Admin → Usage
  (`exhaustionLabel`, `· N degraded` breakdown). Pinned by the `S5:`/`S5 gate:` cases
  in `test_agent_loop.py` + `test_admin_router.py` + `test_migrations.py`.
- **A STRANDED critic revision is not exhaustion.** Two different failures used to
  land in that same tail. The critic `continue`s for a revision round; if that round
  never returns a tool-call-free reply — it fired on the **last** iteration, or it
  burned every remaining iteration on tool calls — the loop ends with `draft_answer`
  set and the settle gate (which lives *inside* the terminator) never runs. The tail
  then skipped its own critic (`not critiqued` is False) and applied
  **no `_leaks_review_meta`**, shipping the revision round's reviewer-rebuttal prose
  verbatim: the PR #43 [[critic-revision-leak]] regression, reintroduced through a
  door that forgot the gate. Now: `res.exhausted = not draft_answer`, and a stranded
  draft **ships the clean pre-critique answer, skipping the synthesis call entirely**
  — that call passes `tools=None`, so a revision could never re-query, so `requeried`
  is False *by construction* and the gate could only ever revert to the draft; the
  call is guaranteed-wasted and its only novel output is the leak. The `_s5_fabricated`
  degrade is gated on `res.exhausted` so a reviewed draft is never replaced by
  `_EXHAUSTION_DEGRADE`. The settle gate itself now lives in ONE place
  (`_settle_revision`), called by both terminators — it existing twice is how they
  drifted. Accepted: a stranded draft skips `_maybe_retry_figure`. Note the metric
  narrowing — a revision round that genuinely burned the budget no longer counts as
  Exhausted; that's deliberate (overloading the flag is what corrupted it), and a
  separate `critic_unsettled` counter is the follow-up if the rate matters. Pinned by
  the `[[critic-stranded-revision]]` cases in `test_agent_loop.py`.
- **Both terminators are ONE function now: `_finalize_answer`.** The normal
  no-tool-call path and the S5 exhaustion/stranded tail ran the same settle
  sequence inline — normalize → extract figure/suggestions → grounding stamps →
  scrub → emit — and had already drifted **twice**, the second time into the #205
  P0 above. The failure mode is a difference that exists only as a **missing
  line**, invisible in review. Every real difference is now a **named flag with a
  reason**: `allow_figure_retry` (normal path only — a tools-disabled S5 synthesis
  could not have grounded a recovered figure) and `allow_degrade` (S5 only, and
  additionally gated on `res.exhausted`, so a reviewed stranded draft is never
  replaced by `_EXHAUSTION_DEGRADE`). Both flags are **proven load-bearing**:
  flipping `allow_figure_retry` off fails the three retry contracts, flipping
  `allow_degrade` off fails the S5 gate contract. The terminal events live in
  `_final_events` (a plain generator — only the async generator itself can yield
  into the stream). `res.model_used`/`results`/`last_result` stay at the call
  sites: they describe the calling context, not the settle. Behaviour-neutral —
  `test_agent_loop.py`/`test_critic.py`/`test_grounding.py` pass **untouched**,
  and the NL→SQL eval stayed 3/3 with no escalation.
- **Structured emission** (`config.structured_emission_enabled`, **DEFAULTS ON**;
  validated 100%-structured / 0-leaks across four vendors). The durable,
  model-agnostic fix for mangled fences: instead of free-typing
  ```figure/```chart/```followups/```clarify fences, the model FINISHES a turn by
  calling an **`emit_answer`** (or **`ask_clarification`**) tool whose fields the
  *provider* validates. `llm.py` intercepts that call and **reconstructs
  WELL-FORMED fences from the validated args** (`_reconstruct_answer` + `_fence` —
  the SERVER writes them, so they always parse), then falls into the SAME
  no-tool-call terminator, leaving `_extract_*` / critic / grounding / retry /
  persistence AND the frontend unchanged. A tool-incapable model falls back to the
  fence path (the retained fallback; set the flag false to force it).
  **Forced re-emit — the structured-emission GUARANTEE:** when a turn free-types
  the terminal answer under structured mode, `_forced_emit` makes ONE
  **reasoning-off** follow-up call that FORCES `emit_answer`
  (`tool_choice:{function:emit_answer}` + `reasoning:{enabled:false}`), so the
  figure/chart come back as validated args (no fence to mangle → no leak, and the
  figure SHIPS). Reasoning-off is REQUIRED: forcing a specific function is rejected
  while thinking is enabled (400 *"Thinking mode does not support this
  tool_choice"*), and the draft turn already did the reasoning. It **FAILS OPEN** →
  `_forced_emit` returns None → the **`_EMIT_REPROMPT` nudge** + fence path. Bounded
  once per turn (`emit_reprompted`). **Clarify is handled FIRST** — a single-function
  `tool_choice` can't target "emit_answer OR ask_clarification", so forcing emit must
  never clobber a clarification. Records `emit_mode="forced"` (counts as structured;
  measures how often the force was NEEDED). `chat_completion` gained per-call
  `tool_choice`/`reasoning` overrides for this.
  A **leak scrubber** (`_scrub_leaked_blocks`) runs on the FINAL answer of both
  terminal paths and STRIPS any residual figure/chart-shaped JSON a mangled fence
  left in the prose — **whatever the wrapping**, keyed off the object SHAPE (figure =
  `value`+`label`, chart = `type`+`data`), so a novel mangle is caught too; a proper
  ```chart fence is preserved (fenced segments skipped whole). The fence-path
  fallback fires ~30% of the time live on a cheap/fast model, so this net matters in
  practice. `usage_log.answer_leaked` records debris **caught and removed** (never
  shipped); with `emit_mode` (structured|fence, migration 24) it drives the
  **Answer-leaks** scrub-rate stat on Admin → Usage (`leakRate`/`leakLabel`). Clarify
  paths are NOT scrubbed (no figure/chart by contract). The **number stays
  model-supplied** (envelope only); server-computed figures from declared provenance
  is the next step. Pinned in `test_agent_loop.py` + `test_llmhttp.py` +
  `test_admin_router.py` + `test_migrations.py`.
- **Disambiguation (clarify).** Prompt INSTRUCTIONS' leading "Before you answer"
  step: when a plausible alternate reading would change the HEADLINE result (e.g.
  "which major produces the most graduates?" — bachelor's-only vs. all award
  levels can crown a different program), the model does NOT query — it asks ONE
  short clarifying question and emits a ```clarify `{"question":"...",
  "options":["<short phrase>",...]}` fence (2–4 SHORT answer phrases, not
  restated questions). `llm.py`'s `_extract_clarify` parses + ALWAYS strips the
  fence (mirrors `_extract_figure`), and when a clarify is found `stream_agent`
  yields `{"type":"clarify",…}` then the answer, sets NO figure/suggestions, and
  **skips the critic entirely** — a clarify turn has no data claim to
  sanity-check. Persisted on `messages.clarify` (migration 20) so a reload shows
  the same question + chips; deliberately **no `query_cache.clarify` column** — a
  clarify turn is **never cached** and **records no critic lesson**
  (`chat.py` guards both on `clarify is None`). Frontend: `Clarify.jsx` (pure
  `clarify.js` normalizer, vitest) renders the answer-phrase chips
  structurally identical to `Suggestions.jsx` but with a **louder accent-FILLED
  treatment** (UX-H2: `.clarify` chips are accent-tinted/filled, the label in the
  accent color) — a clarify is a REQUIRED decision that blocks the answer, not the
  optional "you might also ask" exploration the identical outline chip read as; the
  distinction is shape+fill, not colour alone (the "Did you mean" heading already
  differs). Clicking one — or just typing a free-text reply in the composer, always
  the escape hatch — submits it as an ordinary follow-up turn. When ambiguity is NOT material, the prompt instead
  has the model answer under the most reasonable assumption, name it in the
  method line, and offer the alternate reading as a `followups` chip; a scope
  established earlier in the thread (award level, year range, institution/state
  set, program grouping) carries forward on later turns unless the user changes
  it. Pinned in `frontend/e2e/clarify.spec.js` + `backend/tests/test_agent_loop.py`
  / `test_chat_router.py` / `test_migrations.py`.
- The **signature "figure"** — a typeset hero statistic (mono caption · big serif
  number · ochre rule · mono source) rendered ABOVE an answer. Prompt INSTRUCTIONS
  **step 6** leads with a figure on BOTH kinds of answer (the trigger is prompt-only;
  no code gates the figure by query type). **(i)** When the answer's headline IS a
  single number, it builds the full **BRIEF**: (a) the ```figure fence, (b) a 1–2
  sentence synopsis, (c) a recent-years breakdown table (constant-bound `year >
  (SELECT MAX(year)-5 …)`), and (d) a ```chart trend — the story behind the number,
  not just one point. **(ii)** When the answer is a **trend / ranking / top-N list /
  multi-row comparison** (which already carries its own table/chart), it STILL leads
  with a figure carrying a **derived** hero stat + one insight sentence — a net %
  change over a time range, a leader's value or its share of the total, an average, or
  a max/min — chosen to fit the query; no second table/trend is bolted on. The figure
  is **omitted only** when no single number honestly summarizes the result (a plain
  lookup — address/URL/accreditor — or a tiny two-row fact). The model emits a
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
  some models emit the latter.) The brief applies on **follow-up turns too**, but
  prompt wording alone can't carry it: figure emission **decays with conversation
  DEPTH** — the system prompt must stay FIRST to remain the cacheable prefix, so its
  rules sit behind ever more history, and reword/compress/model-swap experiments all
  under-delivered (a compressed step 6 even broke the FORMAT — correct JSON,
  mis-wrapped). The fix is STRUCTURAL — three guards in `llm.py`:
  (1) **`_TURN_REMINDER`** — a short pointer back to steps 6/7 injected as a
  `system` message **after the history and immediately before the question**, on
  follow-up turns only. Built per request, never persisted, and it must never move
  ahead of the system prefix (that collapses cache reuse) — pinned by
  `test_followup_turn_gets_a_tail_reminder_after_the_cached_prefix`.
  (2) `_extract_figure`'s **mis-wrap fallback** (recovers a bare `{value,label}`
  object at the answer's HEAD, behind an optional stray `[..](..)`; head-scoped so a
  ```chart fence or mid-prose object is never mistaken for a figure) plus
  **`_normalize_misfenced_blocks`** (runs BEFORE extraction; repairs a figure/chart
  emitted as MARKDOWN IMAGE syntax — `![figure]\n{json}` — into real
  ```figure/```chart fences via a balanced-brace scan, firing only when the label is
  followed by a JSON object, so a genuine `![alt](image.png)` is untouched).
  Otherwise that raw JSON leaks (charts have no other net) and can DUPLICATE a
  retry-recovered figure. Pinned in `test_agent_loop.py`.
  (3) A **missing-figure retry** (`retry_missing_figure` + `_maybe_retry_figure`,
  gated `FIGURE_RETRY_ENABLED`, modeled on the critic: own call, fails open): when a
  data-backed answer that should lead with a figure emits none (`_figure_required` —
  has SQL, has a digit, no clarify/error, OR a no-SQL turn with prior results to
  ground against), ONE targeted call asks for ONLY the ```figure fence — a narrower
  ask than re-obeying step 6, which is why it works. A recovered figure is
  **grounded before it ships**: reproducible → kept, derivation tagged **`retry:`**;
  **ungrounded → SUPPRESSED** (`retry:suppressed`) — a forced figure not in the data
  is an induced hallucination, the ONE place a figure is suppressed rather than
  shipped (first-pass ungrounded figures still ship observe-only, #163). **If you
  touch step 6, the reminder, or the retry, re-measure `figure_grounding` before and
  after** — emission is prompt-compliance behaviour and prompt fixes have repeatedly
  under-delivered; `retry:`-prefixed derivations in `usage_log` mark what the retry
  recovered. A brief's
  **table + trend chart render side by side** (`briefdata.js` pairs one-table +
  one-chart → `Markdown.jsx` passes the chart into the table component and suppresses
  the standalone fence; drops the redundant "Chart this"). To hand the chart room,
  the side-by-side table is **capped** (`.brief-figrow:not(.stacked) .table-block {
  max-width: min(360px,100%) }`, `overflow-x: visible`) so a wide table **shrinks and
  WRAPS its multi-word headers** (`.md th` wrapping; data cells stay nowrap) instead
  of taking full width — a `flex`/max-width-on-cell alone won't force this when the
  row has room. `.brief-figrow` **wraps to stacked on a narrow viewport**, AND a
  **wider or taller table (`headers.length > 3 || rows.length > 8`) is forced
  `.stacked`** — chart BELOW the full-width table, since a bigger table can't share a
  row without its nowrap cells sliding UNDER the chart (only the brief's compact
  recent-years strip — a couple of columns, a handful of rows — sits side-by-side;
  the earlier `> 4`-columns-only threshold let a 4-column ranking table overlap the
  chart). Pinned in `frontend/e2e/answer-figure.spec.js`.
  **A reproduced figure is marked "✓ verified"** on its source line (S6). The
  server already graded every figure, but the verdict lived only on `usage_log`,
  so the person reading the number learned nothing. `messages.figure_grounding`
  (migration 31, STATUS only) + the `done` SSE event carry it; `figure.js`'s
  vitest-pinned `isFigureVerified` decides, and `Figure.jsx` renders the mark
  (in the `aria-label` too — a sighted-only trust signal would be the wrong kind
  of quiet). **POSITIVE-ONLY BY DESIGN, and the asymmetry is the contract**: an
  ungrounded figure renders NO mark and NO warning. The kernel is observe-only
  precisely because it has produced false negatives (#212 was a CORRECT figure
  graded `ungrounded`), and a missing mark costs a little trust while a warning on
  a correct number destroys it. **Don't confuse `figure_grounding` with
  `figure_derivation`**: the former is only ever a BARE status
  (`exact`/`rounded`/`derived`/`ungrounded`/…—every assignment in `llm.py` is
  `check.status` or a constant); the latter is the composed provenance string
  (`retry:ctx:sum(q3.awards)`) and stays backend-only telemetry. Writing prefix
  parsing into the frontend predicate models a shape that never occurs.
  The **chart toolbar is compact** so it fits a
  narrow side-by-side chart without overflowing: a single **`<select>`** collapses
  Line / Bar / **Line + trend** (trend is a line subtype, offered whenever the data is
  **trend-eligible** — a single numeric time-series with ≥3 points — **independent of
  the current type**, so "Line + trend" stays selectable while "Bar" is active; the
  fitted line only draws on a line chart). **Data labels** + **Copy image** +
  **Maximize** are **icon-only** buttons (tooltip on hover; `IconCopy`→`IconCheck` on
  copy). **Maximize** (`IconMaximize`) opens `ChartModal.jsx` — the same chart at
  large size in a dialog (reuses the `ConfirmModal` a11y pattern: focus-in/trap,
  Escape/overlay/Close, background `inert`, focus returns to the opener); the inner
  `<Chart inModal>` hides its own maximize control and carries the opener's current
  type/trend/labels via `initial*` props (Chart ↔ ChartModal is an intentional cyclic
  import, resolved at render time). A long chart **title wraps to 2 lines**
  (`wrapLabel`) so a narrow chart doesn't clip it, while the wide PNG export keeps one
  line. `.chart-head` wraps rather than overflowing. **`role="img"` sits on the inner
  `.chart-graphic`, NEVER on the outer `<figure>`** — ARIA's presentational-children
  rule strips every descendant of a `role="img"` from the a11y tree, so on the figure
  it hid the whole toolbar (type select, delta badge, labels/copy/maximize) from
  assistive tech while leaving it on screen. **Playwright's role engine does not prune
  presentational children**, so `getByRole` found the controls and the toolbar specs
  passed the entire time it was broken; the regression test therefore asserts
  **containment** (`[role="img"] .chart-head` → 0) in `chat-happy-path.spec.js`, not
  role. Treat "pinned by e2e" with suspicion for a11y semantics specifically.
- **The analyst layer** on top of the brief:
  - **Trend line + %-change** — `Chart.jsx` overlays a least-squares fit (a computed
    `__trend` `<Line>`, dashed ochre, injected into `chartChildren()` so it flows to
    the PNG export too; kept out of `keys` → no label/legend) and a **delta badge**
    (`▲/▼ X%` over the range, `--ok`/`--danger`) for a single-series line time-series.
    All client-side from the numeric chart data (`trendstats.js`, vitest) — accurate,
    no model dependency; the trend line is default-on via the chart-type control.
    **Both trend line AND delta are gated to a TIME-LIKE x-axis**
    (`/year|date|month|quarter|day/i`) — a
    "% change over the range" / fitted slope is meaningless across categorical
    entities, so a categorical bar (e.g. compare mode below) shows neither.
  - **Richer narrative + rank/share** — prompt step 6(b): direction/magnitude,
    peak/trough years, provisional-year flags, and (when meaningful) the figure's rank
    among peers or share of a national total (the model runs one extra query).
  - **"You might also ask" drill-down chips** — the model emits a ```followups
    fence on EVERY answered turn (step 7 is REQUIRED, not optional — only an
    off-topic/unanswerable turn skips it, so chips appear on every real answer, not
    just single-number briefs); `_extract_suggestions` parses+strips it (mirrors
    the figure) → `{"type":"suggestions",…}` event → `messages.suggestions` (migration
    15) + `query_cache.suggestions` (16). `Suggestions.jsx` (pure `suggestions.js`,
    vitest) renders chips below the actions row; clicking one `submit()`s it as a
    follow-up turn (which gets its own brief) — an exploration loop.
- **Compare mode** — pick 2–4 rows from any result table and **instantly** chart just
  those rows, client-side, from the numbers ALREADY in the table (no new query, no
  backend, no persistence). Gated to a **comparable (categorical) table** — one where
  `chartSpecFromTable` infers `type: "bar"` (entity rows: universities/states/…),
  never a year-over-year trend table. Pure logic in `compare.js` (vitest):
  `comparableTable(headers, rows)` (reuses `chartSpecFromTable`'s entity-column
  inference — `spec.x`) and `compareSpec(spec, selectedLabels)` (filters the parent
  spec's data to the selected entities, forces a bar snapshot). `Markdown.jsx`'s
  `SortableTable` renders the leading checkbox **inline in its own row map**, with
  selection keyed by the entity LABEL rather than a row index — so a tick survives a
  re-sort, which is the whole reason for the label key. (The earlier react-markdown
  `tr` override + per-table `CompareContext` are **gone**; don't go looking for them,
  and see `Markdown.jsx`'s comment at the `SortableTable` definition.) A "Compare N →" bar
  appears once ≥1 row is ticked (action enables at 2, capped at 4), rendering the
  snapshot `<Chart>` in a `.compare-panel`. `Chart.jsx` renders **every** categorical
  tick (`interval={0}`) and **wraps** long labels onto multi-line centered ticks
  (`wrapLabel`/`WrapTick`) — Recharts otherwise silently DROPS colliding ticks, so a
  long-named bar (e.g. "Texas A&M University–College Station") would go unlabeled.
  Browser truth in `frontend/e2e/compare.spec.js`.

### Self-learning & cache
- **Lessons** — a short generalized **headline** + a longer generalized
  **description** (collapsible in the admin UI) + a commented SQL worked example.
  Retrieved as guidance at query time, from **two sources**, both feeding the same
  unverified pool: the **critic** (`app/critic.py`) mines the MODEL's own mistake
  — when it catches one it phrases it as a headline+description in one call,
  reused as both the revision feedback and the stored lesson
  **A REJECTION IS NOW REMEMBERED, AND SUPPRESSION HAS A FIXED ORDER.**
  Rejecting a lesson was a hard `DELETE FROM skills` leaving no trace, and
  `_find_duplicate` can only match rows that still exist — so every rejection
  erased the very evidence that would have suppressed the next proposal, which
  is why the same lesson came back forever. **Migration 35** adds
  `skills.category`, a `lesson_rejections` tombstone table (headline,
  description, embedding, category, `was_verified`, `hits`) and
  `meta['muted_lesson_categories']` (a JSON list, the `seed_lessons_applied`
  precedent — ≤7 elements and admin-mutable at runtime, so it is state, not
  config; corrupt JSON **fails OPEN**, since a corrupt marker should re-queue
  for review, never keep silently suppressing).
  `delete_skill` writes a tombstone before deleting — for **every** deletion,
  approved or queued, because retiring an approved rule also means "don't
  re-suggest this" — reusing the row's **stored** embedding rather than
  re-embedding (free, and it works when fastembed is down). It takes
  `?mute_category=1` so "Reject & mute" is **one atomic request**; chaining two
  calls can leave the delete done and the mute failed, and the mute is the whole
  point of the button.
  **`skill_id` on a tombstone is a non-unique provenance breadcrumb — NOTHING
  may key off it.** `skills.id` is `INTEGER PRIMARY KEY` with no AUTOINCREMENT,
  so SQLite reuses a freed id. An earlier implementation deleted prior
  tombstones sharing a `skill_id`, which **defeated the whole feature**:
  rejecting a new lesson that inherited a reused id erased a genuinely
  different earlier lesson's tombstone, letting it be re-proposed forever. Two
  tombstones sharing a `skill_id` is expected. The tests learned the same
  lesson — they discriminate by **headline**, since these suites share one
  `app.db` for the whole file (a `skill_id` filter matched every tombstone the
  file had ever created: measured `[8, 8, 8, 8, 8, 8, 8, 8, 8]`).
  **The order in `_upvote_or_save` IS the design**, both halves mutation-pinned:
  (1) muted-category gate, in `record_lesson_from_critic` **before any embed**
  (deterministic, and the only step that still works with embeddings
  unavailable) → (2) embed → (3) **tombstone check** → (4) the existing
  `_find_duplicate` same-source upvote check, **unchanged** → (5) the widened
  `_find_suppressor` → (6) save. Step 3 precedes step 4 or a rejected idea still
  inflates a pending row's upvotes. Step 5 **follows** step 4 because the two
  predicates are complements that can each match a *different* row for the same
  candidate — checking the wider net first would suppress on a verified match
  and never reach the same-source upvote, silently dropping the everyday "this
  rule came up again" signal the review queue runs on.
  **`_find_suppressor` is deliberately ASYMMETRIC** (`include_pending_other_source`):
  its **verified arm applies to every source** — an approved rule is already
  active in the prompt, so restating it adds nothing — while the
  **different-source-pending arm is critic-only**, because a user's correction
  and the model's own self-critique on the same scenario are *different
  evidence* and the queue should show both (pinned by
  `test_feedback_lesson_not_collapsed_into_a_critic_row_same_scenario`). The
  predicate needs `IFNULL(created_by,'') != ?` — `created_by` is nullable and
  `NULL != 'critic'` evaluates to NULL, not true, silently excluding every
  NULL-source row. Reuses `skill_dedup_threshold`; **no new setting**, which
  would only invite re-opening the hole.
  **Every suppression logs at INFO** naming the reason — suppression is
  invisible by construction (no row appears), so without the log a legitimate
  lesson vanishes with no trace and nobody can learn the feature over-reaches.
  Admin → Logs is already substring-searchable, so this needed no new UI.
  Admin → Skills gains a category pill, "Reject & mute", and collapsed
  "Rejected (N)" / "Muted categories (N)" sections with Allow-again/Unmute
  (**"Allow again", not "Undo"** — the endpoint deletes the tombstone and does
  NOT restore the lesson, and the visible text has to be a contiguous substring
  of the accessible name for WCAG 2.5.3); a
  rejections **load failure renders an error, never "Rejected (0)"** (the
  `deniedError` precedent). Pure logic in `admin/lessoncats.js` (vitest) —
  `categoryLabel` returns `""` for a NULL category, which is what stops a
  pre-migration row rendering "Reject and mute **undefined**".
  **Known limit:** rows queued before migration 35 have `category = NULL`, so
  "Reject & mute" isn't offered on them; clearing that backlog is a one-time
  manual pass.
  (`skills.record_lesson_from_critic`); the **feedback distiller**
  (`app/feedback.py`, `distill_feedback`) mines the USER's own corrective
  feedback on a follow-up turn ("you should have kept the bachelor's scope") the
  same shape, via `skills.record_lesson_from_feedback`
  (`created_by="user-feedback"`) — a cheap separate probe call, fails open exactly
  like the critic/guard, gated on `skills_enabled`, run only when `history` is
  non-empty (a first-turn question has no prior answer to correct). Lessons
  start **unverified → an admin approves**; deduped on save (scoped per-source, so
  a feedback candidate never collapses into a critic/seed row on the same
  scenario); the embedding key is **headline+description, never the question**.
  `SKILLS_ENABLED=0/1` gates the on/off eval A/B.
  **Shipped SEED lessons arrive per-lesson, and exactly once.** `app/seeds.py`'s
  `SEED_EXAMPLES` are inserted at boot by `skills.seed_from_schema_examples`,
  which used to bail whenever the `skills` table held **any** row — so a seed
  added in a later release reached **fresh installs only**: every existing
  deployment had rows (its original seeds, plus critic/feedback lessons), the
  gate was shut forever, and new exemplars silently never arrived. Found in the
  wild on 0.2.0 by Todd, whose upgraded deployment kept its original 3 while the
  image shipped 8. Each `SeedLesson` now carries a stable **`slug`** — its only
  durable identity, since headline/description/SQL all get rewritten
  (`SEED_LESSON_UPGRADES` exists because they have been) — and the slugs applied
  so far live in `meta.seed_lessons_applied`. Two consequences, both deliberate:
  an admin who **deletes** a seed from the Skills tab has made a decision the
  next boot respects (deriving "missing" from the table alone would resurrect it
  every restart), and a database that predates the marker is recognized by a
  **one-time backfill** matching each seed's headline **OR question** against
  existing `created_by='seed'` rows — `question` because no upgrade path has ever
  rewritten one, so a pre-migration-6 row with a NULL headline still matches and
  the backfill does not depend on `upgrade_seed_lessons` having run first.
  `save_skill` does **no** dedup of its own (that's `_upvote_or_save`, unverified
  same-source rows only), so the marker is the only thing between an upgrade and
  a pile of duplicate seeds. Pinned in `test_skills.py`.
- A **semantic answer cache** short-circuits repeat questions — **scoped to the
  user who asked** (migration 29's `query_cache.user_id`) and **bounded**.
  `cache_lookup` had no user predicate, so a colleague asking within
  `cache_similarity_threshold` (0.93) of your question was served *your* stored
  answer prose verbatim — the same attributable leak `/api/admin/usage` refuses
  to make by never returning question text. Rows written before the migration
  have `user_id` NULL and are reachable by **nobody** (fail closed, not
  shared-by-default); the sweep clears them. Accepted cost: a popular question is
  now answered once *per person*, not once per deployment. It also had **no bound
  of any kind** — the only DELETE anywhere was `invalidate_cache`'s wholesale wipe
  on a data import, while every first-turn question `vstack`s and matmuls the
  WHOLE table before the agent starts, so latency and memory grew with uptime
  forever. `_prune_cache` mirrors `logbuffer._prune` (`cache_retention_days` /
  `cache_max_rows`, non-positive disables, OFFSET-based row cap, incremental-vacuum
  reclaim) and runs opportunistically on the **write** path only — a read must
  stay cheap. Pinned in `test_skills.py`.
  **An APP UPGRADE also wipes it**, which nothing did before: a cached answer is
  a verbatim replay of prose an older build produced under an older
  `SCHEMA.md`/system prompt, so a code change can leave stored answers that are
  simply WRONG. Found while *verifying* #326 — that PR fixed a false award-level
  rule, and re-asking the question returned the pre-fix total from cache
  (`model_used='cache'`, no SQL events); at 30-day retention and 0.93 similarity
  the fix would have reached nobody who had already asked.
  `invalidate_cache_if_version_changed` (called from `lifespan`) compares
  `config.app_version` to the `meta` key `cache_app_version`. **A MISSING marker
  counts as changed** — every deployment upgrading INTO the release that adds
  this has no marker and a full cache, so reading "no marker" as "current" would
  make the feature miss its own first release, the exact bug
  `seed_from_schema_examples` shipped. Version-keyed, not content-keyed, on
  purpose: it wipes once per upgrade even when nothing relevant moved (one cache
  miss per question), because a needless miss costs a query while a stale hit
  ships a wrong answer. Fingerprinting `SCHEMA.md` + the prompt per row is the
  better design and is backlog. `app_version` is `"dev"` locally, so dev wipes
  at most once.
  **A cache hit carries its own evidence** (`query_cache.results` +
  `results_truncated`, migration 31). It used to store the answer but not the ROWS
  behind it, so the cached branch persisted `messages.results=NULL` and every
  LATER turn in that conversation had nothing to ground a recited number against —
  it silently graded `unchecked`, denting a rate the project steers by with no
  visible failure. The rows are legitimate evidence for that answer (the replayed
  prose is byte-identical to the turn that produced them), and they're already
  capped by `_results_for_storage`, so there's no new size risk. Deliberately NOT
  done: re-grading the cached figure on the hit — now possible, but it would move
  the Grounded-figures denominator, and that shouldn't shift inside a plumbing
  change. Pinned by `a cache hit keeps the conversation grounding chain intact`
  (`test_chat_router.py`), which asserts `_load_prior_results` can actually read
  them back — a non-NULL-column check would pass on a blob grounding can't parse.

### Auth & access control
- Passwordless **magic link**, manual **allowlist**, email via a **pluggable
  backend** (`mail_backend`: `auto`/`resend`/`smtp`/`console`) — Resend (hosted API,
  easy pilot) or the institution's own **SMTP** (Google/Microsoft/relay, stdlib
  `smtplib`), console-log in dev. One seam: `mailer.send_email` dispatches via
  `_resolve_backend`; a backend failure is swallowed (returns False, never 500s the
  login/approval). The Outlook-safe HTML templates are backend-agnostic. The
  allowlist is the **sole authority on sign-in**.
- **The sign-in link is built from the canonical `app_public_url`, NEVER
  `request.base_url`.** `mint_login_link` reads `get_settings().app_public_url`
  internally (no caller passes a base) — a request-derived base follows the
  attacker-controllable `Host` header, so an attacker could make the server email a
  victim a genuine signed link pointing at an attacker domain (link-poisoning →
  account takeover). Every email href is also HTML-attribute-escaped (`mailer.py`).
- **The token rides in the URL FRAGMENT (`/verify#token=…`), never a query
  string** — a security property, not a style choice. `/verify` is an SPA route
  served by `main.py`'s catch-all, so a `?token=` link wrote the raw single-use
  token into **uvicorn's access log on PAGE LOAD**, before any API call; anyone
  who could read `docker logs` (routine on a self-hosted box) could replay it
  into an account takeover. `logbuffer._REDACT_RE` could not help — it only
  scrubs what reaches `logs.db`, and uvicorn sets `propagate: False` on
  `uvicorn.access`. A fragment is **never transmitted to the server**, so it
  can't be logged by us, by the operator's reverse proxy, or by a tunnel — none
  of which we control. Three parts, all load-bearing: `mint_login_link` emits
  the fragment; the legacy scanner-bounce 303 (`routers/auth.py`) redirects to a
  fragment too (a `?token=` target would just move the leak to the redirected
  page load); and **`verify-info` is a POST** with the token in the body (the
  GET form is deleted — no email ever pointed at it). `Verify.jsx` reads
  `location.hash` **with a `location.search` fallback**, which is what keeps
  every link already sitting in an inbox working — without it the fix would be a
  lockout. `logbuffer.install_access_log_redaction()` scrubs `token=` from the
  **`uvicorn.access` logger only** (not root — `make up` prints the full link on
  purpose, the documented local sign-in path) and rewrites **`record.args`, not
  `record.msg`**: uvicorn logs a constant format string with the path in
  `args[2]`, so a msg-only filter passes a naive test and leaks every token.
  Pinned by `test_magic_link_token_never_appears_in_a_server_visible_url` +
  `test_verify_info_takes_the_token_in_a_body_not_a_query_string` +
  the access-log cases in `test_logbuffer.py` + the legacy-link case in
  `frontend/e2e/auth-verify.spec.js`.
- **Boot-time cookie-posture check.** `main._insecure_cookie_warning` logs a
  **CRITICAL** on startup when `app_public_url` is `https://` but `COOKIE_SECURE`
  is false (that combo serves an insecure cookie AND relaxes the CSRF loopback
  carve-out — `csrf.py` `allow_loopback=not cookie_secure`). Logged, not raised, so
  dev/tests aren't broken; a prod misconfig screams in stderr + the admin Logs tab.
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
  + a VML bulletproof `_button`: doctype/head, **full-bleed** `role=presentation`
  tables — a teal header band edge-to-edge, no centered card — Arial not
  `system-ui`) in the app's teal palette. The band carries the real **wordmark**
  (`_wordmark_html`: Column mark · mono "IPEDS" · gold rule · serif "Oracle"),
  whose icon ships as an **inline CID attachment** (`_LOGO_PNG`, base64-embedded —
  Gmail and Outlook both refuse `data:` images), attached by *both* transports:
  Resend's `attachments=[{content,content_id,…}]` and SMTP's `add_related(…,
  cid=…)` (which nests the HTML part inside a `multipart/related` — hence
  `msg.walk()`, not `iter_parts()`, in `test_mailer.py`). The PNG is
  cream-shaft/gold-caps on purpose: the app's teal shaft is invisible on the teal
  band. `mailer.py` is E501-exempt in `pyproject.toml` because the templates are
  legitimately long.
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
  by wall-clock (a synchronous provider call — Resend or SMTP — on only some
  branches was a measured 400×+ timing oracle). A residual sub-ms DB-local timing difference (denied/unknown
  skip the INSERT the allowlisted/pending branches do) is **accepted** — it doesn't
  isolate the sensitive states, and equalizing it would violate "store nothing on
  deny"; see `auth.request_login`'s docstring.
- **Dead auth rows are swept in-app, not by cron:** `auth.purge_expired_auth_rows`
  deletes consumed/expired `login_tokens` and past-expiry `sessions` — rows the code
  can never accept again, so removing them changes no behaviour (the lookup misses
  instead of failing the timestamp check: same 400, same message). It runs at boot
  (`main.lifespan`, non-fatal like the seeding steps) and at the top of
  `verify_login`, before the token is marked used. Deliberately **not** in
  `mint_login_link`: that runs on only one of `request_login`'s branches, so a DELETE
  there would make "allowlisted" measurably slower than "pending" — reopening the very
  timing oracle above. (`auth_request_attempts` has its own sweep in `ratelimit.py`.)
  Pinned by `test_signing_in_purges_dead_auth_rows_only` in `backend/tests/test_security.py`.
- **Per-IP rate-limit is spoof-resistant:** `POST /api/auth/request` is capped
  per-email and per-IP (`ratelimit.py`), but `X-Forwarded-For` is client-settable.
  `client_ip` trusts it only `TRUSTED_PROXY_COUNT` hops **from the right** (a
  trusted reverse proxy/tunnel appends the real peer); `0` (dev/CI default) ignores
  XFF and uses the socket peer. Set it to **`1`** in production behind a single
  proxy/tunnel hop (via `.env`); combine with `EMAIL_DOMAIN` to close the
  access-request-spam surface. **The app must be the ONLY interpreter of XFF:**
  uvicorn ships `proxy_headers=True` trusting loopback and rewrites
  `scope["client"]` from the header, which silently defeats
  `TRUSTED_PROXY_COUNT=0` behind any loopback-adjacent ingress (ssh -L,
  cloudflared, host-network nginx). `scripts/docker-entrypoint.sh` therefore runs
  uvicorn with **`--no-proxy-headers`** — keep it there, and pass it in any
  non-Docker deployment too.
- **Per-user chat throttle (SEC-3):** `POST /api/chat/stream` is gated only by
  `current_user`, so without a cap an allowlisted user's runaway loop/script could
  burn unbounded provider spend. `ratelimit.enforce_chat_rate_limit(user_id)` — a
  sliding window over `chat_request_attempts` (**migration 28**), the same app.db
  pattern as the auth limiter — caps turns per user per `chat_rate_window_seconds`
  (`chat_rate_max_per_user` default **30/60s**; a **non-positive max DISABLES** it,
  the off-switch for tests/self-hosters). Called at the top of `chat_stream` before
  any streaming/LLM work, so a 429 is a plain JSON error, not mid-SSE. **NOT pinned
  in `ci_env.sh`** (that would mask a future stream-heavy suite that forgets to pin):
  the modules that fire many turns pin `CHAT_RATE_MAX_PER_USER=0` at import
  (`test_chat_router`/`test_guard`/`test_security`); `test_rate_limit.py` sets a tight
  cap and owns the 429 contract.
- **A question is capped at `MAX_QUESTION_LEN` (4,000 chars).** `BodyLimitMiddleware`
  bounds the whole request at 10 MB, but under that ceiling an unbounded question is
  still written to `app.db` **twice** (the user message + `usage_log.question`) and
  billed as provider tokens. Enforced as a hand-raised **400 with a readable
  sentence**, matching `MAX_TITLE_LEN` — deliberately NOT `Field(max_length=…)`,
  whose 422 sends `detail` as a **LIST**. Mirrored client-side by the composer's
  `maxLength` (keep the two in sync), so the server cap is the backstop, not the UX.
  `authcopy.detailText` flattens a pydantic detail array anyway — FastAPI raises 422
  itself on any malformed body, and the raw array would reach the user as
  `[object Object]`, the same leak `ApiError` exists to end.
- **`GET /api/auth/me` reports the loaded collection years**, from ONE `ipeds_years()`
  probe that also derives `has_data` (so the two can never disagree). The chat empty
  state used to state the range as fact — "collection years 2019-20 through
  2024-25" — while every deployment picks its own years via Admin → Imports and
  `_years` is the only authority. The wording lives in the pure `years.js`
  (vitest-pinned): `year` is the **ending** year, so 2020 renders "2019-20", and a
  single loaded year reads "collection year 2024-25", never "X through X". Guard the
  empties before `Number()` — `Number(null)` is `0` and **finite**, so a naive
  `isFinite` check renders a missing bound as year zero ("-1-00 through 2024-25").
  Pinned in `years.test.js` + the empty-state describe in `chat-interactions.spec.js`
  (which asserts the text FOLLOWS the mocked bounds, so a re-hardcoded literal fails).
- **CSRF defense in depth:** the session cookie is `HttpOnly`+`Secure`+`SameSite=Lax`;
  on top of that a pure-ASGI `CSRFMiddleware` (`csrf.py`) refuses any state-changing
  request whose `Origin` matches neither the request `Host` nor `APP_PUBLIC_URL`.
  Origin-less/non-browser requests pass (SameSite still covers browsers); it's raw
  ASGI so it never buffers the chat SSE stream. In the **dev posture only** (insecure
  cookies) it also accepts loopback origins so the Vite dev-proxy (`changeOrigin`)
  works — production (Secure cookies) enforces strict same-origin.
- **A truncated table says so, and its sort admits its scope.** `run_sql` cuts at
  `sql_row_cap_model` (200) and `QueryResult.truncated` records it, but
  `to_storage` dropped that flag and `get_conversation` never selected `results`
  — so the browser had **no structured signal**, and a 200-row PAGE of an 834-row
  result was byte-identical on screen to a complete one. The only disclosure was
  whatever sentence the model remembered to write. Now plumbed: **migration 30**
  (`messages.results_truncated`), carried on the `done` SSE event so a live turn
  captions without a reload, and selected back on conversation load.
  `tabletruth.js` (pure, vitest) owns the wording. **The caption never states a
  total** — `row_count` is the count AFTER the cut and nothing runs a `COUNT(*)`,
  so "of 3,412" would be invented; it says "First 200 rows · the full result is
  larger". It is **scoped to single-table answers** via the same
  `countMarkdownTables(src) === 1 && messageId != null` gate that decides whether
  the CSV re-runs server-side, because attributing one of N results to one of N
  rendered tables is a heuristic that can pick wrong. **Sorting a truncated table
  is kept but warned**, in `--warn` tone naming the cap ("not a ranking of the
  full result") — the old note appeared only AFTER sorting, in 12px muted text,
  and counted the rows the MODEL transcribed, a number unrelated to both the cap
  and the true total. The CSV button now **names what it does**: `Download full
  result (CSV)` (server re-run at the 100k cap) vs `Download these N rows (CSV)`
  (the transcription). And `downloadServerCsv` **fetches into a blob** instead of
  clicking a bare `<a href>` with no `download` attribute — every error path
  (400/404/**429**/504) used to replace the chat view with a raw JSON page, and a
  slow export timing out is the likeliest failure. Pinned in
  `frontend/e2e/truncated-table.spec.js` + `tabletruth.test.js`.
  **The server re-run PROBES every candidate, and each probe is time-bounded**
  (`CSV_PROBE_TIMEOUT_SECONDS`, 3s). `_select_table_sql` runs each query in the
  answer's `sql_log` at `LIMIT 1` to find which one produced the table you saw —
  and `LIMIT 1` bounds the ROWS returned, never the WORK done, so an unbounded
  probe could burn the full 25s `sql_timeout_seconds`. `sql_log` records **every
  attempt the agent made, failures included**, so 5–8 candidates is routine and
  one export could hold a threadpool worker for two to three minutes. The
  **winning re-run keeps the full default budget** — it is the query the user
  actually asked for. Don't tune the probe timeout down: a probe timeout is
  swallowed by the candidate-skipping `except`, so an over-tight value turns a
  slow-but-valid table query into "No runnable query for this answer." → 400.
  **Numeric columns right-align.** `Markdown.jsx` already computed `numericByCol`
  (via `columnIsNumeric`) to pick a sort comparator; it now also puts `.num` on the
  matching `<th>`/`<td>`, so digits line up on the ones place and magnitudes are
  scannable down the column (`.num` already carried `tabular-nums lining-nums`). The
  CSS stays **`.md`-scoped** — `.num` is shared with the hero figure's big serif
  number, which is centred — and a `thead th` has `padding:0` and delegates to the
  `.th-sort` button, so the *button* is what has to move its content.
- **API errors are typed, and a failure is never silent or raw.** `api.js` threw a
  bare `Error` whose message was the **raw response body** and discarded the
  status, so four call sites re-implemented `JSON.parse(err.message).detail` and
  anything that forwarded `err.message` printed FastAPI's JSON braces at the user
  — an ordinary 429 reached the chat bubble as
  `⚠️ {"detail":"Too many requests…"}`. Now one **`ApiError {status, detail}`**,
  parsed once (and tolerant of a non-JSON body from a proxy). A **401 fires a
  single `setUnauthenticatedHandler` hook** — advisory, not authoritative: the
  handler **re-checks `/api/auth/me` before signing anyone out** (that endpoint is
  exempt from the hook so it can't recurse), and a burst of 401s collapses into
  one confirmation. Trusting the first 401 blindly logs a user out on any
  incidental one — it broke ~226 e2e specs when tried, and would have done the
  same to real users. `App.jsx` distinguishes **expired** (401) from
  **unreachable** (anything else) so a transient 500 doesn't read as "you've been
  logged out". User-facing wording lives in the pure `authcopy.js`
  (vitest-pinned, the `announce.js` split). A failed turn now renders as a
  **condition** — `.msg.assistant.failed`, a `--danger` left edge — not as prose
  that happens to start with an emoji. Admin **Usage / Logs / Skills** render a
  real error instead of "Loading…" forever, **"No log records."**, and "No
  lessons yet" — a load failure must never be indistinguishable from an empty
  result (the `deniedError` precedent, generalized). Pinned in
  `frontend/e2e/error-visibility.spec.js` + `authcopy.test.js`.
- **Security headers on every response:** a pure-ASGI `SecurityHeadersMiddleware`
  (`secheaders.py`, outermost so it stamps even the CSRF 403) sets a restrictive
  **CSP** (`script-src 'self'`, no `unsafe-inline`/`unsafe-eval`; `img-src 'self'
  data:` for chart export; `frame-ancestors 'none'`), plus `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`. The CSP is the
  **second line of defense** under the LLM-markdown render surface — that surface is
  safe today only because react-markdown emits no raw HTML (**no `rehype-raw`, default
  URL sanitizer intact — keep it that way**; a DOM-XSS review confirmed it clean).
- **Pre-auth request-body cap:** FastAPI parses a request body while resolving a
  route's parameters — **before** `solve_dependencies` evaluates
  `Depends(require_admin)`. So an *unauthenticated* POST to `/api/admin/import`
  had its whole multipart body parsed and spooled to the temp dir and only then
  got its 401 (measured: 64 MB in 0.17s; a loop fills a container's writable
  layer). `max_upload_mb` lives *inside* that handler, which never runs. A third
  pure-ASGI layer, `BodyLimitMiddleware` (`bodylimit.py`), refuses the body first:
  **`max_request_body_mb` (10 MB) on every request**, with `/api/admin/import`
  exempt up to `max_upload_mb` **only when a session cookie is PRESENT** —
  presence only, no signature check, no DB hit, no second copy of auth. Honest
  limits, stated in the module docstring: one junk `Cookie:` header, or any
  signed-in non-admin, still reaches the large tier. The three layers run
  **SecurityHeaders → CSRF → BodyLimit → router** (last added = outermost), so a
  cross-origin oversized POST is refused by CSRF having read zero bytes and the
  413 still gets its security headers — pinned by
  `test_the_413_carries_the_security_headers`. `MULTIPART_SLACK_MB` (8) keeps the
  *handler* the authoritative decider for a merely-over-cap upload, so
  `test_security.py`'s import-lock-leak assertion keeps its meaning; the
  middleware only catches a grossly oversized body. Pinned in
  `backend/tests/test_bodylimit.py` (the headline case asserts **413, not 401** —
  a 401 would prove the parser ran).
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
  fetch + integrate. **Redirects are followed by hand, not by httpx (SEC-6):** the
  client runs `follow_redirects=False` and `_validated_redirect_target` checks each
  hop's host+scheme is `https://NCES_HOST` **before** issuing the next request (a hop
  cap defuses loops), so an intermediate redirect can never make the server request
  an off-host/internal URL — the old final-host-only check (kept as defense-in-depth)
  ran only after that request was already sent. Applies to both `head_release` and
  the streamed `download_zip`.
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
- **The swap keeps no `.prev` copy.** `_activate_staging` moves live aside only
  so the two-step move is recoverable if the process dies between them; once
  staging is in place it **deletes** that copy. Nothing used to, so every import
  or year-removal left a full extra ~2 GB dataset on disk, forever. Safe with
  queries in flight — the swap is a rename, so an open connection holds the old
  INODE and unlinking removes the name, not the file. Likewise
  `db._prune_snapshots` caps pre-migration `app.db.pre-v<N>` snapshots at
  `SNAPSHOTS_KEPT` (2). **Scheduled backups are deliberately NOT the app's job**
  — the operator snapshots the bind-mounted volume or crons
  `scripts/backup_app_db.py`; the pre-migration snapshot is an upgrade safety
  net, not a backup, and an in-container run of that script needs
  `BACKUP_DIR=/data/backups` (uid 10001 cannot create its relative default).
- **A manual upload's own files are discarded too**, and `run_import` owns their
  whole lifetime from a `finally` — success or failure, mirroring
  `run_integrate`'s work dir. Previously only the router's `except` unlinked the
  streamed `UPLOAD_DIR` copy, so a SUCCESSFUL import leaked two full 1–3 GB
  Access files: that one, and the `<name>.accdb.bak` a same-year re-upload moves
  aside in `DATA_DIR`. The `DATA_DIR` copy itself stays — it is the loader's
  source and the superset guard reads it. Two guards, both mutation-verified.
  **`UPLOAD_DIR` and `DATA_DIR` are free-form settings that can name the same
  directory**, where the "upload" IS the loader's source, so `_discard_uploads`
  skips an alias — and skips anything it cannot PROVE, since a stray file beats
  deleting the dataset on a wrong guess. Aliasing is decided by
  `_same_file` (device+inode via `os.path.samestat`, which also catches a hard
  link no path normalisation would), whose three-valued answer is load-bearing:
  a MISSING path must answer **False, not None**, or the preflight-failure exit
  — where `data_dir` does not exist yet — would skip the delete and leak every
  upload. And the swap flag is set from **inside `_activate_staging` the instant
  the rename completes**, not from `build_check_swap`'s return: a clean return
  only proves the whole swap TAIL succeeded, and a raise in that tail left the
  flag False, so `_restore_all()` rolled the old `.accdb` back under a live
  database built from the new one.
- A rebuild (manual upload or NCES integrate) streams `scripts/build_ipeds_db.py`'s
  `##PROGRESS##` markers into a determinate rebuild-progress bar on the Imports tab.

### Admin → Usage (privacy)
`GET /api/admin/usage` returns **only aggregates** (totals / series / top_users)
and **deliberately never verbatim question text**. `usage_log.question` is still
written, but echoing it back would be an attributable privacy leak (the
caller-controlled `since`/`until` narrows the window; `top_users` names the user).
A sentinel test in `backend/tests/test_admin_router.py` pins this. The stat cards
are the totals + the observe-only integrity/telemetry rates: **Grounded figures**,
**Grounded cells**, **Answer leaks**, and **Exhausted** — a COUNT (not a rate) of
turns that burned the whole tool budget (`usage_log.exhaustion` NOT NULL), with a
`· N degraded` sub-label for those the S5 grounding gate degraded (see the agent-loop
exhaustion bullet above). A rising Exhausted count is the signal to lift
`LLM_MAX_TOOL_ITERS`.

**EVERY LLM call a turn causes is billed, not just the agent's.** `usage_log` used
to record only `stream_agent`'s usage, so three probes were invisible: the topical
**guard** (`guard.classify` — runs on EVERY question, *before* the answer cache and
before the agent), the **title** call, and the **feedback distiller**. The guard was
the serious one and an oversight rather than a decision — `Verdict` carried a bare
token count that reached `messages.tokens` on the refusal path and nowhere else, and
on the allowed path the verdict was never referenced again. Consequences worth
knowing: **an answer-cache hit is NOT free** (the guard ran before the lookup, so its
row carries that one call — `cache_hits` is unaffected, it counts rows), and a
**refusal row is real spend** (`model_used='guard'`). Three shapes, three
mechanisms, because two of them have no `AgentResult` to accumulate into: the agent
path does a plain `+=` onto `result`; refusal and cache-hit share
**`_guard_usage_kwargs`**; and the two probes that finish *after* `_persist` has
committed (title, distiller) add to the existing row via **`_add_usage`**
(`UPDATE … cost_estimated = MAX(…)`, so a later estimated call taints an otherwise
billed row and a later billed one can't clear the flag). **`_persist` therefore
returns a THIRD value, the `usage_log` row id.** Two things deliberately NOT done:
the title call is **not** moved ahead of `_persist` — that would put a network probe
in front of the only statement that saves the user's answer, trading data safety for
tidier accounting; and `first_call_*` is **never** touched by a probe, since it
isolates the AGENT's schema-prefix reuse and the guard's prompt is a different
prefix (polluting it would corrupt the Schema-cache stat). One **known gap, and it
is CANCELLATION — not the `result is None` branch** (an earlier comment there named
the wrong cause and has been corrected): when a client disconnects, `gen()` unwinds
at its current `yield`, so `_persist` never runs and the whole turn's spend is lost.
`result is None` is reached only via `stream_agent`'s no-API-key return, where
`guard.classify` short-circuits on the same setting and nothing was spent — every
other exit, transport errors included, yields a terminal `done` carrying the result.
**"Stop generating" is unaffected** (abandon-and-drain: the request completes and
bills). The fix, when worth doing: let the caller supply the `AgentResult` that
`stream_agent` mutates in place, so the (already cancellation-shielded) `finally`
has a live reference to bill from — with a did-we-already-persist guard, or a normal
turn bills twice. A row there would NOT distort `queries`: a cancelled turn is a real
question, unlike a title/feedback probe. The `priceable_turns` /
`estimated_turns` / `cost_warning` predicates dropped their `cached=0` clause as a
direct consequence — it was justified by "a cache hit and a guard refusal spend
nothing", which was never true of the guard and is no longer true of a hit; **tokens
spent** is the honest test, and both halves of the `cost_warning` probe are scoped
identically or a single guard-billed refusal could clear a warning for a window
whose agent turns all recorded `cost=0`. A shared **`llmhttp.Usage`** +
`from_response()` is the one extractor for all five probes (`Critique` and
`_FigureRetry` keep their flat fields and just populate from it — which is what made
adopting it behaviour-neutral, proven by `test_critic.py` passing untouched). Pinned
in `test_chat_router.py` (the three turn shapes + `_add_usage`), `test_guard.py`,
`test_feedback.py`, `test_admin_router.py`.

**Spend is two different numbers, and the tile says which.** `usage_log.cost` holds
either the provider's own per-request charge (OpenRouter reports `usage.cost`) or
**our list-price estimate** for a provider that reports none — DeepSeek direct, and
most self-hosted gateways. `llm.effective_cost` picks; the bill always wins.
**Cached-prefix tokens are priced separately** via `LLM_CACHE_READ_COST_PER_MTOK`:
they are a SUBSET of `prompt_tokens` (DeepSeek's `prompt_cache_hit_tokens +
prompt_cache_miss_tokens == prompt_tokens`), so the uncached count is a
subtraction — no new provider key to read, `llmhttp.cached_tokens()` already
normalizes both shapes. This matters because the app is cache-heavy **by
construction** (the whole schema rides every prompt; measured **77.7%** hit rate),
and a provider discounts a hit steeply (DeepSeek **50×**: $0.0028/M vs $0.140/M) —
so pricing every prompt token at the input rate over-stated spend **5.0×**, measured
against OpenRouter's own billed figure ($3.16 estimated vs $0.63 actual over 307
turns). Tiering takes that to ~1.5×. **It remains an ESTIMATE and an upper bound** —
list prices drift from what a vendor bills, and one price pair covers both
`MODEL_DEFAULT` and `MODEL_ESCALATION`. Two traps: `0` means **not configured**
(prompt tokens priced at the full input rate, reproducing the old number exactly),
**never "cache reads are free"** — reading it that way would silently UNDER-state
spend, the one direction a cost estimate must not err in; and the uncached count is
`max(0, …)`-clamped, since `cached_tokens()`'s `or` chain could otherwise drive it
negative and **credit** the turn. The setting is deliberately **NOT** part of
`admin.py`'s `prices_configured` — it never enables an estimate alone, so including
it could only suppress a true `cost_warning`. Provenance is per-row
(`usage_log.cost_estimated`, **migration 34**, stamped from the shared
`llm.cost_is_estimated`) because it **cannot be derived after the fact**: a
deployment that switches providers has both kinds of row in one window, which no
config-derived boolean can describe. The Usage tile marks an estimate with a leading
**`~`** and carries the split in its label (`Spend · 12 of 40 estimated` —
`usagestats.js`'s `spendEstimated`/`spendLabel`, vitest-pinned); absent counts render
**unmarked**, so an older backend or a fixture never has an "estimated" claim
invented for it. Historical rows keep the cost recorded at the time, so the spend
series **steps** on the day prices change — documented in `ADMIN_GUIDE.md`, not a
bug. Pinned in `test_agent_loop.py` (the `effective_cost`/`cost_is_estimated` block)
+ `test_admin_router.py` + `test_migrations.py`.

**Timezone + per-turn timing (viewer's browser tz EVERYWHERE — see
[[date-formatting-preference]]).** The `/usage` **series buckets in the VIEWER's
timezone**: the browser sends `?tz=<IANA>` (its resolved zone), and the endpoint
aggregates in Python with `zoneinfo` (SQLite can't do IANA zones), falling back to
the `TIMEZONE` config default (`config.resolve_tz`) — the chart's x-axis is
labeled "Time (EST)" (the viewer's short zone). Chat **stamps every user turn**
("2:47 PM EST") and shows **"Thought for N seconds"** under each answer — pure
frontend `Intl` (viewer tz) via `frontend/src/datetime.js`
(`formatStamp`/`shortZone`/`thoughtLabel`, vitest-pinned). The duration is
**server-measured wall-clock** persisted on `messages.duration_ms` (**migration
26**) + carried in the `done` SSE event — it can't come from timestamps because
`_persist` stamps the user AND assistant rows with one `now`. The `TIMEZONE`
`.env` setting is only the graph's if-not-specified fallback (the browser normally
provides its own).

### The persisted-answer field list is DERIVED, not remembered
Adding a displayable field to an assistant message used to mean hand-editing ~10
sites (migration · `_persist` signature · INSERT columns · values tuple · the `?`
count · the agent-path call · the cache-hit call · the `done` SSE dict ·
`get_conversation`'s SELECT · three spots in `Chat.jsx`). Nothing checked any
pair against another, and it **shipped two defects** — `results_truncated`
(missed in the SELECT) and `table_cells_matched` (missed on the live path).
The failure is **asymmetric**, which is why review kept missing it: the reload
path inherits new fields free (`Chat.jsx` spreads `...m`) while the live `done`
path enumerates by hand, so a miss renders CORRECTLY after a refresh and wrongly
only on the turn that produced it. **One hand-written tuple and two derivations**
now live in `routers/chat.py`: `MESSAGE_TURN_COLUMNS` (drives the INSERT, whose
`?` count is derived, never counted); `MESSAGE_READ_COLUMNS` (= turn columns
minus `_BACKEND_ONLY` `{results, tokens}`, drives the SELECT); and
**`DONE_EVENT_FIELDS`, itself derived by OPT-OUT** — read columns minus
`_OWN_STREAMED_EVENT` `{sql_log, thinking, figure, suggestions, clarify}` and
`_RENAMED_ON_DONE` `{model_used}` (it rides `done` as `"model"`). They are
asserted against the **actual `messages` schema** by
`test_every_persisted_turn_field_reaches_the_reader_and_the_done_event`. A new
migration column fails that test until it is wired up **or** explicitly excluded
with a reason: a reviewable act instead of a remembered one.

**`DONE_EVENT_FIELDS` used to be decorative** — hand-listed, with its only
reference in `backend/app/` being its own definition, and its "test" one constant
minus another (deleting a key from the hand-built dict left it green). Deriving
it flips the **default for the next field**: a new column rides `done`
automatically, and *not* riding it is the reviewed act. The trade, stated: a
genuinely reload-only column now needs an explicit exclusion or it silently
bloats every frame — moving the failure from "renders wrong live" (invisible;
shipped twice) to "sends more bytes" (visible in a payload).
`_OWN_STREAMED_EVENT` carries **two** reasons at once and both are load-bearing:
those fields already arrive as their own streamed events, **and** they are
exactly the columns `Chat.jsx`'s `hydrate()` JSON-parses — so one riding `done`
would hand the LIVE path a raw JSON string where reload hands the same field a
parsed object.

**Values come from ONE mapping, not two.** `_persist` returns a `_PersistResult`
NamedTuple whose `turn_values` is the dict that fed the INSERT, and
`_done_extras(turn_values)` — the single consumer of `DONE_EVENT_FIELDS` —
projects it onto the event. Reach `turn_values` **by attribute, never a 4th
positional slot**; that friction is the point. This closed a live divergence
already in the code: `_persist` NULLs the cell counts when `table_grounding` is
falsy while the old `done` dict sent them raw, so an ungrounded turn reported
`0` live and `null` on reload. Applied to the **three message-bearing paths**
(agent, cache hit, refusal); the **no-data and interrupted paths are excluded
STRUCTURALLY** — neither calls `_persist`, so neither has a `turn_values`, and a
`done` with no `message_id` describes a turn the client cannot attach anything
to. The cache hit still shows **neither grounding mark**, now expressed by what
that `_persist` call *persists* (grounding kwargs absent → `None`) rather than by
a second hand-written list.

Two things the test gets right on purpose: it runs inside the `TestClient`
context (the app's lifespan is what creates the schema — checking before it would
make the gate **vacuously pass**, the one failure mode a schema gate must not
have), and it populates *every* field at once (a figure AND a clarify never
co-occur in a real turn; this exercises the plumbing, and any field left None
would silently satisfy the "was it persisted?" check). The done-event half is now
pinned on **real payloads**: a streamed turn's actual `done` frame is compared
**field-by-field against the reload** of that same turn, plus a **scalars-only**
check that fires the moment a JSON-text column joins the derivation.
`_persist`'s keyword signature stays explicit — the readable, type-checkable
seam.

**The browser half is derived too.** `Chat.jsx` used to name each field three
times (a local, a read off the event, a key in the finalize merge) — a fourth
hand-enumerated site. `frontend/src/donefields.js` (pure, vitest)
`messageFieldsFromDone(ev)` takes every key **except** a documented `DONE_META_KEYS`
denylist (identity/plumbing/path markers/telemetry), skipping nulls. A
**denylist, not an allowlist**: an allowlist would re-create the two-lists
problem in the worst place, since nothing cross-checks JS against the Python
tuple. **Skipping is `== null`, never truthiness** — `table_cells_matched: 0` is
the caution for an answer where EVERY value failed to reproduce, the worst one to
drop. **Merge order at the finalize is load-bearing**: `results_truncated: false`
*before* the spread (a present value wins, an absent one keeps the default) and
every turn-owned key — `content`, `sql_log`, `figure`, `suggestions`, `clarify`,
`id`, `pending`, `error` — *after* it, so no future server field can clobber the
answer. Keys stay **snake_case**, which is the only reason every render site
works for both live and reloaded messages.

**Full details live in `CONTRIBUTING.md` and the README's Self-hosting section — read them, don't guess.**

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
over the pure-logic modules under test — the JS analogue of `coverage_check.sh`'s
per-`backend/app/`-module rule. The set is **derived from the filesystem** (any
`src/foo.js` with a co-located `src/foo.test.js`), so writing the test is the whole
opt-in and a tested module can't stay silently ungated. Browser-tested components
(`Chat.jsx`, `src/admin/*.jsx`, …) have no `*.test.js` and so stay out of the floor —
Playwright covers them. The derivation walks `src/` **recursively**; it must, or a
module in a subdirectory escapes the floor silently (see the `src/admin/` note above).
**Open a `HelpPopover` in e2e with `focus()` or `tap()`, never a bare
`click()`** — the component opens on hover AND focus while its `onClick`
**toggles**, so a mouse click on an already-open popover closes it, which is
that handler's intent and not a bug. **The click-swallow latch is armed from
`onPointerDown`, and the reason is the whole point of the component's history.**
It used to arm from `onFocus` behind `if (!open)`, which assumed focus arrives
before the wrapper's `mouseenter` has committed `setOpen(true)`. On a REAL touch
tap it does not: Chromium emits the compatibility mouse events first
(pointerdown → touchstart → mouseenter/mousedown → focus → click), so `open`
already read true, the latch never armed, and **every tap closed the popover it
had just opened** — with Admin → Usage telling the admin to "Hover or tap the
ⓘ". `pointerdown` lands before that compat `mouseenter`, so `open` is reliably
false there; `pointerType !== "mouse"` keeps a genuine mouse click toggling, and
the `!open` test is what lets a SECOND tap dismiss (arming on every touch
pointerdown would swallow that one too, and since it fires no new focus nothing
would ever clear the latch). Pinned by a `test.use({ hasTouch: true })` describe
in `csv-import.spec.js` whose assertions are **synchronous** past `closeSoon`'s
140 ms timer — an auto-retrying matcher would wait out a transiently-open
popover. **All eight earlier specs passed with this bug present**, including one
NAMED for the touch tap that staged `focus()` before `click()` and so armed the
latch cleanly; it has been replaced. Two lessons, both still live: **when a
flake has a candidate mechanism, construct the input that FORCES the bad branch
rather than sampling for it** (repetition proved nothing, while a throwaway
spec that hovered-then-clicked failed 5/5), and **a test named for a scenario it
cannot actually produce is worse than no test** — it reads as coverage.
**The axe gate (`frontend/e2e/a11y.spec.js`) fails on `critical` AND `serious`,
and now SCANS THE APP** — a rendered answer with its disclosures open, a
MID-STREAM answer, and all seven admin paths in **both themes** (19 scans).
It previously saw only Login and the EMPTY Chat state, i.e. the two
least-populated screens: every control the product is made of, and every
admin page, sat outside the gate. That is a COVERAGE hole, not a threshold
one — and it is how two whole classes of defect shipped past a green suite.
Widening it immediately found two `serious` violations on `main`: the hidden
PNG-export chart (`aria-hidden-focus` — recharts renders a focusable svg, so a
keyboard user could Tab into an invisible chart that announces nothing; fixed
with **`inert` AS WELL AS `aria-hidden`**, the pair being the point — one
removes it from the a11y tree, the other from the focus order), and an
`aria-label` on a **roleless `<span>`** in Admin → Skills
(`aria-prohibited-attr` — silently IGNORED, so the ▲/▼ vote counts reached a
screen reader as bare glyphs; replaced with `.sr-only` text rather than
`role="img"`, which prunes descendants). Two fixture rules the scans depend
on: mock admin lists with **CONTENT, not empty arrays** (an empty table
renders none of the elements whose contrast could be wrong — the WARNING log
level at 2.52:1 needed a WARNING record to exist), and the answer fixture must
carry a **CHART** (the shared table-only one left the chart defect outside the
gate even after the answer scan was added).
`critical`-only was not a strict threshold but a shaped blind spot: axe rates
colour-contrast, `aria-prohibited-attr`, `scrollable-region-focusable` and
`heading-order` as **`serious`**, i.e. the whole class this suite exists to catch
scored under the bar. Three scans: Login, Chat, and **Chat in the DARK theme as an
admin with an attention badge** — the light-theme non-admin scans structurally
could not render the elements whose contrast was broken. Two hard-won limits:
**(1)** the Login scan runs under `emulateMedia({reducedMotion:"reduce"})`, because
the door's figure gallery auto-advances every 5s through a .34s fade and axe
sampling mid-fade measures the *blended* colour — reporting 3.56:1 against text
that rests at 4.85:1. Scan resting pixels, not a transient frame. **(2)** axe files
a one-character element as **`incomplete`, not a violation** ("content is too short
to determine if it is actual text content"), so the count badge that sat at 2.43:1
on every admin's screen was invisible to the gate — and `incomplete` is not gatable
in general (it also holds the composer's deliberate 1:1 transparent-textarea
overlay). Contrast on such elements needs a **direct computed-style assertion**
(`contrastRatio()` in that spec measures resolved pixels, pinning readability
rather than a colour literal). `--on-fg` is the token for text on an `--accent`
fill; a hardcoded `#fff` there is the recurring bug.
**(3) axe only contrast-checks text INSIDE the viewport** —
`colorContrastEvaluate` opens with `if (!_isVisibleOnScreen(node)) return true`,
a PASS, not an incomplete. This app pins `html, body { overflow: hidden }` and
gives every screen its own inner scroller, so at Playwright's 1280×720 default
everything below the fold went unmeasured: **34% of text nodes on
`/admin/logs`**, and a real 4.44:1 violation sitting at y=767 that the scan
reported clean. The axe describe therefore sets **1280×2600**, and any new scan
needs it or it is theatre. Widening it also exposed a latent mid-animation flake
elsewhere, so **`reducedMotion: "reduce"` now applies to EVERY scan**, not just
Login's — that reasoning was never Login-specific, Login was just the only scan
close enough to the top of the page to be bitten.
**(4) `aria-prohibited-attr` returns `incomplete`, not a violation, whenever the
element has text content** — so `aria-label` on a roleless `<span>` (role
`generic`, where ARIA prohibits it) is never gated. Worse, Playwright's
`getByLabel` computes the name WITHOUT applying the role prohibition, so an e2e
assertion on it passes while screen readers ignore the attribute outright. Use
`.sr-only` text instead, as `Skills.jsx` does.
**One list, not two, for the backend suites:** `scripts/run_backend_suites.sh`
globs `backend/tests/test_*.py` and is called by BOTH `run_ci_local.sh` and CI's
backend job. It replaced a hand-kept array plus ~30 hand-written CI steps that had
drifted — `test_grounding.py` and `test_version.py` were in neither, running only
inside `coverage_check.sh`'s glob with output sent to `/dev/null`, so a grounding
failure read as "coverage gate failed". Adding a suite is now just adding the file;
`coverage_check.sh` replays a failing suite's output instead of discarding it.
Similarly `.env.example` is pinned against `config.Settings` in both directions by
`backend/tests/test_env_example.py`, and **`requirements.lock` is pinned against
`requirements.txt`** by `backend/tests/test_requirements_lock.py`: every direct
dependency must be locked, and the locked version must satisfy the declared floor.
Nothing installs `requirements.txt` — CI and the Dockerfile both install the
**lock** — so a raised floor with a stale lock is invisible drift that leaves every
check green while the suites exercise the version that did *not* change. Dependabot
does exactly this (it cannot run `pip-compile`): #253/#254 each raised a floor above
the pinned version and went fully green. Regenerate with
`pip-compile --generate-hashes --output-file=requirements.lock requirements.txt`
in the same PR that moves a floor.

**The npm side has no equivalent gate — `npm audit` is the check nothing runs.**
CI never invokes it, so a vulnerable transitive is invisible to a fully green
suite, and a dependabot title gives no hint: of three routine-looking npm PRs in
#276, **two were security fixes** (js-yaml 4.3.0→4.3.1 cleared a HIGH, postcss
8.5.19→8.5.26 a MODERATE) and a third HIGH (`brace-expansion`) was sitting
unreferenced by any of them. Run `npm audit` in `frontend/` on `main` **before
and after** any lockfile change and state the delta in the PR; the frontend
currently audits at **zero**, which is only a useful baseline if it is checked.
Two traps behind that: **`npm install` will NOT move a transitive that already
satisfies its parent's range** — js-yaml stayed on the vulnerable version
through a full `npm install --package-lock-only` and needed `npm update
js-yaml`, and a lockfile that still carries the advisory is byte-indistinguishable
from a fixed one at a glance — and **`ci.yml`'s Playwright container tag must
move with `@playwright/test`** (both at 1.62.1). #269 moved the package alone and
went green, because a PATCH pair happens to share a browser build; that is
exactly why the drift survives review until a bump where it does not.
**Prefer ONE PR when several dependabot PRs rewrite the same lockfile.** `main`
is `strict: true`, so each merge puts the rest behind and forces them to
regenerate that file against a moved base — #269/#273/#274 were combined into
#276 for the same reason #271 combined #267/#268.

**Run the full gate before pushing.** `scripts/run_ci_local.sh` reproduces all of
CI (a **gitleaks** secret scan + a **semgrep** SAST pass, each when the binary is on
`PATH`; ruff over `backend/app backend/tests scripts` + ESLint; the `frontend/`
**vitest** unit tests; the `backend/tests/` backend suites against a fixture DB;
Playwright e2e — run against a **prebuilt static bundle**, `E2E_PREVIEW=1`, which
is 3.4× faster over a full run than the dev server that re-transforms modules per
`page.goto`; reuse is off in that mode or the suite runs against **stale source
and reports a false green**). A `.githooks/pre-push` hook runs it automatically
(bypass: `git push --no-verify`; skip e2e: `SKIP_E2E=1`). A **deletion-only push** skips the
gate — it ships no code — while a push mixing a deletion with commits still runs it
(`test_pre_push_hook.py`). It's a **fast pre-check** so failures
surface before CI — but since the repo went public the **authoritative gate is
GitHub CI**: `main` is **branch-protected** (a PR is required; the required checks
must be green AND up to date before merge; force pushes and direct pushes are
blocked). The **secrets** job runs gitleaks over full history as defense-in-depth
under GitHub's native secret-scanning + push-protection (both enabled). Admin
override is left enabled only as a safety valve for a flaky check.

**Static analysis — two layers, complementary.** **CodeQL** (`.github/workflows/codeql.yml`,
`security-extended`, scoped to non-test code) runs on every PR/push and is the
authority on **cross-file taint** (its py/log-injection caught a request `tz` param
logged in another module — CodeQL alerts surface in the Security tab; NB they don't
block a merge unless code-scanning *merge protection* is enabled in repo settings).
The three `github/codeql-action/*` steps are pinned to an **exact patch**
(`@v4.37.4`, was the floating `@v4`) — a reviewable diff for every CodeQL change
instead of silently riding whatever the major tag moves to, at the cost of a
dependabot PR per patch release.
**Semgrep** (the CI **SAST (semgrep)** job + the local gate) is the fast pattern
layer — `p/python` · `p/security-audit` · `p/javascript` plus repo-local rules in
**`.semgrep/`** (a CWE-117 log-injection rule). It runs `--error` (any finding fails
the job) over `backend/app` · `frontend/src` · `scripts`. It is **NOT** a CodeQL
substitute — semgrep OSS does INTRA-file taint only, so cross-file flows stay
CodeQL's job; the two overlap deliberately. Install semgrep isolated from the app
venv (`pipx install semgrep`) so it never enters the app's runtime deps.

**Ship via branch → PR → merge on green.** You can't commit straight to `main`
(branch protection blocks it). Branch (`feat/…`, `fix/…`, `chore/…`, `docs/…`),
keep PRs focused (one item), open a PR, then **watch CI without blocking**: run
`gh pr checks <n> --watch` as a background task (`run_in_background`) and keep
working — the harness re-invokes you when it settles. Merge only when lint · unit ·
backend · e2e · image are all green. End commit messages with the `Co-Authored-By:`
trailer.

**A green PR is NOT an all-clear — check CodeQL separately, every time.**
Code-scanning alerts do **not** block a merge (merge protection is off), and the
`CodeQL` check going green means *the analysis ran*, not that it found nothing.
So a finding lands silently in the Security tab and stays there. After a merge:

```bash
gh api "repos/toddawhittaker/ipeds-oracle/code-scanning/alerts?state=open" \
  --jq '.[] | "\(.number)\t\(.rule.security_severity_level)\t\(.rule.id)\t\(.most_recent_instance.location.path):\(.most_recent_instance.location.start_line)"'
```

Todd had to point out alert #44 himself, several PRs after it appeared — the
whole point of the tool is that it catches what review doesn't, which is
worthless if nobody reads the queue. **Triage, don't just dismiss:** #44
(`py/url-redirection`) was genuinely not exploitable, and it was still worth
fixing rather than annotating away, because a queue with a permanent red item in
it trains you to stop looking. Probe an alert both ways before deciding — the
probe is what tells you whether you're patching a hole or hardening a
non-hole, and the answer belongs in the code comment.

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
image on every PR/`main` push (so a broken Dockerfile can't merge), but publishes
to GHCR **only on a `v*` git tag** — `:X.Y.Z` + `:X.Y` + `:latest` (metadata-action
strips the leading `v`, so the Docker tag is `0.1.0`, not `v0.1.0`). No rolling
`:edge`/`:sha` images are published (deliberate — release tags are the only
artifacts kept). Self-hosters run the published image
(`docker compose pull && docker compose up -d`, pin via `IPEDS_TAG`) — TLS is the
operator's own reverse proxy/tunnel or an optional self-signed cert
(`scripts/gen-selfsigned-cert.sh` + `SSL_CERTFILE`/`SSL_KEYFILE`, served by
`scripts/docker-entrypoint.sh`). Details in the README's **Self-hosting** section.

Two operator-visible defaults that read as breakage if you forget they are
deliberate. **`compose.yaml` publishes :8000 on LOOPBACK** (`BIND_ADDR`, default
`127.0.0.1`), which is a security control rather than a convenience: Docker
inserts published ports into its own iptables chain, which a host
`ufw`/`firewalld` policy does **not** filter, so `0.0.0.0` is reachable from the
network however the host firewall is set. A deployment reached by LAN address
therefore stops working after an upgrade until it sets `BIND_ADDR` explicitly.
And **the container runs as the numeric uid/gid 10001** with
`no-new-privileges` + `cap_drop: ALL`; Docker never chowns a bind mount, so
`/data` must be owned by it (`sudo chown -R 10001:10001 ./srv-data`, or override
`IPEDS_UID`/`IPEDS_GID`). `python -m app.startup_checks` runs from the
entrypoint BEFORE `exec uvicorn` and exits 1 with the exact command and the live
uid — it has to be a separate process, because `app/main.py` calls
`_install_logbuffer()` at IMPORT time and an unwritable data directory would
otherwise surface as `sqlite3.OperationalError` inside uvicorn's app import,
a traceback that never mentions ownership.

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
`CONTRIBUTING.md` / the README's **Self-hosting** section) in the *same* PR. **A major architecture or
infrastructure change — a new test tier, a new gate, a removed/renamed feature, a
changed workflow rule — must also trigger a sweep of `.claude/agents/`.** The
specialist definitions reference the tiers, features, and rules and go stale
silently (the vitest tier landed in #71 while the team still described the removed
👍/👎 feedback until the #72 sweep). Fold the sweep into the same PR when small,
else ship it as an immediate focused follow-up. These files must always reflect
the current state of the project.
