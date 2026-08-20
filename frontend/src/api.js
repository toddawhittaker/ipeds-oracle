// Thin API client. Cookies (session) are sent automatically (same-origin).
import { detailText } from "./authcopy.js";

// Every failed request throws one of these. Before it existed, `j()` threw a bare
// Error whose message was the RAW RESPONSE BODY and discarded the status
// entirely — so callers that wanted the human message had to JSON.parse the
// message string (four places did, each with its own fallback), callers that
// wanted the status had to regex the detail text (two did), and anything that
// forwarded err.message to the UI printed FastAPI's JSON braces at the user.
export class ApiError extends Error {
  constructor(status, detail, statusText) {
    // `message` stays human: it's what leaks into a UI that forgets to read
    // .detail, which is the failure mode this class exists to end.
    super(detail || statusText || `Request failed (${status}).`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail || "";
  }

  get isUnauthenticated() {
    return this.status === 401;
  }
}

// FastAPI errors arrive as {"detail": "..."}; anything else (a proxy's HTML 502,
// an empty body) falls back to the status text.
async function _apiError(r) {
  const text = await r.text().catch(() => "");
  let detail = "";
  try {
    // detailText, not `|| ""`: a pydantic 422 sends detail as an ARRAY, which
    // the Error constructor would stringify to "[object Object]". See authcopy.
    detail = detailText(JSON.parse(text)?.detail);
  } catch {
    // Not JSON. FastAPI always sends {"detail": ...}, but a reverse proxy or
    // tunnel in front of it does not — and a plain-text "upstream timed out" is
    // far more useful to show than a generic apology. Guard against dumping an
    // HTML error page into the UI.
    const t = text.trim();
    if (t && t.length <= 200 && !t.startsWith("<")) detail = t;
  }
  return new ApiError(r.status, detail, r.statusText);
}

// Set by App.jsx. A 401 SUGGESTS the session is gone, and every caller would
// otherwise have to notice for itself — which none did, so an expired session
// left the shell rendered and inert (empty sidebar, "Loading…" forever, "No log
// records."). One hook, notified once per 401.
//
// It is a SUGGESTION, not proof: the handler re-checks /api/auth/me before
// signing anyone out. A single endpoint answering 401 — a stale route, a race
// against sign-out, a background poll hitting something unexpected — must not
// throw a working session away. (Trusting the first 401 blindly logged the user
// out on any incidental one, which broke ~226 e2e specs and would have done the
// same to real users.)
let _onUnauthenticated = null;

export function setUnauthenticatedHandler(fn) {
  _onUnauthenticated = fn;
}

// The auth check itself is exempt, or confirming a 401 would re-enter the
// handler that is doing the confirming.
const AUTH_CHECK_URL = "/api/auth/me";

// A page load fires several requests at once, so an expired session produces a
// BURST of 401s. Collapse them into one confirmation instead of one per failed
// request — otherwise a single expiry costs N extra /auth/me round-trips.
let _confirming = false;

function _raise(err, url) {
  if (err.status === 401 && _onUnauthenticated && !_confirming
      && !String(url).endsWith(AUTH_CHECK_URL)) {
    _confirming = true;
    Promise.resolve(_onUnauthenticated()).finally(() => { _confirming = false; });
  }
  throw err;
}

async function j(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  if (!r.ok) _raise(await _apiError(r), url);
  const ct = r.headers.get("content-type") || "";
  return ct.includes("application/json") ? r.json() : r.text();
}

export const api = {
  me: () => j("GET", "/api/auth/me"),
  publicConfig: () => j("GET", "/api/auth/config"),
  // Running version + whether a newer GitHub release is available.
  version: () => j("GET", "/api/version"),
  requestLink: (email) => j("POST", "/api/auth/request", { email }),
  // Sign-in confirmation page: peek (non-consuming) then verify (consumes).
  // BOTH are POSTs with the token in the body — a token in a query string is
  // written verbatim to the server's access log, which is how a live sign-in
  // link became readable via `docker logs`.
  verifyInfo: (token) => j("POST", "/api/auth/verify-info", { token }),
  verify: (token) => j("POST", "/api/auth/verify", { token }),
  logout: () => j("POST", "/api/auth/logout"),

  conversations: () => j("GET", "/api/chat/conversations"),
  conversation: (id) => j("GET", `/api/chat/conversations/${id}`),
  renameConversation: (id, title) => j("PATCH", `/api/chat/conversations/${id}`, { title }),
  deleteConversation: (id) => j("DELETE", `/api/chat/conversations/${id}`),
  csvUrl: (msgId) => `/api/chat/messages/${msgId}/download.csv`,

  // The caller's own MCP API keys. createApiKey's response is the ONLY time the
  // raw key exists outside the client — nothing stores it and no later request
  // can return it, so whatever calls this has to show it once and say so.
  apiKeys: () => j("GET", "/api/keys"),
  createApiKey: (label) => j("POST", "/api/keys", { label }),
  // The label is the only editable field on a key. A revoked key answers 404
  // here exactly as somebody else's does — see app/routers/keys.py.
  relabelApiKey: (id, label) => j("PATCH", `/api/keys/${id}`, { label }),
  revokeApiKey: (id) => j("DELETE", `/api/keys/${id}`),

  // admin
  allowlist: () => j("GET", "/api/admin/allowlist"),
  addAllow: (email, note, is_admin) =>
    j("POST", "/api/admin/allowlist", { email, note, is_admin }),
  bulkAllow: (users) => j("POST", "/api/admin/allowlist/bulk", { users }),
  removeAllow: (email) => j("DELETE", `/api/admin/allowlist/${encodeURIComponent(email)}`),
  setAdmin: (email, is_admin) =>
    j("PATCH", `/api/admin/allowlist/${encodeURIComponent(email)}`, { is_admin }),
  // Bulk row-selection actions (Admin -> Users tab). Distinct from bulkAllow
  // above (the CSV-import path) -- these act on an explicit selection of
  // already-allowlisted / already-filed rows.
  bulkAllowlistAction: (action, emails) =>
    j("POST", "/api/admin/allowlist/bulk-action", { action, emails }),
  accessRequests: () => j("GET", "/api/admin/access-requests"),
  denyAccessRequest: (email) =>
    j("POST", `/api/admin/access-requests/${encodeURIComponent(email)}/deny`),
  bulkAccessRequests: (action, ids) =>
    j("POST", "/api/admin/access-requests/bulk", { action, ids }),
  deniedRequests: () => j("GET", "/api/admin/access-requests/denied"),
  clearDenial: (email) =>
    j("DELETE", `/api/admin/access-requests/${encodeURIComponent(email)}/denial`),
  bulkClearDenials: (ids) =>
    j("POST", "/api/admin/access-requests/denial/bulk", { action: "unblock", ids }),
  usage: (since, until) => {
    const p = new URLSearchParams();
    if (since) p.set("since", String(Math.floor(since)));
    if (until) p.set("until", String(Math.floor(until)));
    // The viewer's own timezone, so the graph buckets in local time.
    try {
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (tz) p.set("tz", tz);
    } catch { /* fall back to the server default */ }
    const qs = p.toString();
    return j("GET", "/api/admin/usage" + (qs ? `?${qs}` : ""));
  },
  // Attention badges: how much work is waiting in each admin area, plus an
  // acknowledge for the Logs badge (advances this admin's "logs seen" marker).
  attention: () => j("GET", "/api/admin/attention"),
  markLogsSeen: () => j("POST", "/api/admin/logs/seen"),
  skills: () => j("GET", "/api/admin/skills"),
  // A2 (lesson-rejection memory): `muteCategory` folds a category mute into
  // the SAME delete request (one atomic admin intent) — backwards-compatible
  // default so every existing call site (a plain reject) is unaffected.
  deleteSkill: (id, { muteCategory = false } = {}) =>
    j("DELETE", `/api/admin/skills/${id}${muteCategory ? "?mute_category=1" : ""}`),
  patchSkill: (id, body) => j("PATCH", `/api/admin/skills/${id}`, body),
  skillCategories: () => j("GET", "/api/admin/skills/categories"),
  muteSkillCategory: (token, muted) =>
    j(muted ? "POST" : "DELETE",
      `/api/admin/skills/categories/${encodeURIComponent(token)}/mute`),
  skillRejections: () => j("GET", "/api/admin/skills/rejections"),
  deleteSkillRejection: (id) => j("DELETE", `/api/admin/skills/rejections/${id}`),
  clearSkillRejections: () => j("DELETE", "/api/admin/skills/rejections"),
  importJobs: () => j("GET", "/api/admin/import/jobs"),
  importJob: (id) => j("GET", `/api/admin/import/jobs/${id}`),
  importCatalog: (refresh = false) =>
    j("GET", "/api/admin/import/catalog" + (refresh ? "?refresh=1" : "")),
  integrateYears: (years) => j("POST", "/api/admin/import/integrate", { years }),
  deintegrateYear: (startYear) => j("DELETE", `/api/admin/import/year/${startYear}`),
  // Every user's keys, and minting one on somebody else's behalf. Same one-shot
  // `key` contract as createApiKey above: the admin has to hand it over out of
  // band, because nothing can read it back.
  allKeys: () => j("GET", "/api/admin/keys"),
  createKeyFor: (email, label) => j("POST", "/api/admin/keys", { email, label }),
  revokeAnyKey: (id) => j("DELETE", `/api/admin/keys/${id}`),
  logs: (limit = 200, level = "", q = "", since = null, until = null) => {
    const p = new URLSearchParams({ limit: String(limit) });
    if (level) p.set("level", level);
    if (q) p.set("q", q);
    if (since != null) p.set("since", String(since));
    if (until != null) p.set("until", String(until));
    return j("GET", `/api/admin/logs?${p.toString()}`);
  },
};

// Stream a chat answer via SSE (POST + ReadableStream). Calls onEvent per event.
export async function streamChat({ question, conversationId, editMessageId }, onEvent) {
  const r = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question,
      conversation_id: conversationId ?? null,
      edit_message_id: editMessageId ?? null,
    }),
  });
  if (!r.ok) _raise(await _apiError(r), "/api/chat/stream");
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop();
    for (const p of parts) {
      const line = p.trim();
      if (line.startsWith("data:")) {
        try { onEvent(JSON.parse(line.slice(5).trim())); } catch { /* ignore malformed SSE line */ }
      }
    }
  }
}
