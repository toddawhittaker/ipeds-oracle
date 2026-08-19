# Architecture

How the app is put together: where the code lives, what the stack is, and how a
persisted answer travels from the agent to the browser. The other architecture
documents are:

- [`AGENT_LOOP.md`](AGENT_LOOP.md) — the LLM agent loop, its three guards,
  grounding, structured emission, the figure/brief, and the self-learning
  lessons + answer cache.
- [`AUTH_AND_SECURITY.md`](AUTH_AND_SECURITY.md) — magic-link auth, the
  allowlist, rate limits, CSRF/CSP/body limits.
- [`ADMIN.md`](ADMIN.md) — the Imports and Usage admin areas.
- [`DATASET.md`](DATASET.md) — `ipeds.db` itself and the query gotchas.
- [`SCHEMA.md`](SCHEMA.md) — the authoritative data model and query guide.

## Repository layout
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

## Stack & data stores
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

## The persisted-answer field list is DERIVED, not remembered
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
