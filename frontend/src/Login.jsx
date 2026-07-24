import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import Wordmark from "./Wordmark.jsx";
import { IconChevronLeft, IconChevronRight } from "./icons.jsx";

// Shown until the server tells us the institution's domain, and if it never does.
const FALLBACK_HINT = "you@yourschool.edu";

// Real IPEDS specimen answers shown on the login "door" as a rotating gallery —
// each one is a figure the app can actually produce, verified against ipeds.db.
// (Values are display forms; source lines name the survey.)
const DOOR_FIGURES = [
  { label: "Computer science · Bachelor’s · California publics · 2024",
    value: "7,397", source: "degrees conferred · IPEDS Completions" },
  { label: "Fall enrollment · Community colleges · 2021–2025",
    value: "−4.8%", source: "change at public two-year colleges · IPEDS" },
  { label: "Bachelor’s degrees · United States · 2024",
    value: "1.99M", source: "conferred nationwide · IPEDS Completions" },
  { label: "Women’s share · Bachelor’s degrees · U.S. · 2024",
    value: "58.5%", source: "of all bachelor’s degrees · IPEDS Completions" },
  { label: "Total fall enrollment · All U.S. institutions · 2025",
    value: "20.4M", source: "students enrolled · IPEDS Fall Enrollment" },
  { label: "Median endowment · U.S. colleges · 2025",
    value: "$28.3M", source: "half hold more, half less · IPEDS Finance" },
  { label: "Tuition & fees · Private nonprofit four-year · 2021–2024",
    value: "+5.4%", source: "change in the median published price · IPEDS" },
];
const ROTATE_MS = 5000;

// The door's hero statistic as an auto-advancing gallery (5s each) with manual
// ‹ ··· › controls. Rotation pauses while the gallery is hovered or holds
// keyboard focus (WCAG 2.2.2 Pause/Stop/Hide) and never auto-starts under a
// reduced-motion preference; the arrows/dots still work in every case.
function DoorFigures() {
  const [i, setI] = useState(0);
  const [dir, setDir] = useState(1); // slide direction: 1 = forward, -1 = back
  const [paused, setPaused] = useState(false);
  const n = DOOR_FIGURES.length;

  const move = (target, d) => { setDir(d); setI(target); };
  const go = (d) => move((i + d + n) % n, d);

  useEffect(() => {
    if (paused) return undefined;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return undefined;
    // Re-armed on every index change too, so a manual move restarts the 5s clock.
    const t = setInterval(() => { setDir(1); setI((c) => (c + 1) % n); }, ROTATE_MS);
    return () => clearInterval(t);
  }, [paused, i, n]);

  const fig = DOOR_FIGURES[i];

  return (
    <div className="door-figure"
         onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)}
         onFocusCapture={() => setPaused(true)} onBlurCapture={() => setPaused(false)}>
      {/* Re-keyed on index so each change remounts and replays the slide-in;
          the ochre .fig-rule matches the chat answer's figure device. */}
      <div className={"door-fig-slide " + (dir >= 0 ? "fwd" : "back")} key={i}>
        <span className="field-label">{fig.label}</span>
        <div className="figure num">{fig.value}</div>
        <div className="fig-rule" aria-hidden="true" />
        <div className="door-figure-src">{fig.source}</div>
      </div>
      <div className="door-figure-nav">
        <button type="button" className="dfn-arrow" aria-label="Previous example"
                onClick={() => go(-1)}><IconChevronLeft size={18} /></button>
        <span className="dfn-dots">
          {DOOR_FIGURES.map((_, k) => (
            <button key={k} type="button"
                    className={"dfn-dot" + (k === i ? " on" : "")}
                    aria-label={`Show example ${k + 1} of ${n}`}
                    aria-current={k === i ? "true" : undefined}
                    onClick={() => move(k, k >= i ? 1 : -1)} />
          ))}
        </span>
        <button type="button" className="dfn-arrow" aria-label="Next example"
                onClick={() => go(1)}><IconChevronRight size={18} /></button>
      </div>
    </div>
  );
}

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
          <DoorFigures />
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
