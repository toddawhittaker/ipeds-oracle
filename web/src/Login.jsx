import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";

const EXAMPLES = [
  "Top 20 institutions awarding Associate's degrees in Registered Nursing over the last 3 years",
  "How many Computer Science bachelor's degrees did California public universities award last year?",
  "National total of Associate's degrees per year, all programs",
  "Which states awarded the most Master's degrees in Education?",
];

export default function Login() {
  const [email, setEmail] = useState("");
  const [msg, setMsg] = useState(null);
  const [ok, setOk] = useState(false);
  const [busy, setBusy] = useState(false);
  const noticeRef = useRef(null);

  useEffect(() => {
    if (ok) noticeRef.current?.focus();
  }, [ok]);

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
      <div className="card login">
        <h1>IPEDS Query</h1>
        <p className="muted">
          Ask natural-language questions about U.S. colleges &amp; universities.
          Access is by invitation.
        </p>
        {msg && (
          <div className="notice" role="alert" tabIndex={-1} ref={noticeRef}>{msg}</div>
        )}
        {!ok && (
          <form onSubmit={submit}>
            <label htmlFor="login-email" className="sr-only">Email</label>
            <input
              id="login-email"
              type="email" required placeholder="you@franklin.edu" autoComplete="email"
              value={email} onChange={(e) => setEmail(e.target.value)}
            />
            <button type="submit" disabled={busy}>
              {busy ? "Sending…" : "Email me a sign-in link"}
            </button>
          </form>
        )}
        <div className="examples">
          <div className="muted small">You'll be able to ask things like:</div>
          <ul>{EXAMPLES.map((x) => <li key={x}>{x}</li>)}</ul>
        </div>
      </div>
    </div>
  );
}
