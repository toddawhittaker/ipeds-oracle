# Auth & access control
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
