# IPEDS project

A **private FastAPI + React web app** that answers natural-language questions
about U.S. colleges/universities (IPEDS = the U.S. Dept. of Education's census of
postsecondary institutions) for an institution's approved colleagues. A
DeepSeek-backed agent turns each question into SQL against the read-only IPEDS
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
  `data/*.accdb` files (`--dry-run` prints the table→family mapping).
- `CONTRIBUTING.md` — **dev handbook** (stack, local run, tests, lint, CI, agent
  team). `docs/` — `SCHEMA.md` (data model + query guide). Self-hosting lives in the README.
- `brand/` — the **IPEDS Oracle** identity: `icon.svg` (the Column mark — vector
  master) + the ImageMagick recipe that regenerates the favicons from it. The
  header/login **wordmark is inline SVG** (`frontend/src/Wordmark.jsx`, drawn from
  the theme tokens so light/dark comes from one source — mono "IPEDS" · ochre rule ·
  serif "Oracle" · Column), NOT a PNG pair. Product name = `PRODUCT_NAME` in
  `config.py` (feeds the API title + every email); the wordmark's accessible name is
  "IPEDS Oracle".

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
  the **Admin guide link is gated to `isAdmin`** (passed from `App.jsx`). Pinned in
  `frontend/e2e/user-menu.spec.js` + `initials.test.js`.
  Chat interaction contracts (all Playwright-pinned in
  `frontend/e2e/chat-interactions.spec.js`): **Stop generating is
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
  Auto-scroll **follows only while the viewer is near the bottom** (scrolled
  up = never yanked; a "Jump to latest" pill is the way back). Conversation
  switches show a skeleton, never the empty-state prompt. A printable key
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
  answer's hero figure is the most prominent number on screen and used to be the
  least verified: `_extract_figure` validated only its JSON *shape*, so a number
  the model mis-typed while transcribing a result table reached the user with
  nothing comparing it back to the rows. The check reproduces the figure's value
  from the turn's **retained** `QueryResult`s — verbatim, at the figure's own
  display rounding, or via the derivation menu prompt step 6(ii) actually asks
  for (`sum`/`mean`/`pct_change`/`share`/`max`/`min`) — and records
  `exact`/`rounded`/`derived`/`ungrounded` (plus the non-evidence
  `no_figure`/`unchecked`). Pure arithmetic: no DB, no LLM, no network, so it
  runs on every answer and needs no setting. **OBSERVE-ONLY — it alters no
  answer and blocks nothing**; it lands on `usage_log.figure_grounding`
  (migration 21) and surfaces as **Grounded figures** on Admin → Usage
  (`groundedFigureRate`, vitest-pinned), whose denominator counts *only* turns
  that had both a numeric figure and results to check it against — folding the
  no-figure majority in would peg the rate near 100% and quietly destroy the
  signal. Aggregations are barred over **dimension** columns
  (`year`/`unitid`/`cipcode`/… — `_DIMENSION_COL_RE`): a real collision found in
  testing had a genuine +25.0% awards trend "verified" as `share(year)`
  (2021/(2021+…+2024) = 24.98%, inside tolerance), and `year` is in nearly every
  IPEDS result. Retention is the foundation — `last_result` was overwritten per
  call, so a multi-query brief discarded the very result its headline came from;
  `AgentResult.results` now keeps them all, in call order. **Grounding is
  CONVERSATION-scoped, not just turn-scoped**: each turn's results are persisted
  (`messages.results`, migration 23, capped + backend-only) and the recent window
  is re-hydrated (`_load_prior_results`, same `before_id` semantics as
  `_load_history`) into `stream_agent(prior_results=…)`. A figure is checked
  against THIS turn's results FIRST, then the borrowed prior ones
  (`_ground_results`), so a follow-up that recites a number without re-querying —
  previously an unverifiable `unchecked` — now grounds against the earlier turn
  that produced it, tagged **`ctx:`** in `figure_derivation` (composes with
  `retry:` → `retry:ctx:pct_change(q3.x)`). Prior results are borrowed for
  grounding only, **never re-persisted** as this turn's own and **never fed to
  the model** (the prompt is unchanged — we verify recitation, we don't prevent
  it). This also relaxes the retry's `_figure_required` gate to fire on a no-SQL
  turn when prior results exist. Pinned in `backend/tests/test_grounding.py` +
  `test_agent_loop.py` + `test_chat_router.py`.
