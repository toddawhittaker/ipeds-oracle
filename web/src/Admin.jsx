import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api.js";
import Chart from "./Chart.jsx";
import { estimateIntegrate } from "./estimate.js";

function humanBytes(n) {
  if (n == null || !isFinite(n)) return "?";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Math.abs(n);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function humanSeconds(s) {
  if (s == null || !isFinite(s)) return "?";
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const rs = Math.round(s % 60);
  return rs ? `${m}m ${rs}s` : `${m}m`;
}

// Backend _set_status (app/importer.py) only ever emits running/checks/
// swapped/failed — "passed" is not a real job status.
const TERMINAL_JOB_STATUSES = ["failed", "swapped"];

export default function Admin({ me, initialTab, onDataChanged }) {
  const [tab, setTab] = useState(initialTab || "allowlist");
  return (
    <main className="admin thin-scroll">
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
      {tab === "allowlist" && <Allowlist me={me} />}
      {tab === "imports" && <Imports onDataChanged={onDataChanged} />}
      {tab === "usage" && <Usage />}
      {tab === "skills" && <Skills />}
      {tab === "logs" && <Logs />}
    </main>
  );
}

function Allowlist({ me }) {
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

  async function toggleAdmin(r) {
    try {
      await api.setAdmin(r.email, !r.is_admin);
      setFlash(r.is_admin
        ? `${r.email} is no longer an admin.`
        : `${r.email} is now an admin.`);
    } catch (err) {
      let msg = "Could not update admin status.";
      try { msg = JSON.parse(err.message).detail || msg; } catch { /* keep default */ }
      setFlash(msg);
    }
    load();
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
              <td>
                {me && r.email === me.email && r.is_admin ? (
                  <span className="admintoggle on" title="You can't remove your own admin access">
                    ✓ admin (you)
                  </span>
                ) : (
                  <button type="button"
                          className={"link admintoggle" + (r.is_admin ? " on" : "")}
                          aria-pressed={r.is_admin ? "true" : "false"}
                          onClick={() => toggleAdmin(r)}>
                    {r.is_admin ? "✓ admin" : "make admin"}
                  </button>
                )}
              </td>
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

const STATUS_GLYPH = {
  integrated: "✓",
  update: "↑",
  final: "◆",
  provisional: "◑",
  unknown: "?",
};
const STATUS_TEXT = {
  integrated: "Integrated",
  update: "Update",
  final: "Final",
  provisional: "Provisional",
  unknown: "Can't check",
};

function StatusBadge({ status }) {
  return (
    <span className={"badge " + status}>
      <span aria-hidden="true">{STATUS_GLYPH[status] || ""}</span> {STATUS_TEXT[status] || status}
    </span>
  );
}

function YearCard({ entry, locked, checked, onToggle, onRemove }) {
  // The whole card is the toggle (no separate checkbox) — but it still carries
  // full checkbox semantics for keyboard + screen-reader users: role=checkbox,
  // aria-checked, tabbable, and Space/Enter toggle. Non-selectable cards
  // (already-integrated / unknown) are inert static tiles, not controls.
  const interactive = entry.selectable && !locked;
  const label = `Integrate ${entry.year_label} (${entry.release})`;
  const cls = ["year-card", entry.status, checked ? "selected" : "", locked ? "locked" : ""]
    .filter(Boolean).join(" ");

  const toggle = () => { if (interactive) onToggle(!checked); };
  const onKeyDown = (e) => {
    if (interactive && (e.key === " " || e.key === "Enter")) {
      e.preventDefault();  // Space would otherwise scroll the page
      onToggle(!checked);
    }
  };

  // Only an already-integrated (or update — integrated as Provisional, Final
  // now out) year can be removed, and never while a job is running. The
  // trashcan is a real <button>, a SIBLING of the role=checkbox tile (not
  // nested inside it) — a click never toggles selection, so no
  // stopPropagation gymnastics are needed, and screen readers/keyboard users
  // get an unambiguous, independently-focusable control.
  const removable = (entry.status === "integrated" || entry.status === "update") && !locked;

  return (
    <div className="year-card-wrap">
      <div
        className={cls}
        data-year={entry.start_year}
        data-status={entry.status}
        role={interactive ? "checkbox" : undefined}
        aria-checked={interactive ? checked : undefined}
        aria-label={interactive ? label : undefined}
        aria-disabled={!entry.selectable || locked ? "true" : undefined}
        tabIndex={interactive ? 0 : undefined}
        onClick={interactive ? toggle : undefined}
        onKeyDown={interactive ? onKeyDown : undefined}
      >
        <div className="year-card__top">
          <span className="year-label">{entry.year_label}</span>
          {checked && <span className="year-card__check" aria-hidden="true">✓</span>}
        </div>
        <StatusBadge status={entry.status} />
      </div>
      {removable && (
        <button type="button" className="year-remove"
                aria-label={`Remove ${entry.year_label} from the database`}
                title={`Remove ${entry.year_label} from the database`}
                onClick={() => onRemove(entry)}>
          {/* A monochrome inline SVG (not the 🗑 emoji) so `currentColor`
              actually tracks --muted/--danger below — a color emoji glyph
              ignores CSS color entirely, which would make the muted->danger
              hover channel a no-op and leave contrast nondeterministic. */}
          <svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14"
               fill="none" stroke="currentColor" strokeWidth="1.4"
               strokeLinecap="round" strokeLinejoin="round">
            <path d="M3 4.5h10M6.5 4.5V3a1 1 0 0 1 1-1h1a1 1 0 0 1 1 1v1.5" />
            <path d="M4 4.5 4.6 13a1 1 0 0 0 1 .9h4.8a1 1 0 0 0 1-.9l.6-8.5" />
            <path d="M6.7 7v4.5M9.3 7v4.5" />
          </svg>
        </button>
      )}
    </div>
  );
}

function Imports({ onDataChanged }) {
  const [jobs, setJobs] = useState([]);
  const [active, setActive] = useState(null);
  const [activeYears, setActiveYears] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [notice, setNotice] = useState("");
  const fileRef = useRef();
  const poll = useRef();
  const noticeRef = useRef();
  // Read inside `tick` below (a long-lived interval closure), so it must stay
  // fresh across renders rather than closing over a stale `activeYears`.
  const activeYearsRef = useRef(null);

  const [catalog, setCatalog] = useState(null);
  const [catalogError, setCatalogError] = useState(false);
  const [selected, setSelected] = useState(() => new Set());
  const [integrating, setIntegrating] = useState(false);

  useEffect(() => { activeYearsRef.current = activeYears; }, [activeYears]);
  // Move focus to the notice whenever it (re)appears — covers both the
  // success and failure announcements below, and the just-integrated card's
  // checkbox no longer being in the DOM after a catalog refresh.
  useEffect(() => { if (notice) noticeRef.current?.focus(); }, [notice]);

  const loadJobs = () => api.importJobs().then(setJobs);
  const loadCatalog = useCallback((refresh = false) => api.importCatalog(refresh)
    .then((data) => { setCatalog(data); setCatalogError(false); })
    .catch(() => setCatalogError(true)), []);

  useEffect(() => {
    loadJobs();
    loadCatalog();
    return () => clearInterval(poll.current);
  }, [loadCatalog]);

  const jobRunning = active != null && !TERMINAL_JOB_STATUSES.includes(active.status);
  const locked = jobRunning || integrating;

  function watch(id) {
    clearInterval(poll.current);
    const tick = async () => {
      const job = await api.importJob(id);
      setActive(job);
      if (TERMINAL_JOB_STATUSES.includes(job.status)) {
        clearInterval(poll.current);
        loadJobs();
        // Derive wording from the job's own filename (set by the router:
        // "deintegrate:{start_year}" for a removal, "integrate:{years}" or
        // an IPEDS{YYYY}{YY}.accdb name otherwise) so the notice reads right
        // even when reached via "view" on a past job, not just a fresh
        // submit — no separate "action kind" state to keep in sync.
        const isRemoval = (job.filename || "").startsWith("deintegrate:");
        if (job.status === "swapped") {
          setSelected(new Set());
          loadCatalog(true);
          // Either an integrate or a de-integrate can change whether ANY
          // year is loaded at all (e.g. the fresh-deploy first integration,
          // or removing the last-remaining year) -- re-fetch /me so has_data
          // (and any admin no-data routing derived from it) stays current
          // without requiring a full page reload.
          onDataChanged?.();
          // For a removal, the year comes straight from the job's own
          // filename ("deintegrate:{start_year}") — NOT activeYearsRef,
          // which is null whenever this job is reached via "view" on a past
          // job or the 409 watch-someone-else's-job path, and would
          // otherwise fall back to the raw filename ("deintegrate:2024").
          let what;
          if (isRemoval) {
            const sy = parseInt(job.filename.slice("deintegrate:".length), 10);
            what = Number.isFinite(sy) ? `year ${sy}-${String(sy + 1).slice(-2)}` : "the year";
          } else {
            const yrs = activeYearsRef.current;
            what = yrs && yrs.length
              ? `${yrs.length > 1 ? "years" : "year"} ${yrs
                  .map((y) => `${y}-${String(y + 1).slice(-2)}`).join(", ")}`
              : (job.filename || "the file");
          }
          setNotice(isRemoval
            ? `Removal complete — ${what} removed from the live database.`
            : `Integration complete — ${what} added to the live database.`);
        } else if (job.status === "failed") {
          setNotice(isRemoval
            ? "Removal failed — the live database was not changed."
            : "Import failed — the live database was not changed.");
        }
      }
    };
    tick();
    poll.current = setInterval(tick, 2000);
  }

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
    setActiveYears(null);
    if (data.job_id) watch(data.job_id);
    loadJobs();
  }

  function toggleYear(startYear, checked) {
    setSelected((s) => {
      const next = new Set(s);
      if (checked) next.add(startYear); else next.delete(startYear);
      return next;
    });
  }

  const selectableYears = (catalog?.years || []).filter((y) => y.selectable);
  const yearsNewestFirst = catalog ? catalog.years.slice().reverse() : [];
  // Fresh-deploy "no data" state: nothing is integrated yet. An additive
  // banner above the normal catalog UI, not a replacement for it.
  const noData = catalog != null && !catalog.years.some((y) => y.integrated);

  // Structured per-year progress from the polled job row (a JSON string per
  // the API contract — mirrors sql_log on chat messages).
  const progress = useMemo(() => {
    if (!active?.progress) return null;
    try { return JSON.parse(active.progress); } catch { return null; }
  }, [active]);
  const progressYears = useMemo(() => {
    if (!progress?.years) return [];
    return Object.values(progress.years).sort((a, b) => a.start_year - b.start_year);
  }, [progress]);

  // Client-side disk/time estimate over the FULL rebuild union — every
  // already-integrated start year (derived from the catalog's
  // years[].integrated) UNION the newly-checked start years — mirroring
  // exactly what run_integrate (app/importer.py) re-downloads: a full rebuild
  // of the union, never an incremental merge. A year that's both
  // already-integrated AND checked (a status:"update" re-integration) counts
  // once in the union, not twice, so it can't inflate the staging-db term.
  // This is still a UX preview, not the server's authoritative preflight
  // check — see app/estimate.py / web/src/estimate.js for the shared formula.
  const diskEstimate = useMemo(() => {
    if (!catalog?.disk || !catalog?.calibration) return null;
    const calib = catalog.calibration;
    const byYear = new Map(catalog.years.map((y) => [y.start_year, y]));
    const alreadyIntegratedStarts = catalog.years
      .filter((y) => y.integrated)
      .map((y) => y.start_year);
    const unionStarts = Array.from(new Set([...alreadyIntegratedStarts, ...selected]))
      .sort((a, b) => a - b);
    const alreadyIntegratedCount = alreadyIntegratedStarts.length;
    const selectedCount = unionStarts.length - alreadyIntegratedCount;
    return estimateIntegrate({
      zipBytes: unionStarts.map((sy) => byYear.get(sy)?.zip_bytes ?? null),
      alreadyIntegratedCount,
      selectedCount,
      liveDbBytes: calib.live_db_bytes,
      currentIntegratedYearCount: alreadyIntegratedCount,
      diskFreeBytes: catalog.disk.free_bytes,
      diskTotalBytes: catalog.disk.total_bytes,
      expandFactor: calib.expand_factor,
      defaultPerYearDbMb: calib.default_per_year_db_mb,
      bandwidthMbps: calib.bandwidth_mbps,
      buildSecondsPerYear: calib.build_seconds_per_year,
      safetyFactor: calib.safety_factor,
    });
  }, [catalog, selected]);
  const diskOver = diskEstimate != null && !diskEstimate.sufficient;

  async function submitIntegrate() {
    setNotice("");
    setIntegrating(true);
    const years = Array.from(selected);
    try {
      const body = await api.integrateYears(years);
      setActiveYears(years.slice().sort((a, b) => a - b));
      watch(body.job_id);
    } catch (err) {
      let msg = "Could not start the import.";
      try { msg = JSON.parse(err.message).detail || msg; } catch { /* keep default */ }
      setNotice(msg);
      if (/already running/i.test(msg)) {
        // Someone else's import is mid-flight — find it and watch its progress.
        const list = await api.importJobs().catch(() => []);
        const runningJob = list.find((j) => !TERMINAL_JOB_STATUSES.includes(j.status));
        if (runningJob) watch(runningJob.id);
      }
    } finally {
      setIntegrating(false);
    }
  }

  async function removeYear(entry) {
    if (!window.confirm(
      `Remove ${entry.year_label} from the live database? This rebuilds the database ` +
      "without that year and can't be undone.")) return;
    // Set an interim notice BEFORE watch() flips `locked` (which unmounts the
    // very trashcan button the user just activated) — otherwise notice stays
    // empty right when focus needs somewhere to land, and the
    // focus-to-notice effect above never fires, dropping focus to <body>.
    setNotice(`Removing ${entry.year_label}…`);
    try {
      const body = await api.deintegrateYear(entry.start_year);
      setActiveYears(null);
      watch(body.job_id);
    } catch (err) {
      let msg = "Could not start the removal.";
      try { msg = JSON.parse(err.message).detail || msg; } catch { /* keep default */ }
      setNotice(msg);
      if (/already running/i.test(msg)) {
        // Someone else's import/removal is mid-flight — find it and watch it.
        const list = await api.importJobs().catch(() => []);
        const runningJob = list.find((j) => !TERMINAL_JOB_STATUSES.includes(j.status));
        if (runningJob) watch(runningJob.id);
      }
    }
  }

  return (
    <div className="panel">
      <h2>Load IPEDS years</h2>
      <p className="muted small">
        Select one or more years to fetch straight from NCES — each run rebuilds a
        staging database from the union of every already-integrated year plus the
        ones you pick, runs integrity + magnitude checks, and only swaps in if
        everything passes. The live database is never touched until then.
      </p>

      {noData && (
        <div className="notice notice-cta" role="note">
          No dataset loaded yet — pick one or more years below and choose
          &quot;Integrate selected&quot; to get started. The first load fetches
          from NCES and builds the database (this can take a few minutes).
        </div>
      )}

      {notice && (
        <div ref={noticeRef} tabIndex={-1} className="notice" role="status">{notice}</div>
      )}
      {jobRunning && (
        <div className="notice">
          An import is running… controls are locked until it finishes.
        </div>
      )}

      <div className="year-catalog">
        <div className="catalog-legend">
          <span className="legend-item"><StatusBadge status="integrated" /></span>
          <span className="legend-item"><StatusBadge status="update" /></span>
          <span className="legend-item"><StatusBadge status="final" /></span>
          <span className="legend-item"><StatusBadge status="provisional" /></span>
          <span className="legend-item"><StatusBadge status="unknown" /></span>
        </div>

        {!catalog && !catalogError && (
          <>
            <p className="muted small">Checking NCES for available years…</p>
            <div className="year-grid">
              {Array.from({ length: 8 }).map((_, i) => (
                <div key={i} className="year-card skeleton" aria-hidden="true" />
              ))}
            </div>
          </>
        )}

        {catalogError && (
          <div className="notice" role="alert">
            Could not reach NCES to check available years.{" "}
            <button type="button" className="link" onClick={() => loadCatalog(true)}>Retry</button>
          </div>
        )}

        {catalog && (
          <>
            {catalog.partial && (
              <div className="notice" role="status">
                Some years could not be checked.{" "}
                <button type="button" className="link" onClick={() => loadCatalog(true)}>Retry</button>
              </div>
            )}

            <div className="catalog-toolbar">
              <button type="button" disabled={locked || selectableYears.length === 0}
                      onClick={() => setSelected(new Set(selectableYears.map((y) => y.start_year)))}>
                Select all available ({selectableYears.length})
              </button>
              <button type="button" disabled={locked || selected.size === 0}
                      onClick={() => setSelected(new Set())}>
                Clear selection
              </button>
              <span className="muted small">{selected.size} selected</span>
              <button type="button" disabled={locked} onClick={() => loadCatalog(true)}>
                ⟳ Refresh
              </button>
            </div>

            <div className="year-grid">
              {yearsNewestFirst.map((y) => (
                <YearCard key={y.start_year} entry={y} locked={locked}
                          checked={selected.has(y.start_year)}
                          onToggle={(checked) => toggleYear(y.start_year, checked)}
                          onRemove={removeYear} />
              ))}
            </div>

            {diskEstimate && (
              <div className="disk-estimate">
                <div data-testid="disk-meter" aria-hidden="true"
                     className={"disk-meter" + (diskOver ? " over" : "")}>
                  <div className="disk-meter-fill"
                       style={{ width: `${Math.min(100, (diskEstimate.peakUsedBytes / catalog.disk.total_bytes) * 100)}%` }} />
                </div>
                <p id="disk-summary" className="muted small" role="status" aria-live="polite">
                  Estimated peak disk use: {humanBytes(diskEstimate.peakUsedBytes)} of{" "}
                  {humanBytes(catalog.disk.total_bytes)} total
                  ({humanBytes(catalog.disk.free_bytes)} free now)
                  {diskOver
                    ? " — not enough free space for this selection."
                    : " — enough free space."}
                  {selected.size > 0 && (
                    <> ~{humanBytes(diskEstimate.totalDownloadBytes)} to download
                    (~{humanSeconds(diskEstimate.estDownloadSeconds)}),
                    rebuild ~{humanSeconds(diskEstimate.estBuildSeconds)}.</>
                  )}
                </p>
              </div>
            )}

            <div className="integrate-bar">
              <button type="button" disabled={locked || selected.size === 0 || diskOver}
                      aria-describedby={diskOver ? "disk-summary" : undefined}
                      onClick={submitIntegrate}>
                Integrate selected ({selected.size})
              </button>
            </div>
          </>
        )}
      </div>

      <details className="manual-import">
        <summary>Manual upload (.accdb fallback)</summary>
        <p className="muted small">
          Upload a year&apos;s <code>IPEDS{"{YYYY}{YY}"}.accdb</code> directly — the same
          rebuild-and-check pipeline runs on just that file.
        </p>
        <form className="row" onSubmit={upload}>
          <label htmlFor="import-file" className="sr-only">IPEDS Access database file</label>
          <input id="import-file" ref={fileRef} type="file" accept=".accdb" required disabled={locked} />
          <button type="submit" disabled={uploading || locked}>{uploading ? "Uploading…" : "Import"}</button>
        </form>
      </details>

      {active && (
        <div className="job">
          <div role="status" aria-live="polite">
            <div className={"badge " + active.status}>{active.status}</div>
            {activeYears && (
              <span className="muted small">
                &nbsp;integrating start year{activeYears.length > 1 ? "s" : ""}: {activeYears.join(", ")}
              </span>
            )}
            {progress?.overall && (
              <span className="muted small">&nbsp;— {progress.overall.message}</span>
            )}
          </div>
          {progressYears.length > 0 && (
            <div data-testid="import-progress" className="file-progress">
              {progressYears.map((y) => {
                // A fetched year is done (fill full); a failed one shows a full
                // red bar; downloading tracks the live pct; queued sits at 0.
                const width = y.step === "fetched" || y.step === "failed"
                  ? 100 : Math.min(100, Math.max(0, y.pct || 0));
                return (
                  <div key={y.start_year} data-year={y.start_year} className="file-progress-row">
                    <span className="file-progress-year">{y.year_label}</span>
                    <span className="file-progress-step">{y.step}</span>
                    <div className="file-progress-bar" role="progressbar"
                         aria-label={`${y.year_label} download`}
                         aria-valuemin={0} aria-valuemax={100}
                         aria-valuenow={y.step === "fetched" ? 100 : (y.pct || 0)}>
                      <div className="file-progress-fill" data-step={y.step}
                           style={{ width: `${width}%` }} />
                    </div>
                    <span className="file-progress-pct">
                      {y.step === "failed" ? "—" : `${y.step === "fetched" ? 100 : (y.pct || 0)}%`}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
          {progress?.rebuild?.tables_total && !TERMINAL_JOB_STATUSES.includes(active.status) ? (
            <div data-testid="rebuild-progress" className="rebuild-progress">
              <div className="rebuild-progress-label muted small">
                Rebuilding database — {progress.rebuild.tables_done} / {progress.rebuild.tables_total} tables
              </div>
              <div className="file-progress-bar" role="progressbar"
                   aria-label="Rebuild progress"
                   aria-valuemin={0} aria-valuemax={100}
                   aria-valuenow={Math.min(100, Math.max(0, progress.rebuild.pct || 0))}
                   aria-valuetext={`${progress.rebuild.tables_done} of ${progress.rebuild.tables_total} tables`}>
                <div className="file-progress-fill"
                     style={{ width: `${Math.min(100, Math.max(0, progress.rebuild.pct || 0))}%` }} />
              </div>
            </div>
          ) : null}
          {active.report && <pre className="report">{active.report}</pre>}
          <details open>
            <summary>Log</summary>
            <pre className="log thin-scroll">{active.log || "…"}</pre>
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
              <td><button className="link" onClick={() => { setActiveYears(null); watch(jb.id); }}>view</button></td>
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

function ruleName(s) {
  return s.headline || s.lesson || s.notes || s.question || "untitled lesson";
}

function Skills() {
  const [rows, setRows] = useState([]);
  const [status, setStatus] = useState("");
  const [editingId, setEditingId] = useState(null);   // at most one card at a time
  const [draft, setDraft] = useState({ headline: "", lesson: "", canonical_sql: "" });
  // Focus returns to the "edit" button when the editor closes (a11y). We can't
  // hold the clicked node like Chat.jsx does: opening the editor unmounts that
  // button, so the captured node is detached and focusing it silently no-ops.
  // Keep a per-lesson ref map instead and re-find the freshly mounted button.
  const editBtnRefs = useRef({});   // skill id -> its "edit" button node
  const headlineRef = useRef(null);
  const load = () => api.skills().then(setRows);
  useEffect(() => { load(); }, []);
  const pending = rows.filter((r) => !r.verified).length;

  // aria-live only fires when the DOM text actually changes, so re-announcing
  // the same message (editing two lessons in a row) would say nothing. Clear
  // first, then set on the next frame, to force a real mutation.
  function announce(text) {
    setStatus("");
    requestAnimationFrame(() => setStatus(text));
  }

  const setVerified = (s, verified) =>
    api.patchSkill(s.id, { verified }).then(() => {
      announce(verified ? "Lesson verified." : "Lesson moved back to unverified.");
      load();
    });

  function startEdit(s) {
    setEditingId(s.id);
    setDraft({
      headline: s.headline || "",
      lesson: s.lesson || s.notes || "",
      canonical_sql: s.canonical_sql || "",
    });
    requestAnimationFrame(() => headlineRef.current?.focus?.());
  }
  function closeEdit(id) {
    setEditingId(null);
    // rAF runs after React has committed the re-render, so the ref map now
    // holds the newly mounted button rather than the one we just tore down.
    requestAnimationFrame(() => editBtnRefs.current[id]?.focus?.());
  }
  // A lesson with neither headline nor description has nothing to embed against,
  // so retrieval could never surface it — block the save rather than store a
  // rule that's dead on arrival.
  const draftIsEmpty = !draft.headline.trim() && !draft.lesson.trim();
  function saveEdit(s) {
    // Reachable now that Save is aria-disabled rather than disabled: land the
    // user on the field that unblocks them instead of doing nothing.
    if (draftIsEmpty) { headlineRef.current?.focus?.(); return; }
    const description = draft.lesson.trim();
    api.patchSkill(s.id, {
      headline: draft.headline.trim(),
      // lesson and notes are written together, the way migration 6 does it
      // (app/db.py). Every reader resolves the description as
      // `lesson or notes`, so writing lesson alone would let a stale notes
      // resurrect text the admin just deleted — back into the card AND into
      // the model's prompt, while the embedding no longer matches it.
      lesson: description,
      notes: description,
      canonical_sql: draft.canonical_sql.trim(),
    }).then(() => {
      announce("Lesson updated.");
      closeEdit(s.id);
      load();
    }).catch(() => announce("Couldn't save that lesson — nothing was changed."));
  }
  const reject = (s) => {
    // Confirm only when there's curated/used data to lose — a fresh unreviewed
    // proposal (verified=false, no votes/hits) can be dismissed without nagging.
    const risky = s.verified || s.upvotes > 0 || s.hits > 0;
    if (risky && !window.confirm(
      `Delete this ${s.verified ? "verified " : ""}lesson? This can't be undone.`)) return;
    api.deleteSkill(s.id).then(() => { announce("Lesson rejected."); load(); });
  };

  return (
    <div className="panel">
      <h2>Learned lessons ({rows.length})</h2>
      <p className="muted small">
        Rules the assistant applies as guidance. The post-answer critic proposes a
        lesson when it catches a mistake — a short headline plus a longer
        description — and it starts <strong>unverified</strong> until you approve
        it here.
        {pending > 0 && ` ${pending} awaiting review.`}
      </p>
      <div className="sr-only" role="status" aria-live="polite">{status}</div>
      {rows.length === 0 && (
        <p className="muted small">
          No lessons yet — they’ll appear here as the critic proposes them.
        </p>
      )}
      {rows.map((s) => {
        const description = s.lesson || s.notes || "";
        const headline = s.headline || description;
        const showDescription = description && description !== headline;
        return (
        <div key={s.id} className="skill">
          <div className="skill-head">
            <span className="lesson-rule">
              {headline || <em className="muted">(no rule text)</em>}
            </span>
            <span className="tags">
              {s.verified
                ? <span className="tag ok">verified</span>
                : <span className="tag warn">unverified</span>}
              <span className="tag">from {s.created_by || "?"}</span>
              <span className="tag" aria-label={`${s.upvotes} upvotes, ${s.downvotes} downvotes`}>
                <span aria-hidden="true">▲{s.upvotes} ▼{s.downvotes}</span>
              </span>
              <span className="tag">hits {s.hits}</span>
            </span>
          </div>
          {editingId === s.id ? (
            <div className="lesson-edit" role="group"
                 aria-label={`Edit lesson: ${ruleName(s)}`}
                 onKeyDown={(e) => { if (e.key === "Escape") closeEdit(s.id); }}>
              <label className="lesson-field">
                <span className="muted small">Headline</span>
                <input ref={headlineRef} type="text" maxLength={300} value={draft.headline}
                       onChange={(e) => setDraft({ ...draft, headline: e.target.value })} />
              </label>
              <label className="lesson-field">
                <span className="muted small">Description</span>
                <textarea rows={4} maxLength={4000} value={draft.lesson}
                          onChange={(e) => setDraft({ ...draft, lesson: e.target.value })} />
              </label>
              <label className="lesson-field">
                <span className="muted small">Example query</span>
                <textarea rows={6} maxLength={8000} className="mono" value={draft.canonical_sql}
                          onChange={(e) => setDraft({ ...draft, canonical_sql: e.target.value })} />
              </label>
              <div className="msg-actions">
                {/* aria-disabled rather than disabled: a disabled button is
                    unfocusable, so a screen-reader user who empties both
                    fields just finds Save gone with no explanation, and any
                    aria-describedby on it would never be read. saveEdit
                    early-returns, so the click is a safe no-op. */}
                <button className="btn-verify" aria-disabled={draftIsEmpty}
                        aria-describedby={draftIsEmpty ? `save-hint-${s.id}` : undefined}
                        onClick={() => saveEdit(s)}>Save</button>
                <button className="link" onClick={() => closeEdit(s.id)}>Cancel</button>
                {draftIsEmpty && (
                  <span id={`save-hint-${s.id}`} className="muted small">
                    Give it a headline or description to save.
                  </span>
                )}
              </div>
            </div>
          ) : (
          <>
          {showDescription && (
            <details className="lesson-desc">
              <summary className="muted small">Details</summary>
              <p>{description}</p>
            </details>
          )}
          {s.canonical_sql && (
            <details className="lesson-example">
              <summary className="muted small">Example query</summary>
              {s.question && <div className="muted small qtext">{s.question}</div>}
              <pre>{s.canonical_sql}</pre>
            </details>
          )}
          <div className="msg-actions">
            {s.verified ? (
              <button className="link" aria-label={`Unverify lesson: ${ruleName(s)}`}
                      onClick={() => setVerified(s, false)}>unverify</button>
            ) : (
              <button className="btn-verify" aria-label={`Verify lesson: ${ruleName(s)}`}
                      onClick={() => setVerified(s, true)}>Verify</button>
            )}
            <button className="link" aria-label={`Edit lesson: ${ruleName(s)}`}
                    ref={(el) => {
                      if (el) editBtnRefs.current[s.id] = el;
                      else delete editBtnRefs.current[s.id];
                    }}
                    onClick={() => startEdit(s)}>edit</button>
            <button className="link danger" aria-label={`Reject lesson: ${ruleName(s)}`}
                    onClick={() => reject(s)}>reject</button>
          </div>
          </>
          )}
        </div>
        );
      })}
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
