// Thin API client. Cookies (session) are sent automatically (same-origin).

async function j(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  if (!r.ok) {
    const text = await r.text();
    throw new Error(text || r.statusText);
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("application/json") ? r.json() : r.text();
}

export const api = {
  me: () => j("GET", "/api/auth/me"),
  publicConfig: () => j("GET", "/api/auth/config"),
  requestLink: (email) => j("POST", "/api/auth/request", { email }),
  // Sign-in confirmation page: peek (non-consuming) then verify (consumes).
  verifyInfo: (token) =>
    j("GET", "/api/auth/verify-info?token=" + encodeURIComponent(token)),
  verify: (token) => j("POST", "/api/auth/verify", { token }),
  logout: () => j("POST", "/api/auth/logout"),

  conversations: () => j("GET", "/api/chat/conversations"),
  conversation: (id) => j("GET", `/api/chat/conversations/${id}`),
  deleteConversation: (id) => j("DELETE", `/api/chat/conversations/${id}`),
  csvUrl: (msgId) => `/api/chat/messages/${msgId}/download.csv`,

  // admin
  allowlist: () => j("GET", "/api/admin/allowlist"),
  addAllow: (email, note, is_admin) =>
    j("POST", "/api/admin/allowlist", { email, note, is_admin }),
  removeAllow: (email) => j("DELETE", `/api/admin/allowlist/${encodeURIComponent(email)}`),
  setAdmin: (email, is_admin) =>
    j("PATCH", `/api/admin/allowlist/${encodeURIComponent(email)}`, { is_admin }),
  accessRequests: () => j("GET", "/api/admin/access-requests"),
  denyAccessRequest: (email) =>
    j("POST", `/api/admin/access-requests/${encodeURIComponent(email)}/deny`),
  deniedRequests: () => j("GET", "/api/admin/access-requests/denied"),
  clearDenial: (email) =>
    j("DELETE", `/api/admin/access-requests/${encodeURIComponent(email)}/denial`),
  usage: (since, until) => {
    const p = new URLSearchParams();
    if (since) p.set("since", String(Math.floor(since)));
    if (until) p.set("until", String(Math.floor(until)));
    const qs = p.toString();
    return j("GET", "/api/admin/usage" + (qs ? `?${qs}` : ""));
  },
  skills: () => j("GET", "/api/admin/skills"),
  deleteSkill: (id) => j("DELETE", `/api/admin/skills/${id}`),
  patchSkill: (id, body) => j("PATCH", `/api/admin/skills/${id}`, body),
  importJobs: () => j("GET", "/api/admin/import/jobs"),
  importJob: (id) => j("GET", `/api/admin/import/jobs/${id}`),
  importCatalog: (refresh = false) =>
    j("GET", "/api/admin/import/catalog" + (refresh ? "?refresh=1" : "")),
  integrateYears: (years) => j("POST", "/api/admin/import/integrate", { years }),
  deintegrateYear: (startYear) => j("DELETE", `/api/admin/import/year/${startYear}`),
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
  if (!r.ok) throw new Error(await r.text());
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
