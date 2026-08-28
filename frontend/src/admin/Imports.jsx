// Admin → Imports: the NCES year catalog, dataset rebuild/de-integrate jobs, and
// the manual .accdb upload. Split out of Admin.jsx unchanged.
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { estimateIntegrate } from "../estimate.js";
import { useConfirm } from "../ConfirmModal.jsx";
import TableScroll from "../TableScroll.jsx";
import { humanBytes, humanSeconds } from "./format.js";

// Backend _set_status (app/importer.py) only ever emits running/checks/
// swapped/failed — "passed" is not a real job status.
const TERMINAL_JOB_STATUSES = ["failed", "swapped"];


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
  // `interactive` gates only the HANDLERS. It used to gate role, aria-checked,
  // aria-label and tabIndex too, which left a locked or non-selectable card as a
  // roleless <div> carrying aria-disabled — an attribute ARIA does not permit on
  // a generic element, so it is simply ignored. The whole year grid degraded to
  // unannounced static text with no disabled semantics, where a native disabled
  // button would at least announce "button, dimmed".
  //
  // Newly reachable because Imports now ADOPTS a running job on mount: a second
  // admin, or the same one after a reload, lands in exactly this state. An
  // aria-disabled control staying focusable is the point — it is how the state
  // becomes discoverable at all.
  const interactive = entry.selectable && !locked;
  const disabled = !entry.selectable || locked;
  // role="checkbox" is in ARIA's presentational-children list, so the tile's
  // own .year-label and StatusBadge are PRUNED and this label is the entire
  // accessible name. Making the role unconditional therefore made the name
  // wrong for cards that are not selectable: an already-loaded year announced
  // "Integrate 2022-23 (Final)" with its "Integrated" badge gone — an
  // affordance that does not exist — and `release` is null for an unprobed or
  // unknown year, which rendered the literal string "(null)".
  // Three cases, not two. `update` is BOTH integrated and selectable (loaded as
  // Provisional, a Final is now out), so it fell through to the "Integrate …"
  // branch and rendered byte-identically to a never-loaded year — _derive_status
  // only returns `update` when release == "Final". With role="checkbox" pruning
  // the "↑ Update" badge, a screen-reader admin had no way to tell a re-fetch of
  // data they already have from a genuinely new year. Same defect the
  // non-selectable branch below was added to fix, surviving in the one state
  // that is both.
  const label = entry.status === "update"
    ? `Update ${entry.year_label} to ${entry.release || "the latest release"}`
    : entry.selectable
      ? `Integrate ${entry.year_label}${entry.release ? ` (${entry.release})` : ""}`
      : `${entry.year_label} — ${STATUS_TEXT[entry.status] || entry.status}`;
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
        role="checkbox"
        aria-checked={checked}
        aria-label={label}
        aria-disabled={disabled ? "true" : undefined}
        tabIndex={0}
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

