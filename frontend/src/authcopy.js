// User-facing wording for auth + request failures. Only the WORDING lives here;
// the behaviour around it (who clears `user`, when the login door renders, how a
// failed turn is styled) stays in App.jsx / Chat.jsx and is covered by
// Playwright. The exact strings are pinned by authcopy.test.js — same split as
// announce.js.
//
// The rule these strings exist to enforce: a user never sees a raw response
// body. Before this, an expired session showed nothing at all (the shell just
// went inert) and an ordinary rate-limit showed the literal text
// ⚠️ {"detail":"Too many requests — please slow down and try again in a moment."}

export const SESSION_EXPIRED =
  "Your session expired. Sign in again to pick up where you left off — your chats are saved.";

// Deliberately distinct from SESSION_EXPIRED: being logged out because the
// server is unreachable is a different problem with a different fix, and
// telling someone to sign in again when signing in won't work wastes their time.
export const SERVER_UNREACHABLE =
  "We couldn't reach the server. Check your connection and reload — you may still be signed in.";

// Shown in the answer bubble when a turn fails. Keyed on status so the common,
// EXPECTED failures read as ordinary conditions rather than as breakage.
export function turnErrorMessage(status, detail) {
  if (status === 429) {
    return "You're asking faster than the assistant can answer. Give it a moment and try again.";
  }
  if (status === 401) return SESSION_EXPIRED;
  if (status === 503 || status === 502 || status === 504) {
    return "The assistant is unavailable right now. Try again in a moment.";
  }
  // Anything else: prefer the server's own sentence — it is written for a human
  // (guard refusals, "no query is associated with this answer") and is more
  // useful than a generic apology. Fall back only when there isn't one.
  return detail || "Something went wrong answering that. Try again.";
}

// A load that failed is NOT an empty list. Every one of these replaced a state
// that read as "there is nothing here": Logs said "No log records." while the
// request was failing, which is the worst possible thing to tell an admin whose
// job on that screen is to find out whether something is wrong.
export function loadErrorMessage(what, detail) {
  return detail || `Couldn't load ${what}. Try again in a moment.`;
}
