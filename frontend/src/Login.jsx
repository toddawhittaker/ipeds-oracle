import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import Wordmark from "./Wordmark.jsx";

// Shown until the server tells us the institution's domain, and if it never does.
const FALLBACK_HINT = "you@yourschool.edu";

export default function Login() {
  const [email, setEmail] = useState("");
  const [msg, setMsg] = useState(null);
  const [ok, setOk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState(FALLBACK_HINT);
  const noticeRef = useRef(null);

  useEffect(() => {
    if (ok) noticeRef.current?.focus();
  }, [ok]);

  useEffect(() => {
    // A hint only — the field stays usable if this never resolves.
    api.publicConfig()
      .then((c) => { if (c.email_domain) setHint(`you@${c.email_domain}`); })
      .catch(() => {});
  }, []);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    try {
      const r = await api.requestLink(email);
      setMsg(r.message);
      setOk(true);
    } catch {
      setMsg("Something went wrong. Please try again.");
      setOk(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="center">
      <div className="login-door">
        {/* Left: the value, stated before anything is asked — a real specimen
            answer, set as a typeset figure. */}
        <div className="door-left">
          <span className="field-label">The higher-education census, answerable</span>
          <h2 className="door-thesis">Ask a question. Get a figure you can cite.</h2>
          <p className="muted">
            Degrees, enrollment, tuition, staffing and finance across every U.S.
            institution IPEDS tracks.
          </p>
          <div className="door-figure">
            <span className="field-label">
              Computer science · Bachelor&apos;s · California publics · 2024
            </span>
            <div className="figure num">7,679</div>
            <div className="door-figure-src">degrees conferred · IPEDS Completions</div>
          </div>
        </div>

        {/* Right: the reader's card. */}
        <div className="card login door-right">
          <h1><Wordmark /></h1>
          <p className="muted">
            Access is by invitation. We&apos;ll email a one-time sign-in link —
            no password to remember.
          </p>
          {msg && (
            <div className={"notice " + (ok ? "ok" : "error")} role="alert"
                 tabIndex={-1} ref={noticeRef}>{msg}</div>
          )}
          {!ok && (
            <form onSubmit={submit}>
              <label htmlFor="login-email" className="sr-only">Email</label>
              <input
                id="login-email"
                type="email" required placeholder={hint} autoComplete="email"
                autoFocus
                value={email} onChange={(e) => setEmail(e.target.value)}
              />
              <button type="submit" disabled={busy}>
                {busy ? "Sending…" : "Email me a sign-in link"}
              </button>
            </form>
          )}
          <p className="door-fineprint muted small">
            Not on the list? Request access with your institution email and an
            administrator will review it.
          </p>
        </div>
      </div>
    </div>
  );
}