export default function Imports({ onDataChanged }) {
  const [jobs, setJobs] = useState([]);
  const [active, setActive] = useState(null);
  const [activeYears, setActiveYears] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [dropFiles, setDropFiles] = useState([]);
  const [dragging, setDragging] = useState(false);
  const [uploadMsg, setUploadMsg] = useState("");
  const [notice, setNotice] = useState("");
  const [noticeKind, setNoticeKind] = useState(""); // "" | "ok" | "error"
  // Set the job-result notice AND its semantic color together, so a failed
  // import/removal reads red and a completed one reads green instead of both
  // being the same neutral box.
  const notify = (text, kind = "") => { setNotice(text); setNoticeKind(kind); };
  const confirm = useConfirm();
  const fileRef = useRef();
  const dragDepth = useRef(0);
  const poll = useRef();
  const noticeRef = useRef();
  const lockedRef = useRef();
  // Read inside `tick` below (a long-lived interval closure), so it must stay
  // fresh across renders rather than closing over a stale `activeYears`.
  const activeYearsRef = useRef(null);

  const [catalog, setCatalog] = useState(null);
  const [catalogError, setCatalogError] = useState(false);
  const [selected, setSelected] = useState(() => new Set());
  const [integrating, setIntegrating] = useState(false);
  const [adopted, setAdopted] = useState(false);

  useEffect(() => { activeYearsRef.current = activeYears; }, [activeYears]);
  // Move focus to the notice whenever it (re)appears — covers both the
  // success and failure announcements below, and the just-integrated card's
  // checkbox no longer being in the DOM after a catalog refresh.
  useEffect(() => { if (notice) noticeRef.current?.focus(); }, [notice]);
  const wasLocked = useRef(false);

  // Returns the jobs as well as storing them, so the mount effect can adopt a
  // job that is already running (see below).
  const loadJobs = () => api.importJobs().then((j) => { setJobs(j); return j; });
  const loadCatalog = useCallback((refresh = false) => api.importCatalog(refresh)
    .then((data) => { setCatalog(data); setCatalogError(false); })
    .catch(() => setCatalogError(true)), []);

  // ADOPT a job that is already running when this tab mounts.
  //
  // `locked` derives from `active`, and `active` was only ever set by watch(),
  // which only ran for a job THIS browser session started or clicked "view" on.
  // So an admin who reloaded the tab -- or a second admin, or the same admin on
  // another machine -- got the ordinary catalog: year cards selectable,
  // "Integrate selected" enabled, manual upload enabled, trashcans live, with a
  // full rebuild and atomic swap of the live database in progress. The only
  // trace was a row reading `running` at the bottom of a long page.
  //
  // The 409 hand-off means nothing corrupts, but recovering from a
  // wrong-looking-but-blocked click is not the same as never showing the wrong
  // state. Adopting reuses the whole existing apparatus -- the locked notice,
  // per-year progress, and the terminal toast all light up for a job this
  // session did not start (watch() already handles that; see its isRemoval
  // note).
  useEffect(() => {
    loadJobs()
      .then((list) => {
        const running = (list || []).find(
          (j) => !TERMINAL_JOB_STATUSES.includes(j.status));
        if (running) watch(running.id, { adopted: true });
      })
      .catch(() => { /* the jobs table shows its own empty state */ });
    loadCatalog();
    return () => clearInterval(poll.current);
    // `watch` and `loadJobs` are recreated every render, so listing them here
    // would re-run this MOUNT effect on every render — re-adopting the job and
    // restarting its poll interval repeatedly. This must run exactly once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadCatalog]);

  const jobRunning = active != null && !TERMINAL_JOB_STATUSES.includes(active.status);
  const locked = jobRunning || integrating;

  // The case the notice effect above CANNOT cover: on the happy path
  // submitIntegrate calls notify("") — an EMPTY notice — so that effect never
  // fires, while setIntegrating(true) disables the button the admin just
  // pressed. Focus would land on <body>, mid-import, with nothing explaining
  // why the control vanished. Move it to the locked notice, which is exactly
  // the element that answers that. Edge-triggered, so a re-render during a long
  // import doesn't keep yanking focus back.
  useEffect(() => {
    const busy = locked || uploading;
    if (busy && !wasLocked.current) lockedRef.current?.focus();
    wasLocked.current = busy;
  }, [locked, uploading]);

  // `adopted` marks a job this session did not start, so the locked notice can
  // say so — "controls are locked until it finishes" is confusing when you did
  // not do anything. Set here rather than at each call site so the flag cannot
  // drift out of sync with what is actually being watched.
  function watch(id, { adopted: isAdopted = false } = {}) {
    setAdopted(isAdopted);
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
          notify(isRemoval
            ? `Removal complete — ${what} removed from the live database.`
            : `Integration complete — ${what} added to the live database.`, "ok");
        } else if (job.status === "failed") {
          notify(isRemoval
            ? "Removal failed — the live database was not changed."
            : "Import failed — the live database was not changed.", "error");
        }
      }
    };
    tick();
    poll.current = setInterval(tick, 2000);
  }

  function addFiles(fileList) {
    const all = Array.from(fileList || []);
    const accdb = all.filter((f) => f.name.toLowerCase().endsWith(".accdb"));
    const ignored = all.length - accdb.length;
    if (accdb.length) {
      setDropFiles(accdb);
      // A partial selection must announce what was dropped, not silently keep
      // only the .accdb (role="alert" carries it to a screen reader).
      setUploadMsg(ignored
        ? `${ignored} non-.accdb file${ignored > 1 ? "s were" : " was"} ignored.`
        : "");
    } else if (all.length) {
      setUploadMsg("Only .accdb files are accepted.");
    } else {
      setUploadMsg("");
    }
  }
  // Drag state via a depth counter so crossing child boundaries doesn't flicker
  // it; the handlers no-op while an import is running (locked).
  function onDragEnter(e) {
    if (locked) return;
    e.preventDefault();
    dragDepth.current += 1;
    setDragging(true);
  }
  function onDragOver(e) { if (!locked) e.preventDefault(); }
  function onDragLeave(e) {
    e.preventDefault();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragging(false);
  }
  function onDrop(e) {
    if (locked) return;
    e.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    addFiles(e.dataTransfer.files);
  }

  async function upload(e) {
    e.preventDefault();
    if (!dropFiles.length) return;
    setUploading(true);
    setUploadMsg("");
    const fd = new FormData();
    for (const f of dropFiles) fd.append("files", f);
    let data;
    try {
      const r = await fetch("/api/admin/import", { method: "POST", body: fd });
      data = await r.json().catch(() => ({}));
      if (!r.ok) { setUploadMsg(data.detail || `Upload failed (${r.status}).`); return; }
    } catch {
      setUploadMsg("Upload failed — could not reach the server.");
      return;
    } finally {
      setUploading(false);
    }
    setDropFiles([]);
    if (fileRef.current) fileRef.current.value = "";
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
  // check — see app/estimate.py / frontend/src/estimate.js for the shared formula.
  // The union counts, derived ONCE and shared by the disk estimate and the
  // confirmation. A year with status "update" is BOTH `integrated` and
  // `selectable` (re-integrating picks up a Final release over a Provisional
  // one), so ticking one is a REPLACEMENT, not an addition. Counting it as
  // `already + selected.size` double-counts: six years loaded, tick the one
  // that went final, and the modal claimed "all 7 years (6 already loaded + 1
  // new)" for a deployment that has six and will still have six. The disk
  // estimate got this right via a set union; the confirmation re-derived the
  // same fact and disagreed with it, which is why they are now one computation.
  const yearCounts = useMemo(() => {
    const integratedStarts = (catalog?.years || [])
      .filter((y) => y.integrated).map((y) => y.start_year);
    const unionStarts = Array.from(new Set([...integratedStarts, ...selected]))
      .sort((a, b) => a - b);
    const already = integratedStarts.length;
    return { already, adding: unionStarts.length - already,
             total: unionStarts.length, unionStarts };
  }, [catalog, selected]);

  const diskEstimate = useMemo(() => {
    if (!catalog?.disk || !catalog?.calibration) return null;
    const calib = catalog.calibration;
    const byYear = new Map(catalog.years.map((y) => [y.start_year, y]));
    return estimateIntegrate({
      zipBytes: yearCounts.unionStarts.map((sy) => byYear.get(sy)?.zip_bytes ?? null),
      alreadyIntegratedCount: yearCounts.already,
      selectedCount: yearCounts.adding,
      liveDbBytes: calib.live_db_bytes,
      currentIntegratedYearCount: yearCounts.already,
      diskFreeBytes: catalog.disk.free_bytes,
      diskTotalBytes: catalog.disk.total_bytes,
      expandFactor: calib.expand_factor,
      defaultPerYearDbMb: calib.default_per_year_db_mb,
      bandwidthMbps: calib.bandwidth_mbps,
      buildSecondsPerYear: calib.build_seconds_per_year,
      safetyFactor: calib.safety_factor,
    });
  }, [catalog, yearCounts]);
  const diskOver = diskEstimate != null && !diskEstimate.sufficient;

  // Adding years is the SAME operation as removing one, with different inputs --
  // a full rebuild from the union, ending in an atomic swap of the live
  // database. Removing had a danger modal and adding fired on a single click,
  // which teaches an admin that the guarded one is the dangerous one. It is
  // "warning", not "danger": this is additive, and the live database keeps
  // answering questions until every check passes.
  //
  // Every number in the body is already on screen (diskEstimate / catalog), so
  // this is copy assembly, not new computation. `diskOver` still disables the
  // trigger, so the modal is never the thing standing between an admin and a
  // rebuild that cannot fit.
  function submitIntegrate() {
    const years = Array.from(selected);
    const { already, adding, total } = yearCounts;
    let cost = "";
    if (diskEstimate) {
      const secs = (diskEstimate.estDownloadSeconds || 0) + (diskEstimate.estBuildSeconds || 0);
      cost = ` It downloads about ${humanBytes(diskEstimate.totalDownloadBytes)}`
        + ` and takes roughly ${humanSeconds(secs)}.`;
    }
    confirm({
      variant: "warning",
      title: adding === 0
        ? "Rebuild the database?"
        : `Rebuild the database with ${adding} more year${adding === 1 ? "" : "s"}?`,
      body: `This rebuilds from all ${total} year${total === 1 ? "" : "s"}`
        + ` (${already} already loaded`
        + `${adding ? `, ${adding} new` : `, re-fetching ${total === 1 ? "a year" : "years"} you already have`}`
        + `).${cost} The live database is only replaced if every check`
        + " passes — until then it keeps answering questions.",
      confirmLabel: "Start rebuild",
      // Mirrors removeYear: a genuine failure RETHROWS so the modal stays open
      // showing the error, instead of dismissing exactly like a success and
      // leaving a notice further down the page as the only trace. The 409
      // hand-off is not a failure — it resolves an outcome and closes.
      onConfirm: () => runIntegrate(years),
      errorToast: "Could not start the import.",
    });
  }

  async function runIntegrate(years) {
    notify("");
    setIntegrating(true);
    try {
      const body = await api.integrateYears(years);
      setActiveYears(years.slice().sort((a, b) => a - b));
      watch(body.job_id);
    } catch (err) {
      let msg = "Could not start the import.";
      msg = err?.detail || msg;   // ApiError carries the server's own wording
      if (/already running/i.test(msg)) {
        // Someone else's import is mid-flight — hand off to it. This is NOT a
        // failure of the admin's action, so the modal closes and that job's
        // progress surfaces (same shape as removeYear's hand-off).
        notify(msg, "error");
        const list = await api.importJobs().catch(() => []);
        const runningJob = list.find((j) => !TERMINAL_JOB_STATUSES.includes(j.status));
        // { adopted: true } — by definition a job this session did NOT start,
        // which is exactly what the flag describes. Omitting it was the drift
        // the flag's own comment claimed immunity from.
        if (runningJob) watch(runningJob.id, { adopted: true });
        return;
      }
      // A genuine failure: rethrow so useConfirm keeps the modal open showing
      // the error. Swallowing it dismissed the dialog exactly like a success,
      // leaving a notice further down the page as the only trace.
      //
      // No notify() here — removeYear's genuine-failure branch throws WITHOUT
      // one, and the comment above claims to mirror it. Calling both reported a
      // single failure three times (in-modal error + errorToast + a page notice
      // behind the inert background) and set the notice-focus effect fighting
      // ConfirmModal for focus.
      throw err;
    } finally {
      setIntegrating(false);
    }
  }

  function removeYear(entry) {
    // The outcome is resolved in onConfirm and consumed by onSuccess (after the
    // modal closes + un-inerts): either a started removal, or a HAND-OFF to a
    // job already mid-flight. Both close the modal and surface a job to watch —
    // rethrowing on "already running" would trap that live job behind the inert
    // error modal (and drop focus to <body> once watch() unmounts the trashcan).
    let outcome = null; // { jobId, message, kind }
    confirm({
      variant: "danger",
      title: `Remove ${entry.year_label} from the database?`,
      body: "This rebuilds the database without that year and can't be undone.",
      confirmLabel: "Remove year",
      onConfirm: async () => {
        try {
          const body = await api.deintegrateYear(entry.start_year);
          outcome = { jobId: body.job_id, message: `Removing ${entry.year_label}…`, kind: "" };
        } catch (err) {
          let msg = "Could not start the removal.";
          msg = err?.detail || msg;   // ApiError carries the server's own wording
          if (/already running/i.test(msg)) {
            const list = await api.importJobs().catch(() => []);
            const runningJob = list.find((j) => !TERMINAL_JOB_STATUSES.includes(j.status));
            // Hand off to the running job: close the modal and show ITS progress
            // (matches the old inline path, which surfaced it immediately).
            if (runningJob) {
              outcome = { jobId: runningJob.id, message: msg, kind: "error",
                          adopted: true };
              return;
            }
          }
          throw err; // genuine failure -> modal stays open with the error
        }
      },
      errorToast: "Could not start the removal.",
      onSuccess: () => {
        // Set the notice BEFORE watch() flips `locked` — the focus-to-notice
        // effect above then lands focus on the notice (the trashcan that opened
        // the modal has since unmounted).
        notify(outcome.message, outcome.kind);
        setActiveYears(null);
        watch(outcome.jobId, { adopted: !!outcome.adopted });
      },
    });
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
        <div ref={noticeRef} tabIndex={-1} role="status"
             className={"notice" + (noticeKind ? " " + noticeKind : "")}>{notice}</div>
      )}
      {/* Rendered for the whole locked window, not just jobRunning: pressing
          "Integrate selected" or "Rebuild" disables that very button, so
          without a landing spot focus falls to <body> and the admin is dumped
          to the top of the page (WCAG 2.4.3) — mid-import, with no idea why
          the control vanished. This notice is the thing that answers that, so
          it is where focus goes (see the effect above). */}
      {(locked || uploading) && (
        <div ref={lockedRef} tabIndex={-1} className="notice" role="status">
          {adopted
            ? "An import started by another session is running… controls are locked until it finishes."
            : "An import is running… controls are locked until it finishes."}
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
          <div className="notice error" role="alert">
            Could not reach NCES to check available years.{" "}
            <button type="button" className="link" onClick={() => loadCatalog(true)}>Retry</button>
          </div>
        )}

        {catalog && (
          <>
            {catalog.partial && (
              <div className="notice warn" role="status">
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
        <summary>Manual upload (.accdb — offline / full rebuild)</summary>
        <p className="muted small">
          Drop the <strong>complete set</strong> of{" "}
          <code>IPEDS{"{YYYY}{YY}"}.accdb</code> files the database should contain — the
          rebuild replaces the dataset with exactly these, so include every year
          currently loaded plus any new ones (a build that would drop a live year is
          refused). To add a single year online, use <strong>NCES Integrate</strong> above.
        </p>
        <form onSubmit={upload}>
          <div
            className={"dropzone" + (dragging ? " dragging" : "") + (locked ? " disabled" : "")}
            onDragEnter={onDragEnter}
            onDragOver={onDragOver}
            onDragLeave={onDragLeave}
            onDrop={onDrop}
          >
            <span className="dropzone-hint" aria-hidden="true">
              {dragging ? "Drop the .accdb files" : "Drag .accdb files here, or"}
            </span>
            <label htmlFor="import-file" className="link">Choose files</label>
            <input
              id="import-file"
              ref={fileRef}
              type="file"
              accept=".accdb"
              multiple
              className="sr-only"
              disabled={locked}
              onChange={(e) => addFiles(e.target.files)}
            />
          </div>
          {/* Announce the SELECTION to a screen reader. The drag phase is
              mouse-only and would just churn the live region, so it's omitted. */}
          <div className="sr-only" role="status" aria-live="polite">
            {dropFiles.length ? `${dropFiles.length} file${dropFiles.length > 1 ? "s" : ""} selected` : ""}
          </div>
          {dropFiles.length > 0 && (
            <ul className="dropfile-list small">
              {dropFiles.map((f) => (
                <li key={f.name}>
                  {f.name} <span className="muted">({humanBytes(f.size)})</span>
                </li>
              ))}
            </ul>
          )}
          {uploadMsg && <p className="notice error small" role="alert">{uploadMsg}</p>}
          <button type="submit" disabled={uploading || locked || !dropFiles.length}>
            {uploading
              ? "Uploading…"
              : dropFiles.length
                ? `Rebuild from ${dropFiles.length} file${dropFiles.length > 1 ? "s" : ""}`
                : "Rebuild"}
          </button>
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
            {/* Caps at 40vh with nothing focusable inside — unreachable by
                keyboard without this (WCAG 2.1.1, Level A). */}
            <pre className="log thin-scroll" tabIndex={0} role="region"
                 aria-label="Import job log">{active.log || "…"}</pre>
          </details>
        </div>
      )}

      <h3>Recent jobs</h3>
      {/* The same reflow scroll region the other admin tables use (WCAG
          1.4.10). This one was measured as FITTING when #315 swept the rest —
          against fixtures whose filenames are a few characters long. A real
          IPEDS upload is not: `IPEDS_2023-24_Provisional_All_Data.zip` is one
          unbreakable token, and the localised timestamp beside it does not wrap
          either, so the table came to 440px at a 320px viewport and the whole
          `.admin` column scrolled sideways — heading and section nav with it.
          No `min-width`: like Top users this table is otherwise fluid, so it
          only scrolls when its own content is genuinely too wide.
          FOCUSABLE, by the same rule `needsScrollRegion` applies to a
          DataTable: the one thing a keyboard can land on in here is the "view"
          button in the last column, and no header is a sort button, so tabbing
          in scrolls the region to its RIGHT edge with nothing to bring File and
          Status back. */}
      <TableScroll focusable label="Recent jobs">
      <table className="grid" aria-label="Recent jobs">
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
      </TableScroll>
    </div>
  );
}

