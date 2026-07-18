// Shared /api/** route-mocking helpers for the Playwright e2e suite.
//
// The React app (frontend/src/*) is driven for real through a Playwright webServer
// (Vite dev, see playwright.config.js); nothing here talks to a live backend.
// Every helper takes the Playwright `page` and installs a `page.route(...)`
// interceptor that fulfills a canned response, so specs stay deterministic
// with no LLM_API_KEY and no ipeds.db.
//
// Contracts mirrored here come from frontend/src/api.js and frontend/src/Chat.jsx.

/**
 * GET /api/auth/me -> 200 {email,is_admin,has_data} when signed in, or 401
 * (logged out) when user is null.
 *
 * `has_data` defaults to `true` when the caller's `user` object doesn't
 * specify it, so every existing spec (written before the no-data/onboarding
 * feature existed) keeps rendering Chat/Admin normally without having to be
 * touched. Pass `has_data: false` explicitly to exercise the fresh-deploy
 * no-data state (see web/e2e/no-data-onboarding.spec.js).
 *
 * `trust_llm_provider` is likewise absent (falsy) by default, so the chat
 * privacy warning shows unless a spec passes `trust_llm_provider: true` to
 * exercise the trusted-deployment suppression (see chat-empty-state.spec.js).
 */
export async function mockMe(page, user) {
  await page.route("**/api/auth/me", async (route) => {
    if (user == null) {
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ detail: "unauthorized" }),
      });
    } else {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ has_data: true, ...user }),
      });
    }
  });
}

/**
 * GET /api/auth/config -> {email_domain}. Unauthenticated; Login.jsx polls
 * this on mount to build its "you@<domain>" placeholder hint (falling back to
 * the generic FALLBACK_HINT when email_domain is empty or the call fails).
 */
export async function mockAuthConfig(page, emailDomain = "") {
  await page.route("**/api/auth/config", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ email_domain: emailDomain }),
    });
  });
}

/** POST /api/auth/request {email} -> 200 {message}. */
export async function mockRequestLink(page, message = "Check your email for a sign-in link.") {
  await page.route("**/api/auth/request", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ message }),
    });
  });
}

/**
 * GET /api/auth/verify-info?token=… -> 200 {email} (non-consuming peek) or a
 * 4xx {detail} for an invalid/expired token. Drives the /verify confirm page.
 */
export async function mockVerifyInfo(page, email, { status = 200 } = {}) {
  await page.route("**/api/auth/verify-info*", async (route) => {
    if (status === 200) {
      await route.fulfill({ status: 200, contentType: "application/json",
        body: JSON.stringify({ email }) });
    } else {
      await route.fulfill({ status, contentType: "application/json",
        body: JSON.stringify({ detail: "invalid" }) });
    }
  });
}

/**
 * POST /api/auth/verify {token} -> 200 {email,is_admin} (consumes + sets cookie)
 * or a 4xx {detail}. Returns captured POST bodies so specs can assert it fired.
 */
export async function mockVerify(page, { status = 200, is_admin = false } = {}) {
  const calls = [];
  await page.route("**/api/auth/verify", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    calls.push(route.request().postDataJSON());
    if (status === 200) {
      await route.fulfill({ status: 200, contentType: "application/json",
        body: JSON.stringify({ email: "user@example.edu", is_admin }) });
    } else {
      await route.fulfill({ status, contentType: "application/json",
        body: JSON.stringify({ detail: "invalid" }) });
    }
  });
  return { calls };
}

/** POST /api/auth/logout -> 200. Returns a handle so specs can assert it fired. */
export async function mockLogout(page) {
  const calls = [];
  await page.route("**/api/auth/logout", async (route) => {
    calls.push(true);
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true }) });
  });
  return { calls };
}

/**
 * GET /api/chat/conversations -> [{id,title}].
 * Returns a handle whose `setList` lets a spec change what's returned for
 * later requests (e.g. after a chat is saved), without re-registering the route.
 */
export async function mockConversations(page, initial = []) {
  let list = initial;
  await page.route("**/api/chat/conversations", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(list) });
  });
  return { setList: (l) => { list = l; } };
}

