import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api.js";
import Chart from "./Chart.jsx";

export default function Admin() {
  const [tab, setTab] = useState("allowlist");
  return (
    <main className="admin">
      <h1 className="sr-only">Admin</h1>
      <nav className="subtabs" aria-label="Admin sections">
        {["allowlist", "imports", "usage", "skills", "logs"].map((t) => (
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
      {tab === "logs" && <Logs />}
    </main>
  );
}

function Allowlist() {
  const [rows, setRows] = useState([]);
  const [reqs, setReqs] = useState([]);
  const [email, setEmail] = useState("");
  const [note, setNote] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [flash, setFlash] = useState("");

  const load = () => {
    api.allowlist().then(setRows);
    api.accessRequests().then(setReqs);
  };
  useEffect(load, []);

  async function invite(addr, noteText, admin = false) {
    const res = await api.addAllow(addr, noteText, admin).catch(() => ({}));
    setFlash(res?.invited
      ? `Approved — a sign-in link was emailed to ${addr}.`
      : `${addr} added. (No email was sent — the sign-in link is in the server log.)`);
    load();
  }

  async function add(e) {
    e.preventDefault();
    await invite(email, note, isAdmin);
    setEmail(""); setNote(""); setIsAdmin(false);
  }

  return (
    <div className="panel">
      {flash && <div className="notice" role="status">{flash}</div>}
      {reqs.length > 0 && (
        <div className="requests">
          <h2>Pending access requests</h2>
          {reqs.map((r) => (
            <div key={r.id} className="req">
              <span>{r.email}</span>
              <button onClick={() => invite(r.email, "approved request", false)}>
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

const RANGES = [
  { key: "hour", label: "Hour", secs: 3600 },
  { key: "day", label: "Day", secs: 86400 },
  { key: "7d", label: "7 days", secs: 7 * 86400 },
  { key: "30d", label: "30 days", secs: 30 * 86400 },
];
const money = (v) => "$" + Number(v || 0).toFixed(Number(v) >= 1 ? 2 : 4);

const METRICS = ["queries", "tokens", "spend"];

function Usage() {
  const [range, setRange] = useState("7d");
  const [custom, setCustom] = useState({ since: "", until: "" });
  const [metric, setMetric] = useState("tokens");
  const [u, setU] = useState(null);
  const [loading, setLoading] = useState(true);

  // `<input type=date>` values are parsed as LOCAL midnight (matching the local
  // "now" used by the quick ranges), not UTC, so the window aligns with the
  // admin's day.
  useEffect(() => {
    const now = Date.now() / 1000;
    let since, until;
    if (range === "custom") {
      since = custom.since ? new Date(`${custom.since}T00:00:00`).getTime() / 1000 : now - 7 * 86400;
      until = custom.until ? new Date(`${custom.until}T23:59:59`).getTime() / 1000 : now;
    } else {
      since = now - RANGES.find((r) => r.key === range).secs;
      until = now;
    }
    api.usage(since, until).then(setU).catch(() => {}).finally(() => setLoading(false));
  }, [range, custom]);

  const pick = (fn) => { setLoading(true); fn(); };
  const t = u?.totals || {};
  const spec = useMemo(() => {
    const s = u?.series || [];
    return s.length ? {
      type: "line", x: "t", y: [metric], yLabel: metric === "spend" ? "USD" : metric,
      data: s.map((r) => ({ t: r.t, queries: r.queries, tokens: r.tokens, spend: Number(r.spend) })),
    } : null;
  }, [u, metric]);

  return (
    <div className="panel">
      <h2 className="sr-only">Usage</h2>
      <div className="usage-range" role="group" aria-label="Time range">
        {RANGES.map((r) => (
          <button key={r.key} className={range === r.key ? "on" : ""} aria-pressed={range === r.key}
                  onClick={() => pick(() => setRange(r.key))}>{r.label}</button>
        ))}
        <button className={range === "custom" ? "on" : ""} aria-pressed={range === "custom"}
                onClick={() => pick(() => setRange("custom"))}>Custom</button>
        {range === "custom" && (
          <span className="usage-custom">
            <input type="date" value={custom.since} aria-label="From date"
                   onChange={(e) => pick(() => setCustom((c) => ({ ...c, since: e.target.value })))} />
            <span className="muted">to</span>
            <input type="date" value={custom.until} aria-label="To date"
                   onChange={(e) => pick(() => setCustom((c) => ({ ...c, until: e.target.value })))} />
          </span>
        )}
        {loading && u && <span className="muted small">updating…</span>}
      </div>

      {!u ? <div className="muted">Loading…</div> : (
        <div className={"usage-body" + (loading ? " updating" : "")}>
          <div className="stats">
            <Stat label="Queries" value={(t.queries || 0).toLocaleString()} />
            <Stat label="Tokens" value={(t.tokens || 0).toLocaleString()} />
            <Stat label="Spend" value={money(t.spend)} />
            <Stat label="Cache hits" value={t.cache_hits || 0} />
            <Stat label="Escalations" value={t.escalations || 0} />
            <Stat label="Failures" value={t.failures || 0} />
          </div>

          <div className="usage-chart-head">
            <h3>{metric[0].toUpperCase() + metric.slice(1)} over time ({u.bucket === "hour" ? "hourly" : "daily"})</h3>
            <div className="chart-types" role="group" aria-label="Metric">
              {METRICS.map((m) => (
                <button key={m} className={metric === m ? "on" : ""} aria-pressed={metric === m}
                        onClick={() => setMetric(m)}>{m[0].toUpperCase() + m.slice(1)}</button>
              ))}
            </div>
          </div>
          {spec ? <Chart spec={spec} /> : <div className="muted">No activity in this range.</div>}

          <h3>Top users</h3>
          <table className="grid">
            <thead><tr>
              <th scope="col">User</th><th scope="col">Queries</th>
              <th scope="col">Tokens</th><th scope="col">Spend</th>
            </tr></thead>
            <tbody>{u.top_users.map((x) => (
              <tr key={x.email}>
                <td>{x.email}</td><td>{x.queries}</td>
                <td>{(x.tokens || 0).toLocaleString()}</td><td>{money(x.spend)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
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

function Logs() {
  const [records, setRecords] = useState([]);
  const [level, setLevel] = useState("");
  const [q, setQ] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [auto, setAuto] = useState(true);

  const load = useCallback(() => {
    const since = from ? Math.floor(new Date(`${from}T00:00:00`).getTime() / 1000) : null;
    const until = to ? Math.floor(new Date(`${to}T23:59:59.999`).getTime() / 1000) : null;
    api.logs(500, level, q.trim(), since, until)
      .then((d) => setRecords(d.records || []))
      .catch(() => {});
  }, [level, q, from, to]);

  // Debounced load on any filter change (also the initial load).
  useEffect(() => {
    const t = setTimeout(load, 250);
    return () => clearTimeout(t);
  }, [load]);
  useEffect(() => {
    if (!auto) return undefined;
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [auto, load]);

  const clearFilters = () => { setQ(""); setFrom(""); setTo(""); setLevel(""); };
  const filtered = level || q.trim() || from || to;
  const fmt = (ts) => new Date(ts * 1000).toLocaleString();
  return (
    <div className="panel">
      <h2>Server logs</h2>
      <p className="muted small">
        Persisted across restarts (newest at the bottom). Filter by level, search
        message text, or pick a date range.
      </p>
      <div className="row">
        <label className="chk">Level:&nbsp;
          <select value={level} onChange={(e) => setLevel(e.target.value)}>
            <option value="">all</option>
            <option value="INFO">info</option>
            <option value="WARNING">warning</option>
            <option value="ERROR">error</option>
          </select>
        </label>
        <input
          type="search"
          className="logsearch"
          placeholder="Search message text…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          aria-label="Search log messages"
        />
        <label className="chk">From:&nbsp;
          <input type="date" value={from} max={to || undefined}
            onChange={(e) => setFrom(e.target.value)} aria-label="From date" />
        </label>
        <label className="chk">To:&nbsp;
          <input type="date" value={to} min={from || undefined}
            onChange={(e) => setTo(e.target.value)} aria-label="To date" />
        </label>
        <label className="chk">
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
          auto-refresh
        </label>
        <button onClick={load}>Refresh</button>
        {filtered && <button onClick={clearFilters}>Clear filters</button>}
      </div>
      <div className="log logbox thin-scroll">
        {records.length === 0
          ? <div className="muted">{filtered ? "No matching log records." : "No log records."}</div>
          : records.map((r, i) => (
            <div key={i} className={"logline lvl-" + r.level}>
              <span className="logts">{fmt(r.ts)}</span>
              <span className="loglvl">{r.level}</span>
              <span className="logname">{r.name}</span>
              <span className="logmsg">{r.msg}</span>
            </div>
          ))}
      </div>
    </div>
  );
}
