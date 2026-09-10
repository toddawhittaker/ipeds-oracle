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

// FastAPI's OWN 422 — raised by pydantic before any handler runs, on a
// malformed body (a non-integer conversation_id, a missing field) — sends
// `detail` as an ARRAY of {loc, msg, type} objects rather than a string. Passing
// that array to `new ApiError(...)` lets the Error constructor stringify it to
// "[object Object]", which is the raw-body failure above wearing a different
// hat: nothing in this codebase raises a 422 by hand, so it is the one status
// that slips past every hand-written message. Flatten to the human `msg` parts.
export function detailText(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => d?.msg).filter((m) => typeof m === "string" && m).join("; ");
  }
  return "";
}

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

// Why an SSO sign-in bounced back to the door. The server redirects with
// `?auth_error=<code>` from a CLOSED set (app/oidc.py's AUTH_ERRORS), never with
// text a provider supplied — and this map is the second half of that guarantee:
// anything unrecognised falls through to generic copy, so a code that somehow
// arrives from outside the set renders our words rather than an attacker's.
//
// Wording rule: say what the reader can DO. "not_authorized" is the one that
// actually happens to real people (they authenticated fine and are outside the
// configured group or domain), and telling them to contact an administrator is
// the only useful thing there is to say.
const AUTH_ERROR_COPY = {
  invalid_state:
    "That sign-in link had expired. Start again — it only takes a moment.",
  provider_error:
    "Your identity provider couldn't complete the sign-in. Try again, or contact your administrator.",
  provider_unreachable:
    "We couldn't reach your identity provider. Try again in a moment.",
  not_authorized:
    "Your account isn't authorised for this application. Contact your administrator.",
  denied:
    "Your access to this application has been withdrawn. Contact your administrator.",
};

// Exported so a test iterates the map's OWN keys rather than a second hand-kept
// copy of them. The Python side (app/oidc.py's AUTH_ERRORS) is pinned against
// this file by a backend test, since nothing else can compare the two.
export const AUTH_ERROR_CODES = Object.keys(AUTH_ERROR_COPY);

export function authErrorMessage(code) {
  return AUTH_ERROR_COPY[code]
    || "Sign-in didn't complete. Try again, or contact your administrator.";
}
