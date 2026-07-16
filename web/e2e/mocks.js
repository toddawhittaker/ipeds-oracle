// Shared /api/** route-mocking helpers for the Playwright e2e suite.
//
// The React app (web/src/*) is driven for real through a Playwright webServer
// (Vite dev, see playwright.config.js); nothing here talks to a live backend.
// Every helper takes the Playwright `page` and installs a `page.route(...)`
// interceptor that fulfills a canned response, so specs stay deterministic
// with no LLM_API_KEY and no ipeds.db.
//
// Contracts mirrored here come from web/src/api.js and web/src/Chat.jsx.

/**
 * GET /api/auth/me -> 200 {email,is_admin,has_data} when signed in, or 401
 * (logged out) when user is null.
 *
 * `has_data` defaults to `true` when the caller's `user` object doesn't
 * specify it, so every existing spec (written before the no-data/onboarding
 * feature existed) keeps rendering Chat/Admin normally without having to be
 * touched. Pass `has_data: false` explicitly to exercise the fresh-deploy
 * no-data state (see web/e2e/no-data-onboarding.spec.js).
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
 * GET /api/chat/conversations/:id -> array of {role, content, id?, sql_log?}.
 * `sql_log` must be passed as a JSON STRING per the real API contract (Chat.jsx
 * does JSON.parse on it) — pass an array of SQL strings and this helper
 * stringifies it for you.
 */
export async function mockConversation(page, id, messages) {
  const body = messages.map((m) => ({
    ...m,
    sql_log: m.sql_log !== undefined ? JSON.stringify(m.sql_log) : undefined,
  }));
  await page.route(`**/api/chat/conversations/${id}`, async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
}

/**
 * POST /api/chat/stream -> SSE stream of {type,...} events.
 * Emits, in order: conversation -> status -> sql* -> answer -> done.
 * The final `done` carries `message_id`/`user_message_id` (the app reads these
 * to attach ids that unlock CSV/copy — see Chat.jsx submit()).
 * `answer` should be markdown containing a GFM table so specs can assert the
 * rendered <table>.
 */
export async function mockStreamChat(page, {
  conversationId,
  statusText = "Thinking…",
  sql = [],
  answer = "Answer.",
  messageId = null,
  userMessageId = null,
  title = null,
} = {}) {
  await page.route("**/api/chat/stream", async (route) => {
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
}

/** GET/POST /api/admin/allowlist. Returns captured POST bodies. */
export async function mockAllowlist(page, rows) {
  const posts = [];
  await page.route("**/api/admin/allowlist", async (route) => {
    const req = route.request();
    if (req.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(rows) });
    } else if (req.method() === "POST") {
      posts.push(req.postDataJSON());
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true }) });
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
 * status is one of passed/failed/swapped — see web/src/Admin.jsx `watch()`);
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
 * year's trashcan button fired the request (web/src/api.js:
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