/**
 * GET /api/chat/conversations/:id -> array of {role, content, id?, sql_log?}
 * (or a non-200 `httpStatus`, e.g. 404 for "doesn't exist" / 403 for "not
 * yours" -- see web/e2e/routing-chat.spec.js's bad-:id notice tests, which
 * assert the SAME rendered text for both so the UI isn't an enumeration
 * oracle). `sql_log` must be passed as a JSON STRING per the real API
 * contract (Chat.jsx does JSON.parse on it) — pass an array of SQL strings
 * and this helper stringifies it for you.
 *
 * Returns a handle whose live `.calls` getter counts GET requests actually
 * received, so a spec can assert a fetch for this id never fired at all (e.g.
 * a route param that must never reach the network, or a live SSE stream that
 * must not trigger a redundant reload right after the URL flips to it).
 */
export async function mockConversation(page, id, messages, { httpStatus = 200, detail } = {}) {
  let calls = 0;
  const body = (messages || []).map((m) => ({
    ...m,
    sql_log: m.sql_log !== undefined ? JSON.stringify(m.sql_log) : undefined,
  }));
  await page.route(`**/api/chat/conversations/${id}`, async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    calls += 1;
    if (httpStatus === 200) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    } else {
      await route.fulfill({ status: httpStatus, contentType: "application/json",
        body: JSON.stringify({ detail: detail || "Not found." }) });
    }
  });
  return { get calls() { return calls; } };
}

/**
 * POST /api/chat/stream -> SSE stream of {type,...} events.
 * Emits, in order: conversation -> status -> sql* -> answer -> done.
 * The final `done` carries `message_id`/`user_message_id` (the app reads these
 * to attach ids that unlock CSV/copy — see Chat.jsx submit()).
 * `answer` should be markdown containing a GFM table so specs can assert the
 * rendered <table>.
 *
 * Returns `{ calls }`: the parsed POST body (`{question, conversation_id,
 * edit_message_id}`, per frontend/src/api.js streamChat()) for every request this
 * route has fulfilled, in order — so a spec can assert exactly which
 * `conversation_id` a given turn sent (e.g. a follow-up turn must carry the
 * conversation id the FIRST turn was assigned, not null — see
 * web/e2e/routing-chat.spec.js's orphaned-conversation regression).
 *
 * `delayMs` (default 0) delays fulfilling the response by that many ms —
 * i.e. simulates network/model latency so the request is genuinely
 * "in-flight" (busy===true) for a spec to act during, e.g. clicking
 * "+ New chat" mid-stream. The whole response (SSE body) still lands as one
 * chunk once the delay elapses; this only defers *when* it lands, since
 * route.fulfill can't drip a body incrementally.
 */
export async function mockStreamChat(page, {
  conversationId,
  statusText = "Thinking…",
  sql = [],
  answer = "Answer.",
  messageId = null,
  userMessageId = null,
  title = null,
  delayMs = 0,
} = {}) {
  const calls = [];
  await page.route("**/api/chat/stream", async (route) => {
    calls.push(route.request().postDataJSON());
    if (delayMs > 0) await new Promise((r) => setTimeout(r, delayMs));
    const events = [
      { type: "conversation", id: conversationId },
      { type: "status", text: statusText },
      ...sql.map((s) => ({ type: "sql", sql: s })),
      { type: "answer", text: answer },
      { type: "done", message_id: messageId, user_message_id: userMessageId,
        model: "test", tokens: 0, ...(title ? { title } : {}) },
    ];
    const body = events.map((e) => `data: ${JSON.stringify(e)}`).join("\n\n") + "\n\n";
    await route.fulfill({ status: 200, contentType: "text/event-stream", body });
  });
  return { calls };
}

/**
 * POST /api/chat/stream -> SSE stream WITHOUT a `conversation` event -- mirrors
 * backend/app/routers/chat.py's has_data:false guard, which streams only
 * status/answer/done and deliberately never assigns/returns a conversation id
 * (there's nothing to save yet). Used to exercise the "URL never leaves /"
 * path -- see web/e2e/routing-chat.spec.js's "+ New chat" no-op regression.
 */