- a deterministic **table-grounding check** (`grounding.check_table`, same
  module, also **OBSERVE-ONLY**) — the results **table** is the model re-typing
  the query rows one-for-one, the densest block of numbers on screen and (like
  the figure once was) unverified. It parses the answer's GFM tables
  (`parse_markdown_tables`, header kept, skipping ```` ``` ````-fenced regions so
  a ```chart block isn't read as a table) and grades the **MEASURE columns only**
  — `_is_measure_column` excludes a **rank ordinal** (values are a pure 1..N
  sequence, whatever the header — "#"/"No."/"Rank") and any **dimension** column
  (header matches `is_dimension`: rank/year/unitid/cipcode/id/…). This keeps the
  rate a clean transcription-accuracy signal for the DATA rather than dragging it
  down with a model-added Rank column that was never in the DB (the live-test
  regression: a perfectly-transcribed top-5 table scored 5/10 because its five
  rank ordinals can't ground). Each graded cell is reconciled against THIS turn's
  retained results via the SAME kernel as the figure — extracted into the shared
  `_reconcile_value` (verbatim / display-rounded / derivable, dimension bar
  intact) — so a legitimately **computed measure** (a share/%-change column) still
  grounds instead of false-alarming, at the cost of the figure's known
  coincidental-match bias (acceptable observe-only: `messages.results` is
  persisted, so an all-columns variant is recomputable offline). Records a
  per-turn status (`matched`/`partial`/`unmatched`/`no_table`/`unchecked`) +
  numeric-cell counts on
  `usage_log.table_grounding`/`table_cells_checked`/`table_cells_matched`
  (**migration 25**; `no_table`/`unchecked` carry 0 counts so they self-exclude
  from the SUM-based rate), surfaced as a **cell-level** **Grounded cells** stat
  on Admin → Usage (`groundedTableRate`, vitest-pinned). Stamped in `llm.py`
  (`_stamp_table_grounding`) right after the figure stamp, on the FINAL settled
  answer. Pinned in `test_grounding.py` + `test_admin_router.py` +
  `test_migrations.py`.
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
  **The critic also runs on the TOOL-BUDGET-EXHAUSTED path** (S5): when the agent
  burns all `llm_max_tool_iters` without answering and falls back to the
  tools-disabled "best effort" synthesis, that answer used to ship with ZERO
  review (the highest-risk path, least checking). It now gets the same critic,
  and on a REVISE a **bounded correction round with tools RE-ENABLED**
  (`_CRITIC_CORRECTION_ITERS=3` — a deliberate, capped exception to the "no more
  tools" budget, fired only by a REVISE) so a flagged aggregation error can
  actually be re-queried and fixed. Re-enabling tools is what makes `requeried`
  meaningful again there — the SAME anti-leak gate applies, so a rebuttal or a
  confirm-only re-query reverts to the clean draft. Pinned by the `S5:` cases in
  `backend/tests/test_agent_loop.py`.
- **Structured emission** (`config.structured_emission_enabled`, PR-1 of the
  "structured output, not fenced text" work — **default OFF, dark-shipped**).
  The durable, model-agnostic fix behind #167's `_normalize_misfenced_blocks`
  band-aid: instead of free-typing ```figure/```chart/```followups/```clarify
  fences it can mangle, the model FINISHES a turn by calling an **`emit_answer`**
  (or **`ask_clarification`**) tool whose fields the *provider* validates.
  `llm.py` intercepts that call before dispatch and **reconstructs WELL-FORMED
  fences from the validated args** (`_reconstruct_answer` + `_fence` — the SERVER
  writes them, so they always parse), then falls into the SAME no-tool-call
  terminator — so `_extract_*` / critic / grounding / retry / persistence AND the
  **frontend are all unchanged** (figure/followups/clarify were already
  structured events; the chart stays a server-written ```chart fence the
  frontend already renders). A model that ignores the tool falls back to the
  fence path. **Adoption nudge (0.1):** a model that free-types a plain-text
  answer under structured mode is REJECTED once (`_EMIT_REPROMPT`, bounded by
  `emit_reprompted`) and told to call `emit_answer` — the same targeted-reprompt
  pattern that fixed missing figures. It fires before the clarify check (a
  free-typed clarify → `ask_clarification` too); a second free-type falls back to
  the fence path. **Measured 3/10 → 10/10 structured, 0 leaks over two runs — and
  a bonus: figure emission went to 10/10 too** (the figure is now a tool field
  the model fills, not a fence it forgets — this dissolves the earlier
  emission-decay saga). A **leak scrubber** (`_scrub_leaked_blocks`, evolved from
  the observe-only `_leak_flag` sentinel) runs on the FINAL answer of both
  terminal paths: it STRIPS any residual figure/chart-shaped JSON a mangled fence
  left in the prose — **whatever the wrapping** (a bare object, an
  `inline-code`-wrapped one, a stray `}}`) — so raw JSON never reaches the user.
  It's **model-agnostic**: it keys off the object SHAPE (figure = `value`+`label`,
  chart = `type`+`data`), not a per-model quirk, so a novel mangle is caught too;
  a proper ```chart fence is preserved (fenced segments are skipped whole). The
  fence path FALLBACK is exercised ~30% of the time live on DeepSeek flash (far
  more than the near-0 the tests suggested), and ~10% of those turns mangled the
  figure fence into inline-code JSON that the extractor missed (the observed
  conv-18 leak) — so this net matters in practice, not just for tool-incapable
  models. `usage_log.answer_leaked` now records that debris was **caught and
  removed** (never shipped) rather than shipped; with `emit_mode` (structured|
  fence, migration 24) it drives the **Answer-leaks** stat on Admin → Usage
  (`leakRate`/`leakLabel`) — now a scrub rate. (The clarify terminal paths are
  intentionally NOT scrubbed: a clarify turn carries no figure/chart by contract.) **`structured_emission_enabled` DEFAULTS ON
  (0.2)** — validated 100%-structured / 0-leaks across FOUR vendors
  (DeepSeek/MiniMax/Anthropic/Moonshot); the fence path is the retained fallback
  for a tool-incapable model (set the flag false to force it). The sentinel
  deliberately ignores a plain
  ```chart fence (that's the intended chart delivery — a false-positive caught in
  the PR-1 dark-ship run). The **number stays model-supplied** (envelope only);
  server-computed figures from declared provenance are the next step (PR-2).
  Pinned in `test_agent_loop.py` (structured + reprompt cases) +
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
  structurally identical to `Suggestions.jsx`; clicking one — or just typing a
  free-text reply in the composer, always the escape hatch — submits it as an
  ordinary follow-up turn. When ambiguity is NOT material, the prompt instead
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
  some models emit the latter.) The brief applies on **follow-up turns too** — never
  code-gated, but **the prompt wording carries the whole load, and the first version
  of it did not work**: measured over a 10-turn conversation, the figure appeared on
  1/1 first turns and **0/9 follow-ups**. `suggestions` survived the same
  strip-before-persist treatment on 11/13 turns, so the cause was not the stripped
  history — it was that step 6 was *conditional* ("whenever a single number can
  honestly capture…", plus a judgment-call SKIP clause) while step 7 was flatly
  REQUIRED. The model read a follow-up as a lighter conversational reply and took the
  hatch every time (one answer literally opened "I already have this at hand from the
  first query"). Step 6 now mirrors step 7's grammar — REQUIRED on every answered
  turn, with the skip narrowed to three enumerable cases (no number anywhere /
  couldn't answer / clarify turn) and the "already shown above" excuses named and
  refused. **That reword alone moved follow-up emission only 0/9 → 1/9.** A second
  measurement isolated the real driver: emission decays with conversation **DEPTH**,
  not question type — turn 6 ("how many nursing bachelor's nationally", structurally
  identical to turn 1) emitted nothing where turn 1 did. The system prompt must stay
  FIRST to remain the cacheable prefix, so by turn 10 its rules sit behind ten turns
  of conversation, and *rewording buried text does not make it less buried*. Hence
  `llm.py`'s **`_TURN_REMINDER`** — a short pointer back to steps 6/7 injected as a
  `system` message **after the history and immediately before the question**, on
  follow-up turns only (first turns already comply; the rules are still adjacent
  there). It is built per request and never persisted, so it cannot accumulate in
  history; and it must never move ahead of the system prefix, which would collapse
  cache reuse — pinned by
  `test_followup_turn_gets_a_tail_reminder_after_the_cached_prefix`. That took
  follow-up emission to **3/9 — a real improvement but NOT a fix**. Two further
  PROMPT experiments then FAILED and were abandoned: compressing step 6
  (42→20 lines, taxonomy moved to a FIGURE SHAPING section) **regressed to 0/10**
  — the model emitted correct figure JSON but MIS-WRAPPED (`[Figure: 767](767)`
  + bare object, no fence), so separating the requirement from its worked
  fence-in-context examples broke the FORMAT; and swapping flash→v4-pro made it
  *worse* at 3.4× cost. Conclusion (pre-registered): prompt wording is not the
  lever. **The fix is STRUCTURAL** — two guards in `llm.py`:
  (1) `_extract_figure` now has a **mis-wrap fallback** — after the fence/tag
  regex misses, it recovers a bare `{value,label}` object at the answer's HEAD
  (behind an optional stray `[..](..)` artifact), for zero LLM cost; scoped to
  the head so a ```chart fence or a mid-prose object is never mistaken for a
  figure. Related, **`_normalize_misfenced_blocks`** runs BEFORE extraction and
  repairs the model's other observed mis-wrap — a figure/chart emitted as
  MARKDOWN IMAGE syntax (`![figure]\n{json}`, `![chart]\n{json}`,
  `![Figure: 767](767) {json}`) — into real ```figure/```chart fences. Without
  it that raw JSON LEAKS into chat (charts have no other safety net) and, when
  the retry separately recovers the figure, DUPLICATES it. Balanced-brace scan
  (so a chart's nested `data:[…]` is captured whole), fires only when the label
  is actually followed by a JSON object (a genuine `![alt](image.png)` is
  untouched), scoped to the two block names. Pinned in `test_agent_loop.py`.
  (2) A **missing-figure retry** (`retry_missing_figure` +
  `_maybe_retry_figure`, gated `FIGURE_RETRY_ENABLED`, modeled on the critic:
  own call, fails open): when a data-backed answer that should lead with a figure
  emits none (`_figure_required` — has SQL, has a digit, no clarify/error), ONE
  targeted call asks for ONLY the ```figure fence — a far narrower ask than
  re-obeying step 6, which is why it works. A recovered figure is **grounded
  before it ships**: reproducible → kept, derivation tagged **`retry:`**;
  **ungrounded → SUPPRESSED** (`retry:suppressed`) — a figure we FORCED that
  isn't in the data is an induced hallucination, worse than the honest absence,
  the ONE place figures are suppressed rather than shipped (first-pass ungrounded
  figures still ship, observe-only per #163). Measured 4/10→**5–7/10** across two
  runs, every shipped figure grounded, the suppress path confirmed firing. The
  RESIDUAL gap is turns that run **no SQL** and recite from conversation context
  (deep follow-ups): `_figure_required` skips them (no fresh results to ground
  against) — closing that needs **conversation-scoped retention**, which would
  both ground context-recited numbers AND make them retry-eligible. **If you
  touch step 6, the reminder, or the retry, re-measure `figure_grounding` before
  and after** — emission is prompt-compliance behaviour and three prompt fixes
  already under-delivered; `retry:`-prefixed derivations in `usage_log` mark what
  the retry recovered. A brief's
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
  chart). Pinned in `frontend/e2e/answer-figure.spec.js`. The **chart toolbar is compact** so it fits a
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
  line. `.chart-head` wraps rather than overflowing.
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
  spec's data to the selected entities, forces a bar snapshot). `Markdown.jsx` injects
  a leading checkbox column into comparable tables via a react-markdown `tr` override +
  a per-table `CompareContext` (selection keyed by entity-label text, so each row
  self-identifies from its own hast node — no row-index plumbing); a "Compare N →" bar
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
- A **semantic answer cache** short-circuits repeat questions.

### Auth & access control
- Passwordless **magic link**, manual **allowlist**, email via a **pluggable
  backend** (`mail_backend`: `auto`/`resend`/`smtp`/`console`) — Resend (hosted API,
  easy pilot) or the institution's own **SMTP** (Google/Microsoft/relay, stdlib
  `smtplib`), console-log in dev. One seam: `mailer.send_email` dispatches via
  `_resolve_backend`; a backend failure is swallowed (returns False, never 500s the
  login/approval). The Outlook-safe HTML templates are backend-agnostic. The
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
  access-request-spam surface.
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
over an explicit **allowlist** of the pure-logic modules under test — the JS
analogue of `coverage_check.sh`'s per-`backend/app/`-module rule. Add a module to that
list when (and only when) it gets real unit tests, so JS logic never silently
escapes a gate. Browser-tested components (`Chat.jsx`, `Admin.jsx`, …) are
deliberately not in the floor — Playwright covers them.

**Run the full gate before pushing.** `scripts/run_ci_local.sh` reproduces all of
CI (a **gitleaks** secret scan when the binary is on `PATH`; ruff over `backend/app backend/tests scripts` + ESLint; the `frontend/` **vitest** unit tests; the
`backend/tests/` backend suites against a fixture DB; Playwright e2e). A
`.githooks/pre-push` hook runs it automatically (bypass: `git push --no-verify`;
skip e2e: `SKIP_E2E=1`). It's a **fast pre-check** so failures surface before CI —
but since the repo went public the **authoritative gate is GitHub CI**: `main` is
**branch-protected** (a PR is required; all of secrets · lint · unit · backend · e2e ·
image must be green AND up to date before merge; force pushes and direct pushes are
blocked). The **secrets** job runs gitleaks over full history as defense-in-depth
under GitHub's native secret-scanning + push-protection (both enabled). Admin
override is left enabled only as a safety valve for a flaky check.

**Ship via branch → PR → merge on green.** You can't commit straight to `main`
(branch protection blocks it). Branch (`feat/…`, `fix/…`, `chore/…`, `docs/…`),
keep PRs focused (one item), open a PR, then **watch CI without blocking**: run
`gh pr checks <n> --watch` as a background task (`run_in_background`) and keep
working — the harness re-invokes you when it settles. Merge only when lint · unit ·
backend · e2e · image are all green. End commit messages with the `Co-Authored-By:`
trailer.

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
