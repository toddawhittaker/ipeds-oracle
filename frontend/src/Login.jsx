import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { authErrorMessage } from "./authcopy.js";
import { loginMethod, ssoButtonLabel } from "./loginmethod.js";
import Wordmark from "./Wordmark.jsx";
import { IconChevronLeft, IconChevronRight, IconPause, IconPlay } from "./icons.jsx";

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
// How long to wait for /api/auth/config before drawing the magic-link door
// anyway. Long enough that a slow-but-working server still picks the right
// form; short enough that a hung one does not strand a visitor.
const CONFIG_TIMEOUT_MS = 8000;

// The door's hero statistic as an auto-advancing gallery (5s each) with manual
// ‹ ··· › controls and an explicit pause/play toggle. Rotation stops while the
// gallery is hovered or holds keyboard focus, while the sign-in form is focused
// (`externalPaused`), while the user has pressed pause, and never auto-starts
// under a reduced-motion preference (observed live) — a persistent, durable
// Pause/Stop mechanism (WCAG 2.2.2). The arrows/dots/toggle work in every case,
// grouped and labelled so a screen reader knows they page the figure (1.3.1).
function DoorFigures({ externalPaused = false }) {
  const [i, setI] = useState(0);
  const [dir, setDir] = useState(1); // slide direction: 1 = forward, -1 = back
  const [hovering, setHovering] = useState(false);
  const [userPaused, setUserPaused] = useState(false);
  const [reduce, setReduce] = useState(
    () => !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
  const n = DOOR_FIGURES.length;
  const stopped = hovering || userPaused || externalPaused || reduce;

  const move = (target, d) => { setDir(d); setI(target); };
  const go = (d) => move((i + d + n) % n, d);

  // Observe reduced-motion so toggling it mid-session starts/stops rotation.
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!mq) return undefined;
    const on = () => setReduce(mq.matches);
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);

  useEffect(() => {
    if (stopped) return undefined;
    // Re-armed on every index change too, so a manual move restarts the 5s clock.
    const t = setInterval(() => { setDir(1); setI((c) => (c + 1) % n); }, ROTATE_MS);
    return () => clearInterval(t);
  }, [stopped, i, n]);

  const fig = DOOR_FIGURES[i];

  return (
    <div className="door-figure"
         onMouseEnter={() => setHovering(true)} onMouseLeave={() => setHovering(false)}
         onFocusCapture={() => setHovering(true)} onBlurCapture={() => setHovering(false)}>
      {/* Re-keyed on index so each change remounts and replays the slide-in;
          the ochre .fig-rule matches the chat answer's figure device. */}
      <div className={"door-fig-slide " + (dir >= 0 ? "fwd" : "back")} key={i}>
        <span className="field-label">{fig.label}</span>
        <div className="figure num">{fig.value}</div>
        <div className="fig-rule" aria-hidden="true" />
        <div className="door-figure-src">{fig.source}</div>
      </div>
      <div className="door-figure-nav" role="group" aria-label="Example statistics">
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
        <button type="button" className="dfn-arrow dfn-toggle" aria-pressed={userPaused}
                aria-label={userPaused ? "Resume auto-rotation" : "Pause auto-rotation"}
                onClick={() => setUserPaused((p) => !p)}>
          {userPaused ? <IconPlay size={15} /> : <IconPause size={15} />}
        </button>
      </div>
    </div>
  );
}

