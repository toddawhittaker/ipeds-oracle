import React, { useEffect, useState } from "react";
import { api } from "./api.js";

// Sign-in confirmation page (route: /verify?token=…). The magic-link email
// points here. We do NOT consume the token on load — a deliberate click POSTs
// it — so email link-scanners that merely GET the link can't burn it. The
// token is stripped from the URL/history immediately so it doesn't linger in
// bookmarks, history, or referrers.
export default function Verify() {
  const [token] = useState(
    () => new URLSearchParams(window.location.search).get("token") || ""
  );
  const [email, setEmail] = useState(null);
  // Derive the initial state from the token synchronously so the effect never
  // has to setState in its body (react-hooks/set-state-in-effect).
  const [state, setState] = useState(token ? "loading" : "error"); // loading|ready|signing|error
  const [error, setError] = useState(
    token ? "" : "This sign-in link is missing its token."
  );

  useEffect(() => {
    // Drop the token from the address bar / history right away.
    window.history.replaceState({}, "", "/verify");
    if (!token) return;
    api
      .verifyInfo(token)
      .then((r) => {
        setEmail(r.email);
        setState("ready");
      })
      .catch(() => {
        setState("error");
        setError("This sign-in link is invalid or has expired.");
      });
  }, [token]);

  async function signIn() {
    setState("signing");
    try {
      await api.verify(token);
      // Reload into the app; App.jsx re-runs api.me() and renders signed-in.
      window.location.assign("/");
    } catch {
      setState("error");
      setError("This sign-in link is invalid or has expired.");
    }
  }

  return (
    <div className="center">
      <div className="card login">
        <h1><span className="wordmark" role="img" aria-label="IPEDS Query" /></h1>
        {state === "loading" && (
          <p className="muted">Checking your sign-in link…</p>
        )}
        {state === "error" && (
          <>
            <div className="notice" role="alert">{error}</div>
            <p>
              <a className="link" href="/">Return to sign in</a>
            </p>
          </>
        )}
        {(state === "ready" || state === "signing") && (
          <>
            <p>
              Sign in to IPEDS Query as <strong>{email}</strong>?
            </p>
            <button onClick={signIn} disabled={state === "signing"}>
              {state === "signing" ? "Signing in…" : "Sign in"}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
