// Admin → Logs: the persistent server log viewer (logs.db) + seen-marking.
// Split out of Admin.jsx unchanged.
import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { loadErrorMessage } from "../authcopy.js";
import { loadNotice } from "../loadstate.js";

export default function Logs({ onAttentionChanged }) {
  const [records, setRecords] = useState([]);
  const [err, setErr] = useState("");
  const [level, setLevel] = useState("");
  const [q, setQ] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [auto, setAuto] = useState(true);

  // Acknowledge the log problems: viewing the tab advances this admin's "logs
  // seen" marker, so the attention badge clears. Mark on mount (then refresh the
  // badge immediately) AND on unmount, so problems that arrive while the admin
  // is watching are also marked read when they leave. Fire-and-forget — a failed
  // mark just leaves the badge up, which is safe.
  const refreshAttention = onAttentionChanged || (() => {});
  useEffect(() => {
    api.markLogsSeen().then(refreshAttention).catch(() => {});
    return () => { api.markLogsSeen().catch(() => {}); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const load = useCallback(() => {
    const since = from ? Math.floor(new Date(`${from}T00:00:00`).getTime() / 1000) : null;
    const until = to ? Math.floor(new Date(`${to}T23:59:59.999`).getTime() / 1000) : null;
    api.logs(500, level, q.trim(), since, until)
      .then((d) => { setRecords(d.records || []); setErr(""); })
      // The worst of the three: a swallowed failure rendered "No log
      // records." to an admin whose entire job on this screen is to find out
      // whether something is wrong -- so they'd conclude the server was fine
      // and stop looking. Clearing on success matters too: the 4s refresh
      // must not pin a stale error.
      //
      // A repeated failing poll calls setErr with the SAME string content
      // every time (loadErrorMessage is a pure function of the same detail),
      // so React's setState bail-out kicks in: verified directly (a throwaway
      // jsdom/createRoot harness) that an identical-content setState still
      // re-invokes the component function but does NOT re-commit its DOM or
      // re-fire effects -- so the role="alert" text node never actually
      // changes on screen, and the alert does not re-announce every 4s. Only
      // a genuinely NEW error string (or clearing back to "") produces a real
      // announcement.
      .catch((e) => setErr(loadErrorMessage("the logs", e?.detail)));
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
  // The load-failure notice (see loadstate.js): a FIRST load failure replaces
  // the panel (no rows to protect), a REFRESH failure on an already-populated
  // screen keeps the rows and adds a stale-data notice instead. This used to
  // be gated behind `records.length === 0`, so the 4s auto-refresh poll
  // failing after a successful first load rendered NOTHING -- the stale rows
  // kept displaying as current and an admin watching a live problem had no
  // way to tell the server had stopped answering.
  const notice = loadNotice({ error: err, hasRows: records.length > 0 });
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
        <label className="switch">
          <input type="checkbox" role="switch" checked={auto}
            onChange={(e) => setAuto(e.target.checked)} />
          Auto-refresh
        </label>
        <button onClick={load}>Refresh</button>
        {filtered && <button onClick={clearFilters}>Clear filters</button>}
      </div>
      {/* Focusable + named: the log viewer caps at 60vh and its rows hold no
          focusable children, so without this a keyboard-only admin can read
          only the first screenful of the server log (WCAG 2.1.1, Level A). */}
      <div className="log logbox thin-scroll" tabIndex={0} role="region"
           aria-label="Server log">
        {/* Rendered unconditionally now, not gated behind records.length===0 --
            a poll failure on a populated screen still needs to say so. */}
        {notice && (
          <p className={notice.replace ? "denied-error" : "notice warn small"} role="alert">
            {notice.text}
          </p>
        )}
        {/* The empty state is now `!err && records.length === 0`: a first-load
            failure (notice.replace) already said everything via the notice
            above and shows no rows/empty-message underneath it. */}
        {!notice?.replace && (
          records.length === 0
            ? <div className="muted">{filtered ? "No matching log records." : "No log records."}</div>
            : records.map((r, i) => (
              <div key={i} className={"logline lvl-" + r.level}>
                <span className="logts">{fmt(r.ts)}</span>
                <span className="loglvl">{r.level}</span>
                <span className="logname">{r.name}</span>
                <span className="logmsg">{r.msg}</span>
              </div>
            ))
        )}
      </div>
    </div>
  );
}