// `notice` seeds the message slot from outside — App.jsx passes it when the door
// is being shown BECAUSE something happened (an expired session, an unreachable
// server) rather than because the visitor arrived logged out. Without it, all
// three arrivals looked identical and silent, so "my session expired" was
// indistinguishable from "I signed out" and from "the backend is down".
export default function Login({ notice = "" }) {
  const [email, setEmail] = useState("");
  // An SSO failure comes back as ?auth_error=<code> on the door. Read it during
  // the FIRST render rather than in an effect, so the message is on screen in
  // the first paint instead of appearing a frame later. The code is looked up in
  // a closed map; an unrecognised one renders OUR generic wording, never text
  // that arrived in the query string.
  const [msg, setMsg] = useState(() => {
    const code = new URLSearchParams(window.location.search).get("auth_error");
    return code ? authErrorMessage(code) : (notice || null);
  });
  const [ok, setOk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState(FALLBACK_HINT);
  // null until /api/auth/config answers. The form slot stays EMPTY until then --
  // rendering the magic-link form first and swapping it for an SSO button a
  // moment later is a flash of the wrong door, and on a slow connection it is
  // long enough to type an email into a field that is about to vanish.
  const [method, setMethod] = useState(null);
  // No default string here: `ssoButtonLabel` owns the fallback and runs before
  // the button can render, so a second copy could only ever drift from it.
  const [ssoLabel, setSsoLabel] = useState("");
  const [ssoBusy, setSsoBusy] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  // Pause the gallery while the sign-in card holds focus — the input autoFocuses
  // on load, so the specimens don't slide in the user's peripheral vision at the
  // exact moment they're reading the instructions and typing their email.
  const [cardFocused, setCardFocused] = useState(false);
  const noticeRef = useRef(null);

  // An `?auth_error=` message is present in the FIRST paint, and a role="alert"
  // whose text is already there when the region mounts is announced
  // unreliably. Move focus to it instead, which is also where a keyboard user
  // needs to be — the SSO card's only other control is the button they are
  // about to press again.
  const [arrivedWithError] = useState(
    () => !!new URLSearchParams(window.location.search).get("auth_error"));
  useEffect(() => {
    if (ok || arrivedWithError) noticeRef.current?.focus();
  }, [ok, arrivedWithError]);

  useEffect(() => {
    // The domain is a hint only. The METHOD decides which form renders, and a
    // failure here falls back to magic_link — the door that still works when the
    // server is half-reachable, and the only one gated by the allowlist.
    // Raced against a timeout because the form slot renders NOTHING until this
    // resolves, and `api.js`'s fetch has no timeout of its own — a proxy holding
    // the connection open would otherwise leave a wordmark, some marketing copy
    // and no way to sign in, indefinitely and with no error. Losing the race
    // falls back to magic_link, the same direction as an outright failure.
    let settled = false;
    const fallback = setTimeout(() => {
      if (!settled) { settled = true; setMethod("magic_link"); }
    }, CONFIG_TIMEOUT_MS);
    api.publicConfig()
      .then((c) => {
        clearTimeout(fallback);
        if (settled) return;
        settled = true;
        if (c.email_domain) setHint(`you@${c.email_domain}`);
        setMethod(loginMethod(c));
        setSsoLabel(ssoButtonLabel(c));
      })
      .catch(() => {
        clearTimeout(fallback);
        if (!settled) { settled = true; setMethod("magic_link"); }
      });
    return () => clearTimeout(fallback);
  }, []);

  // Strip the code from the URL once it has been read — the same hygiene
  // Verify.jsx does for its token, so a reload or a copied link doesn't re-raise
  // an error that has already been dealt with.
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("auth_error")) {
      window.history.replaceState({}, "", window.location.pathname);
    }
  }, []);

  async function submitLdap(e) {
    e.preventDefault();
    if (busy) return;
    // Clear the previous message FIRST. The server answers every rejection with
    // the same sentence by design, so on a second wrong password setMsg would
    // receive a string equal to current state, React would bail out, and the
    // role="alert" text would never change — announcing nothing to a screen
    // reader on the one door where repeated failures are the ordinary case.
    setMsg(null);
    setBusy(true);
    try {
      await api.ldapSignIn(username, password);
      // Full reload rather than a state update, so the Shell re-runs api.me()
      // and every downstream view sees a signed-in user — the same thing
      // Verify.jsx does after consuming a magic link.
      window.location.assign("/");
    } catch (err) {
      // The server answers every rejection identically on purpose (a varying
      // message is a directory enumeration oracle), so this shows its sentence
      // rather than inventing a more specific one.
      setMsg(err?.detail || "Sign-in failed. Check your username and password.");
      setOk(false);
      setBusy(false);
    }
  }

  async function startSso() {
    if (ssoBusy) return;   // the aria-disabled half — see the button
    setSsoBusy(true);
    try {
      const r = await api.oidcStart();
      // Leaving the app entirely, so `ssoBusy` is deliberately not reset — the
      // button must not flick back to enabled while the browser navigates.
      window.location.assign(r.authorization_url);
    } catch (e) {
      setMsg(e?.detail
        || "Single sign-on is unavailable right now. Try again in a moment.");
      setOk(false);
      setSsoBusy(false);
    }
  }

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
          <DoorFigures externalPaused={cardFocused} />
        </div>

        {/* Right: the reader's card. Focus anywhere inside it pauses the gallery. */}
        <div className="card login door-right"
             onFocusCapture={() => setCardFocused(true)}
             onBlurCapture={() => setCardFocused(false)}>
          <h1><Wordmark /></h1>
          {method === "oidc" && (
            <p className="muted">
              Access is managed by your institution&apos;s single sign-on.
            </p>
          )}
          {method === "ldap" && (
            <p className="muted">
              Sign in with your institution directory username and password.
            </p>
          )}
          {method === "magic_link" && (
            <p className="muted">
              Access is by invitation. We&apos;ll email a one-time sign-in link —
              no password to remember.
            </p>
          )}
          {msg && (
            <div className={"notice " + (ok ? "ok" : "error")} role="alert"
                 tabIndex={-1} ref={noticeRef}>{msg}</div>
          )}
          {!ok && method === "oidc" && (
            // aria-disabled + an early return, never `disabled`: disabling the
            // control the user just activated moves focus to <body>, and on a
            // failure this is the card's only control — a keyboard user would
            // have to Tab from the top of the document, past the figure
            // gallery, to try again. Same pattern as Keys.jsx and Chat.jsx.
            <button type="button" onClick={startSso} aria-disabled={ssoBusy}>
              {ssoBusy ? "Redirecting…" : ssoLabel}
            </button>
          )}
          {method === "ldap" && (
            <form onSubmit={submitLdap}>
              <label htmlFor="ldap-username" className="sr-only">Username</label>
              <input
                id="ldap-username" type="text" required autoComplete="username"
                placeholder="Username" autoFocus
                value={username} onChange={(e) => setUsername(e.target.value)}
              />
              <label htmlFor="ldap-password" className="sr-only">Password</label>
              <input
                id="ldap-password" type="password" required
                autoComplete="current-password" placeholder="Password"
                value={password} onChange={(e) => setPassword(e.target.value)}
              />
              <button type="submit" aria-disabled={busy}>
                {busy ? "Signing in…" : "Sign in"}
              </button>
            </form>
          )}
          {!ok && method === "magic_link" && (
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
          {(method === "oidc" || method === "ldap") && (
            <p className="door-fineprint muted small">
              Trouble signing in? Your administrator manages who has access.
            </p>
          )}
          {method === "magic_link" && (
            <p className="door-fineprint muted small">
              Not on the list? Request access with your institution email and an
              administrator will review it.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
