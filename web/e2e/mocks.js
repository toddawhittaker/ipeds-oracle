// Shared /api/** route-mocking helpers for the Playwright e2e suite.
//
// The React app (web/src/*) is driven for real through a Playwright webServer
// (Vite dev, see playwright.config.js); nothing here talks to a live backend.
// Every helper takes the Playwright `page` and installs a `page.route(...)`
// interceptor that fulfills a canned response, so specs stay deterministic
// with no OPENROUTER_API_KEY and no ipeds.db.
//
// Contracts mirrored here come from web/src/api.js and web/src/Chat.jsx.

/** GET /api/auth/me -> 200 {email,is_admin} when signed in, or 401 (logged out) when user is null. */
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
        body: JSON.stringify(user),
      });
    }
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
        body: JSON.stringify({ email: "user@franklin.edu", is_admin }) });
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
 * to attach ids that unlock feedback/CSV/copy — see Chat.jsx submit()).
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

/** POST /api/chat/messages/:id/feedback {value} -> 200. Returns captured POST bodies. */
export async function mockFeedback(page) {
  const posts = [];
  await page.route("**/api/chat/messages/*/feedback", async (route) => {
    posts.push(route.request().postDataJSON());
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true }) });
  });
  return { posts };
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

/** GET /api/admin/skills -> [{id,question,canonical_sql,notes,verified,upvotes,downvotes,hits}]. */
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
