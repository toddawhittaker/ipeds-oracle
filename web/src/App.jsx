import React, { useEffect, useState } from "react";
import { api } from "./api.js";
import Login from "./Login.jsx";
import Chat from "./Chat.jsx";
import Admin from "./Admin.jsx";
import Verify from "./Verify.jsx";

const isVerifyRoute = () => window.location.pathname === "/verify";

function currentTheme() {
  const forced = document.documentElement.getAttribute("data-theme");
  if (forced) return forced;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export default function App() {
  const [user, setUser] = useState(undefined); // undefined=loading, null=logged out
  const [view, setView] = useState("chat");
  const [theme, setTheme] = useState(currentTheme);

  useEffect(() => {
    if (isVerifyRoute()) return; // the /verify page manages its own flow
    api.me().then((u) => {
      setUser(u);
      // A fresh deploy with no dataset yet: route an admin straight to
      // Admin -> Imports on load (once) rather than leaving them on an
      // empty Chat with no obvious way to fix it. They can still navigate
      // freely afterward.
      if (u?.is_admin && !u.has_data) setView("admin");
    }).catch(() => setUser(null));
  }, []);

  // Re-fetch /me after a successful data-import swap so a fresh-deploy
  // admin's has_data flips true without a full page reload. Deliberately NOT
  // wired into a useEffect keyed on `user` -- the initial no-data->Admin
  // routing above only runs once, on load, so this later refresh updates
  // `user.has_data` without yanking the admin's current view/tab around.
  const refreshMe = () => api.me().then(setUser).catch(() => {});

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
  }

  if (isVerifyRoute()) return <Verify />;
  if (user === undefined) return <div className="center muted">Loading…</div>;
  if (!user) return <Login onDone={() => api.me().then(setUser).catch(() => {})} />;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand"><span className="wordmark" role="img" aria-label="IPEDS Query" /></div>
        <nav className="tabs" aria-label="Primary">
          <button className={view === "chat" ? "on" : ""} aria-current={view === "chat" ? "page" : undefined}
                  onClick={() => setView("chat")}>Chat</button>
          {user.is_admin && (
            <button className={view === "admin" ? "on" : ""} aria-current={view === "admin" ? "page" : undefined}
                    onClick={() => setView("admin")}>Admin</button>
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
      {view === "chat"
        ? <Chat me={user} />
        : <Admin me={user} initialTab={user.is_admin && !user.has_data ? "imports" : "allowlist"}
                 onDataChanged={refreshMe} />}
    </div>
  );
}
