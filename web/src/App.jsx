import React, { useEffect, useRef, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { api } from "./api.js";
import Login from "./Login.jsx";
import Chat from "./Chat.jsx";
import { AdminRoute } from "./Admin.jsx";
import Verify from "./Verify.jsx";

function currentTheme() {
  const forced = document.documentElement.getAttribute("data-theme");
  if (forced) return forced;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

// A11y (code review MEDIUM): wording is intentionally loose -- callers only
// assert a non-empty substring, never a full string match. Covers three
// otherwise-silent navigations: swapping Chat<->Admin's main content, the
// /admin -> /admin/:tab redirects (index tab and unknown-tab), and a
// non-admin's /admin/x -> / bounce.
function routeAnnouncement(pathname) {
  if (pathname === "/admin" || pathname.startsWith("/admin/")) {
    const tab = pathname.split("/")[2];
    return tab ? `Admin — ${tab[0].toUpperCase() + tab.slice(1)}` : "Admin";
  }
  return "Chat";
}

// /verify manages its own flow (peek-then-confirm a magic-link token) and
// must NEVER trigger the "am I logged in" check below -- mounting Verify
// INSTEAD OF Shell (rather than routing to it inside Shell) is what keeps
// Shell's api.me() effect from ever running while a token is being verified.
export default function App() {
  const { pathname } = useLocation();
  if (pathname === "/verify") return <Verify />;
  return <Shell />;
}

function Shell() {
  const [user, setUser] = useState(undefined); // undefined=loading, null=logged out
  const [theme, setTheme] = useState(currentTheme);
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const landedAt = useRef(pathname);
  // Route announcer (a11y, code review MEDIUM): same always-mounted mechanic
  // as Admin.jsx's flash live region (Admin.jsx:258) and the Chat
  // bad-conversation-notice fix -- but here it's a plain derived value, not
  // effect+state. Shell (and its children) stay mounted across every
  // client-side nav -- Chat<->Admin swaps main content but the announcer
  // node below is never unmounted -- so a plain render mutates the same
  // already-committed node exactly the same as an effect would, one render
  // sooner and without react-hooks/set-state-in-effect noise. The one path
  // where mounting timing actually matters -- a direct page load of, say,
  // /chat/abc -- isn't helped by an effect(+setTimeout) either: the mutation
  // still lands inside the initial-load window screen readers swallow.
  const routeAnnounce = routeAnnouncement(pathname);

  useEffect(() => {
    api.me().then((u) => {
      setUser(u);
      // A fresh deploy with no dataset yet: route an admin straight to
      // Admin -> Imports on load (once) rather than leaving them on an
      // empty Chat with no obvious way to fix it. They can still navigate
      // freely afterward. Scoped to landing on bare "/" -- a deep link to
      // /chat/:id or another /admin/:tab must not be yanked out from under
      // the admin, and this must never re-fire on a later refreshMe(). No
      // separate "already onboarded" ref is needed to guard that: this
      // effect's `[]` dep array (and the absence of StrictMode) already
      // means the body runs exactly once per mount, so landedAt.current
      // alone is sufficient -- refreshMe() is a distinct, separately-called
      // function (see below) that never re-runs this effect.
      if (u?.is_admin && !u.has_data && landedAt.current === "/") {
        navigate("/admin/imports", { replace: true });
      }
    }).catch(() => setUser(null));
    // Deliberately NOT depending on `navigate`: react-router v6's
    // useNavigate() returns a NEW function identity whenever the pathname
    // changes (it closes over useLocation() internally), and the very
    // navigate() call above changes the pathname -- so a `[navigate]` dep
    // array would re-run this effect a second time on mount and double-fetch
    // /me. Every navigate() target used anywhere in this file is an absolute
    // path ("/", "/admin/...", "/chat/..."), for which react-router never
    // consults the (possibly-stale) closed-over location, so reusing the
    // mount-time `navigate` here is safe.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-fetch /me after a successful data-import swap so a fresh-deploy
  // admin's has_data flips true without a full page reload. Deliberately NOT
  // wired into a useEffect keyed on `user` -- the initial no-data->Admin
  // routing above only runs once, on load, so this later refresh updates
  // `user.has_data` without yanking the admin's current view/URL around.
  // CRITICAL: keep this imperative (a plain .then(setUser)), never
  // declarative (e.g. `{!user.has_data && <Navigate .../>}` in the JSX
  // below) -- a declarative redirect re-evaluates on EVERY render and would
  // re-fire on this very refresh, yanking the admin's view right after an
  // import completes. That was the exact bug this comment (and its
  // pre-router predecessor) was written to prevent.
  const refreshMe = () => api.me().then(setUser).catch(() => {});

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
  }

  if (user === undefined) return <div className="center muted">Loading…</div>;
  if (!user) return <Login onDone={() => api.me().then(setUser).catch(() => {})} />;

  const onAdmin = pathname === "/admin" || pathname.startsWith("/admin/");
  // UX-only guard: an unauthorized viewer bounces straight back to "/" and
  // never renders (or fetches data for) an admin panel. The real security
  // boundary is server-side -- app/routers/admin.py's require_admin -- this
  // just keeps the URL/UI honest for a non-admin who deep-links here.
  const adminOnly = (el) => (user.is_admin ? el : <Navigate to="/" replace />);

  return (
    <div className="app">
      {/* No role="status" here, deliberately -- several OTHER live regions on
          these pages already use it (Admin's flash box, Skills' status
          region, Chat's bad-conversation notice), and several existing e2e
          specs assert on an UNSCOPED page.getByRole("status") expecting
          exactly one match. aria-live alone still makes this a real live
          region; the testid is how tests target it unambiguously. */}
      <div className="sr-only" aria-live="polite" data-testid="route-announcer">
        {routeAnnounce}
      </div>
      <header className="topbar">
        <div className="brand"><span className="wordmark" role="img" aria-label="IPEDS Query" /></div>
        <nav className="tabs" aria-label="Primary">
          <button className={onAdmin ? "" : "on"} aria-current={onAdmin ? undefined : "page"}
                  onClick={() => navigate("/")}>Chat</button>
          {user.is_admin && (
            <button className={onAdmin ? "on" : ""} aria-current={onAdmin ? "page" : undefined}
                    onClick={() => navigate("/admin")}>Admin</button>
          )}
        </nav>
        <div className="userbox">
          <button className="link theme-toggle" onClick={toggleTheme}
                  title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
                  aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}>
            {theme === "dark" ? "☀️" : "🌙"}
          </button>
          <span className="muted">{user.email}</span>
          <button className="link" onClick={async () => { await api.logout(); setUser(null); }}>
            Sign out
          </button>
        </div>
      </header>
      <Routes>
        {/* key="chat" is a defensive pin, not what actually keeps Chat
            mounted across a "/" <-> "/chat/:id" URL change: react-router's
            _renderMatches creates the route element with no key of its own,
            and React reconciles a single non-array same-type child at the
            same position as an UPDATE regardless -- Chat would survive the
            swap without this. What genuinely matters for the live SSE stream
            surviving that URL flip is the auto-batching described at
            Chat.jsx (submit()'s `conversation` event handler) -- see the
            v7_startTransition warning there. */}
        <Route path="/" element={<Chat key="chat" me={user} />} />
        <Route path="/chat/:id" element={<Chat key="chat" me={user} />} />
        {/* Code-review LOW: a no-data admin who clicks Chat then Admin must
            land back on Imports, not an empty Users tab -- Chat's own
            no-data CTA (Chat.jsx) tells them to go to Admin -> Imports. An
            admin WITH data keeps landing on Users, unchanged. */}
        <Route path="/admin" element={adminOnly(
          <Navigate to={user.has_data ? "/admin/users" : "/admin/imports"} replace />,
        )} />
        <Route path="/admin/:tab" element={adminOnly(<AdminRoute me={user} onDataChanged={refreshMe} />)} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  );
}