export async function mockStreamChatNoConversation(page, {
  statusText = "Thinking…",
  answer = "Answer.",
  messageId = null,
} = {}) {
  await page.route("**/api/chat/stream", async (route) => {
    const events = [
      { type: "status", text: statusText },
      { type: "answer", text: answer },
      { type: "done", message_id: messageId, user_message_id: null, model: "test", tokens: 0 },
    ];
    const body = events.map((e) => `data: ${JSON.stringify(e)}`).join("\n\n") + "\n\n";
    await route.fulfill({ status: 200, contentType: "text/event-stream", body });
  });
}

/**
 * POST /api/chat/stream -> a non-200 response, fulfilled BEFORE any SSE event
 * is ever written (mirrors a network drop / 500 that fails before the
 * `conversation` event). frontend/src/api.js's streamChat() throws
 * `new Error(await r.text())` as soon as `!r.ok`, which Chat.jsx's submit()
 * catches and renders as "⚠️ " + message -- so the URL never leaves / either.
 */
export async function mockStreamChatError(page, { httpStatus = 500, detail = "Internal error" } = {}) {
  await page.route("**/api/chat/stream", async (route) => {
    await route.fulfill({ status: httpStatus, contentType: "text/plain", body: detail });
  });
}

/**
 * DELETE /api/chat/conversations/:id -> {ok:true} (or a non-200 httpStatus).
 * Captures every deleted id (parsed out of the URL), mirroring
 * mockDeintegrate/mockClearDenial's shape (frontend/src/api.js: deleteConversation(id)
 * -> DELETE /api/chat/conversations/${id}).
 *
 * REST reuses the exact same path GET /api/chat/conversations/:id uses
 * (mockConversation), so this is registered on the same glob. A non-DELETE
 * request calls `route.fallback()` (never `route.continue()`, which would
 * escape straight to the real network and 404/hang in a mocked-only test env)
 * so it defers to an earlier-registered mockConversation(...) handler for
 * that id. Playwright runs the NEWEST-registered matching handler first, so
 * call mockDeleteConversation(...) AFTER any per-id mockConversation(...)
 * calls in a spec — that ordering is what lets fallback() actually reach them.
 */
export async function mockDeleteConversation(page, { httpStatus = 200 } = {}) {
  const calls = [];
  await page.route("**/api/chat/conversations/*", async (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    const url = new URL(route.request().url());
    calls.push(url.pathname.split("/").pop());
    await route.fulfill({ status: httpStatus, contentType: "application/json",
      body: JSON.stringify({ ok: httpStatus === 200 }) });
  });
  return { calls };
}

/**
 * GET/POST /api/admin/allowlist. Returns captured POST bodies.
 *
 * `postStatus`/`postBody` let a spec control exactly what POST returns, so
 * the four `inviteFlash()` branches in frontend/src/Admin.jsx (added/failed-to-add,
 * invited, mail_configured true/false) can each be driven deterministically —
 * see web/e2e/admin-allowlist-flash.spec.js. Defaults (200, {ok:true}) match
 * every pre-existing caller of this helper, which only cares about the GET.
 */
export async function mockAllowlist(page, rows, { postStatus = 200, postBody = { ok: true } } = {}) {
  const posts = [];
  await page.route("**/api/admin/allowlist", async (route) => {
    const req = route.request();
    if (req.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(rows) });
    } else if (req.method() === "POST") {
      posts.push(req.postDataJSON());
      await route.fulfill({ status: postStatus, contentType: "application/json", body: JSON.stringify(postBody) });
    } else {
      await route.continue();
    }
  });
  return { posts };
}

/** GET /api/admin/access-requests -> [{id,email}]. */
export async function mockAccessRequests(page, rows) {
  await page.route("**/api/admin/access-requests", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(rows) });
  });
}

/** GET /api/admin/usage?since&until -> {totals, series, top_users, bucket}. */
export async function mockUsage(page, data) {
  await page.route("**/api/admin/usage*", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
  });
}

/**
 * GET /api/admin/skills ->
 * [{id,question,headline,lesson,canonical_sql,notes,verified,upvotes,downvotes,hits,
 *   created_by}].
 * `headline` is the short generalized rule title that now leads the admin UI
 * (see web/e2e/admin-lessons.spec.js); `lesson` is the longer generalized
 * description, collapsed behind a "Details" `<details>`.
 */
