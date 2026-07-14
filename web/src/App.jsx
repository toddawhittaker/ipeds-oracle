import React, { useEffect, useState } from "react";
import { api } from "./api.js";
import Login from "./Login.jsx";
import Chat from "./Chat.jsx";
import Admin from "./Admin.jsx";

export default function App() {
  const [user, setUser] = useState(undefined); // undefined=loading, null=logged out
  const [view, setView] = useState("chat");

  useEffect(() => {
    api.me().then(setUser).catch(() => setUser(null));
  }, []);

  if (user === undefined) return <div className="center muted">Loading…</div>;
  if (!user) return <Login onDone={() => api.me().then(setUser).catch(() => {})} />;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">IPEDS Query</div>
        <nav className="tabs">
          <button className={view === "chat" ? "on" : ""} onClick={() => setView("chat")}>Chat</button>
          {user.is_admin && (
            <button className={view === "admin" ? "on" : ""} onClick={() => setView("admin")}>Admin</button>
          )}
        </nav>
        <div className="userbox">
          <span className="muted">{user.email}</span>
          <button className="link" onClick={async () => { await api.logout(); setUser(null); }}>
            Sign out
          </button>
        </div>
      </header>
      {view === "chat" ? <Chat /> : <Admin />}
    </div>
  );
}
