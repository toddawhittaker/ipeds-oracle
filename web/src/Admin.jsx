import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";

export default function Admin() {
  const [tab, setTab] = useState("allowlist");
  return (
    <main className="admin">
      <h1 className="sr-only">Admin</h1>
      <nav className="subtabs" aria-label="Admin sections">
        {["allowlist", "imports", "usage", "skills"].map((t) => (
          <button key={t} className={tab === t ? "on" : ""}
                  aria-current={tab === t ? "page" : undefined}
                  onClick={() => setTab(t)}>
            {t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </nav>
      {tab === "allowlist" && <Allowlist />}
      {tab === "imports" && <Imports />}
      {tab === "usage" && <Usage />}
      {tab === "skills" && <Skills />}
    </main>
  );
}

function Allowlist() {
  const [rows, setRows] = useState([]);
  const [reqs, setReqs] = useState([]);
  const [email, setEmail] = useState("");
  const [note, setNote] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);

  const load = () => {
    api.allowlist().then(setRows);
    api.accessRequests().then(setReqs);
  };
  useEffect(load, []);

  async function add(e) {
    e.preventDefault();
    await api.addAllow(email, note, isAdmin);
    setEmail(""); setNote(""); setIsAdmin(false); load();
  }

  return (
    <div className="panel">
      {reqs.length > 0 && (
        <div className="requests">
          <h2>Pending access requests</h2>
          {reqs.map((r) => (
            <div key={r.id} className="req">
              <span>{r.email}</span>
              <button onClick={() => api.addAllow(r.email, "approved request", false).then(load)}>
                Approve
              </button>
            </div>
          ))}
        </div>
      )}

      <h2>Allowlist</h2>
      <form className="row" onSubmit={add}>
        <label htmlFor="allow-email" className="sr-only">Email</label>
        <input id="allow-email" type="email" placeholder="email" required value={email}
               onChange={(e) => setEmail(e.target.value)} />
        <label htmlFor="allow-note" className="sr-only">Note</label>
        <input id="allow-note" placeholder="note (optional)" value={note}
               onChange={(e) => setNote(e.target.value)} />
        <label className="chk">
          <input type="checkbox" checked={isAdmin}
                 onChange={(e) => setIsAdmin(e.target.checked)} /> admin
        </label>
        <button type="submit">Add</button>
      </form>

      <table className="grid">
        <thead><tr><th scope="col">Email</th><th scope="col">Note</th><th scope="col">Admin</th><th scope="col">Last login</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.email}>
              <td>{r.email}</td>
              <td>{r.note}</td>
              <td>{r.is_admin ? "✓" : ""}</td>
              <td>{r.last_login ? new Date(r.last_login * 1000).toLocaleDateString() : "—"}</td>
              <td><button className="link danger"
                          onClick={() => api.removeAllow(r.email).then(load)}>remove</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Imports() {
  const [jobs, setJobs] = useState([]);
  const [active, setActive] = useState(null);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef();
  const poll = useRef();

  const loadJobs = () => api.importJobs().then(setJobs);
  useEffect(() => { loadJobs(); return () => clearInterval(poll.current); }, []);

  async function upload(e) {
    e.preventDefault();
    const f = fileRef.current.files[0];
    if (!f) return;
    setUploading(true);
    const fd = new FormData();
    fd.append("file", f);
    const r = await fetch("/api/admin/import", { method: "POST", body: fd });
    const data = await r.json();
    setUploading(false);
    if (data.job_id) watch(data.job_id);
    loadJobs();
  }

  function watch(id) {
    clearInterval(poll.current);
    const tick = async () => {
      const job = await api.importJob(id);
      setActive(job);
      if (["passed", "failed", "swapped"].includes(job.status)) {
        clearInterval(poll.current);
        loadJobs();
      }
    };
    tick();
    poll.current = setInterval(tick, 2000);
  }

  return (
    <div className="panel">
      <h2>Load a new IPEDS year</h2>
      <p className="muted small">
        Upload the year&apos;s <code>IPEDS{"{YYYY}{YY}"}.accdb</code>. It rebuilds into
        a staging database, runs integrity + magnitude checks, and only swaps in
        if everything passes — the live database is never touched until then.
      </p>
      <form className="row" onSubmit={upload}>
        <label htmlFor="import-file" className="sr-only">IPEDS Access database file</label>
        <input id="import-file" ref={fileRef} type="file" accept=".accdb" required />
        <button type="submit" disabled={uploading}>{uploading ? "Uploading…" : "Import"}</button>
      </form>

      {active && (
        <div className="job" aria-live="polite">
          <div className={"badge " + active.status}>{active.status}</div>
          {active.report && <pre className="report">{active.report}</pre>}
          <details open>
            <summary>Log</summary>
            <pre className="log">{active.log || "…"}</pre>
          </details>
        </div>
      )}

      <h3>Recent jobs</h3>
      <table className="grid">
        <thead><tr><th scope="col">#</th><th scope="col">File</th><th scope="col">Status</th><th scope="col">When</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
        <tbody>
          {jobs.map((jb) => (
            <tr key={jb.id}>
              <td>{jb.id}</td><td>{jb.filename}</td>
              <td><span className={"badge " + jb.status}>{jb.status}</span></td>
              <td>{new Date(jb.updated_at * 1000).toLocaleString()}</td>
              <td><button className="link" onClick={() => watch(jb.id)}>view</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Usage() {
  const [u, setU] = useState(null);
  useEffect(() => { api.usage().then(setU); }, []);
  if (!u) return <div className="panel muted">Loading…</div>;
  const t = u.totals;
  return (
    <div className="panel">
      <h2 className="sr-only">Usage</h2>
      <div className="stats">
        <Stat label="Queries" value={t.queries} />
        <Stat label="Tokens" value={(t.tokens || 0).toLocaleString()} />
        <Stat label="Cache hits" value={t.cache_hits} />
        <Stat label="Escalations" value={t.escalations} />
        <Stat label="Failures" value={t.failures} />
      </div>
      <h3>Last 30 days</h3>
      <table className="grid">
        <thead><tr><th scope="col">Day</th><th scope="col">Queries</th><th scope="col">Tokens</th></tr></thead>
        <tbody>{u.by_day.map((d) => (
          <tr key={d.day}><td>{d.day}</td><td>{d.queries}</td><td>{d.tokens.toLocaleString()}</td></tr>
        ))}</tbody>
      </table>
      <h3>Top users</h3>
      <table className="grid">
        <thead><tr><th scope="col">User</th><th scope="col">Queries</th></tr></thead>
        <tbody>{u.top_users.map((x) => (
          <tr key={x.email}><td>{x.email}</td><td>{x.queries}</td></tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function Stat({ label, value }) {
  return <div className="stat"><div className="v">{value}</div><div className="l">{label}</div></div>;
}

function Skills() {
  const [rows, setRows] = useState([]);
  const load = () => api.skills().then(setRows);
  useEffect(() => { load(); }, []);
  return (
    <div className="panel">
      <h2>Learned skills ({rows.length})</h2>
      <p className="muted small">Validated NL→SQL exemplars used as few-shot context. 👍 on answers promotes new ones.</p>
      {rows.map((s) => (
        <div key={s.id} className="skill">
          <div className="skill-head">
            <strong>{s.question}</strong>
            <span className="tags">
              {s.verified ? <span className="tag ok">verified</span> : <span className="tag">unverified</span>}
              <span className="tag">▲{s.upvotes} ▼{s.downvotes}</span>
              <span className="tag">hits {s.hits}</span>
            </span>
          </div>
          <pre>{s.canonical_sql}</pre>
          {s.notes && <div className="muted small">{s.notes}</div>}
          <div className="msg-actions">
            <button className="link" onClick={() => api.patchSkill(s.id, { verified: !s.verified }).then(load)}>
              {s.verified ? "unverify" : "verify"}
            </button>
            <button className="link danger" onClick={() => api.deleteSkill(s.id).then(load)}>delete</button>
          </div>
        </div>
      ))}
    </div>
  );
}