export async function mockSkills(page, rows) {
  await page.route("**/api/admin/skills", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(rows) });
  });
}

/** GET /api/admin/import/jobs -> [{id,filename,status,updated_at}]. */
export async function mockImportJobs(page, rows) {
  await page.route("**/api/admin/import/jobs", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(rows) });
  });
}

/**
 * GET /api/admin/import/jobs/:id -> a job detail row, advancing through
 * `sequence` on each successive poll (the app polls every 2s until the
 * status is one of passed/failed/swapped — see frontend/src/Admin.jsx `watch()`);
 * once `sequence` is exhausted, the last entry is returned forever.
 *
 * A `progress` field on any sequence entry is passed through verbatim — per
 * the real API contract it's a JSON STRING (mirrors `sql_log` on chat
 * messages) that the app JSON.parses; shape: {overall:{phase,message},
 * years:{"<start_year>":{start_year,year_label,step,downloaded_bytes,
 * total_bytes,pct}}}. See web/e2e/nces-catalog.spec.js's per-file-progress
 * test for a worked example.
 */
export async function mockImportJobPoll(page, jobId, sequence) {
  let i = 0;
  await page.route(`**/api/admin/import/jobs/${jobId}`, async (route) => {
    const job = sequence[Math.min(i, sequence.length - 1)];
    i += 1;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(job) });
  });
}

/**
 * POST /api/admin/access-requests/{email}/deny -> {ok:true, email} (or a
 * non-200 httpStatus, e.g. 404/500). Captures the exact (decoded) email
 * parsed out of the request URL for every call, so a spec can assert
 * precisely which pending row's Reject button fired the request
 * (frontend/src/api.js: denyAccessRequest(email) ->
 * POST /api/admin/access-requests/${encodeURIComponent(email)}/deny).
 */
export async function mockDenyAccessRequest(page, { httpStatus = 200, detail } = {}) {
  const calls = [];
  await page.route("**/api/admin/access-requests/*/deny", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const url = new URL(route.request().url());
    const parts = url.pathname.split("/");
    const email = decodeURIComponent(parts[parts.length - 2]);
    calls.push(email);
    if (httpStatus === 200) {
      await route.fulfill({ status: 200, contentType: "application/json",
        body: JSON.stringify({ ok: true, email }) });
    } else {
      await route.fulfill({ status: httpStatus, contentType: "application/json",
        body: JSON.stringify({ detail: detail || "Could not reject that address." }) });
    }
  });
  return { calls };
}

/**
 * GET /api/admin/access-requests/denied ->
 * [{id, canon_email, emails:[...], created_at}] (or a non-200 `httpStatus`,
 * e.g. 500, to exercise the load-failure state -- see SEC #3,
 * web/e2e/undo-denial.spec.js). One object per CANONICAL group (deliberately
 * grouped differently from mockAccessRequests' raw-address pending list --
 * see admin.py's access_requests_denied docstring): `canon_email` is the
 * ACTUALLY-BLOCKED address (also the argument the Undo control's DELETE call
 * keys on) and `emails` is every distinct ORIGINAL address in the group.
 * Which the UI renders, and when, is a UI decision -- see SEC #1 in
 * web/e2e/undo-denial.spec.js for why canon_email is NOT always hidden.
 */
export async function mockDeniedRequests(page, rows, { httpStatus = 200 } = {}) {
  await page.route("**/api/admin/access-requests/denied", async (route) => {
    if (httpStatus === 200) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(rows) });
    } else {
      await route.fulfill({ status: httpStatus, contentType: "application/json",
        body: JSON.stringify({ detail: "Could not load blocked addresses." }) });
    }
  });
}

/**
 * DELETE /api/admin/access-requests/{email}/denial -> {ok:true, email,
 * cleared} (or a non-200 httpStatus, e.g. 500). Captures the exact (decoded)
 * address parsed out of the request URL for every call -- Admin.jsx's undo()
 * calls this with the row's `canon_email`, never a displayed original, so a
 * spec can assert precisely that (frontend/src/api.js: clearDenial(email) ->
 * DELETE /api/admin/access-requests/${encodeURIComponent(email)}/denial).
 * Routed on `.../denial` specifically so it never matches
 * mockDenyAccessRequest's `.../deny` route on the same page.
 */
