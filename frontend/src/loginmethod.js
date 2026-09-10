// Which sign-in form the door renders.
//
// Pure so it can be pinned without a browser, and separate from Login.jsx
// because the DEFAULT is a safety property rather than a display detail: an
// absent, unknown or malformed `auth_method` must read as "magic_link". The
// config call can fail (offline, a proxy returning HTML, an older backend), and
// the magic-link form is the one that still works when it does — it is also the
// only method gated by the manually curated allowlist, so guessing it is the
// conservative guess in both directions.
export const METHODS = ["magic_link", "oidc"];

export function loginMethod(cfg) {
  const raw = cfg && typeof cfg.auth_method === "string" ? cfg.auth_method.trim() : "";
  return METHODS.includes(raw) ? raw : "magic_link";
}

// The button label an operator configured, falling back to wording that makes
// sense at every institution. A blank label is a real possibility — the setting
// is free text and an empty .env value reaches the browser as "" — and a button
// with no name is unusable, so this is not defensive tidiness.
export function ssoButtonLabel(cfg) {
  const raw = cfg && typeof cfg.oidc_button_label === "string"
    ? cfg.oidc_button_label.trim() : "";
  return raw || "Sign in with SSO";
}