export async function mockClearDenial(page, { httpStatus = 200, cleared = 1, detail } = {}) {
  const calls = [];
  await page.route("**/api/admin/access-requests/*/denial", async (route) => {
    if (route.request().method() !== "DELETE") return route.continue();
    const url = new URL(route.request().url());
    const parts = url.pathname.split("/");
    const email = decodeURIComponent(parts[parts.length - 2]);
    calls.push(email);
    if (httpStatus === 200) {
      await route.fulfill({ status: 200, contentType: "application/json",
        body: JSON.stringify({ ok: true, email, cleared }) });
    } else {
      await route.fulfill({ status: httpStatus, contentType: "application/json",
        body: JSON.stringify({ detail: detail || "Could not undo that block." }) });
    }
  });
  return { calls };
}

/**
 * GET /api/admin/logs?... -> {records:[{ts,level,name,msg}]}. Drives the
 * Admin -> Logs subtab (frontend/src/api.js: logs(...) -> GET /api/admin/logs?...;
 * Admin.jsx's Logs() reads `d.records`).
 */
export async function mockLogs(page, records) {
  await page.route("**/api/admin/logs*", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ records }) });
  });
}

/**
 * GET /api/admin/import/catalog -> {probed_at, partial, years:[{start_year,
 * year, year_label, status, integrated, available, release, selectable,
 * zip_bytes}], disk:{free_bytes,total_bytes,used_bytes}, calibration:{
 * expand_factor,default_per_year_db_mb,bandwidth_mbps,build_seconds_per_year,
 * safety_factor,per_year_db_bytes,live_db_bytes,already_integrated_count}}.
 * `status` gains an "update" value (already-integrated but a newer release
 * is now out) alongside the existing integrated/final/provisional/unknown.
 * This helper is a pure passthrough — every field on `data` (however you
 * shape it) is forwarded verbatim, so callers control zip_bytes/disk/
 * calibration/status directly rather than this helper synthesizing them.
 */
export async function mockImportCatalog(page, data) {
  await page.route("**/api/admin/import/catalog", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
  });
}

/**
 * POST /api/admin/import/integrate {years:[start_year,...]} -> {job_id, status}
 * (or a non-200 `httpStatus`, e.g. 409 when an import is already running).
 * Returns captured POST bodies so specs can assert the exact years posted.
 */
export async function mockIntegrate(page, { jobId = 1, status = "pending", httpStatus = 200, detail } = {}) {
  const posts = [];
  await page.route("**/api/admin/import/integrate", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    posts.push(route.request().postDataJSON());
    if (httpStatus === 200) {
      await route.fulfill({ status: 200, contentType: "application/json",
        body: JSON.stringify({ job_id: jobId, status }) });
    } else {
      await route.fulfill({ status: httpStatus, contentType: "application/json",
        body: JSON.stringify({ detail: detail || "An import is already running. Wait for it to finish." }) });
    }
  });
  return { posts };
}

/**
 * DELETE /api/admin/import/year/{start_year} -> {job_id, status} (or a
 * non-200 `httpStatus`, e.g. 400/409). Captures the exact start_year parsed
 * out of the request URL for every call, so a spec can assert precisely which
 * year's trashcan button fired the request (frontend/src/api.js:
 * deintegrateYear(startYear) -> DELETE /api/admin/import/year/${startYear}).
 */
export async function mockDeintegrate(page, { jobId = 1, status = "pending", httpStatus = 200, detail } = {}) {
  const calls = [];
  await page.route("**/api/admin/import/year/*", async (route) => {
    if (route.request().method() !== "DELETE") return route.continue();
    const url = new URL(route.request().url());
    calls.push(Number(url.pathname.split("/").pop()));
    if (httpStatus === 200) {
      await route.fulfill({ status: 200, contentType: "application/json",
        body: JSON.stringify({ job_id: jobId, status }) });
    } else {
      await route.fulfill({ status: httpStatus, contentType: "application/json",
        body: JSON.stringify({ detail: detail || "An import is already running. Wait for it to finish." }) });
    }
  });
  return { calls };
}
