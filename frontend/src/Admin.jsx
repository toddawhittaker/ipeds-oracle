import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { NavLink, Navigate, useParams } from "react-router-dom";
import { api } from "./api.js";
import Chart from "./Chart.jsx";
import { estimateIntegrate } from "./estimate.js";
import { USER_CONFIG } from "./userlist.js";
import { PENDING_CONFIG, BLOCKED_CONFIG } from "./accesstables.js";
import DataTable from "./DataTable.jsx";
import { buildImportPlan } from "./csvimport.js";
import { IconShieldPlus, IconShieldMinus, IconTrash, IconUpload, IconCheck, IconClose, IconUnlock } from "./icons.jsx";
import HelpPopover from "./HelpPopover.jsx";
import { useToast } from "./Toast.jsx";
import { useConfirm } from "./ConfirmModal.jsx";

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

// One source of truth for both the subtab nav and the /admin/:tab route
// validator below -- "users" is the route/label; the underlying component
// stays named Allowlist (it mirrors the /api/admin/allowlist endpoints it
// drives, which are unchanged by this rename).
export const ADMIN_TABS = ["users", "imports", "usage", "skills", "logs"];

// Reads the :tab route param and either renders Admin with it or bounces an
// unknown tab back to /admin/users. Kept separate from Admin itself so Admin
// stays a plain {tab} prop component, easy to reason about without also
// threading route matching through it.
export function AdminRoute({ me, onDataChanged }) {
  const { tab } = useParams();
  if (!ADMIN_TABS.includes(tab)) return <Navigate to="/admin/users" replace />;
  return <Admin me={me} tab={tab} onDataChanged={onDataChanged} />;
}

export default function Admin({ me, tab, onDataChanged }) {
  return (
    <main className="admin thin-scroll">
      <h1 className="sr-only">Admin</h1>
      <nav className="subtabs" aria-label="Admin sections">
        {ADMIN_TABS.map((t) => (
          <NavLink key={t} to={`/admin/${t}`} end
                   className={({ isActive }) => (isActive ? "on" : "")}>
            {t[0].toUpperCase() + t.slice(1)}
          </NavLink>
        ))}
      </nav>
      {tab === "users" && <Allowlist me={me} />}
      {tab === "imports" && <Imports onDataChanged={onDataChanged} />}
      {tab === "usage" && <Usage />}
      {tab === "skills" && <Skills />}
      {tab === "logs" && <Logs />}
    </main>
  );
}

// Mirrors app.auth.canon_email exactly (lowercase + strip a `+tag`
// local-part suffix, dots left alone) -- used ONLY to name the address that
// will ACTUALLY be blocked in the Reject confirm dialog (SEC #2, round-4
// security review). Canonicalization propagates the block TOWARD the base
// address, so a confirm that just echoes the literal typed-in string gets
// the direction backwards for a +tag input -- rejecting
// "victim+newsletter@example.edu" blocks "victim@example.edu", which is not
// itself "a +tag variant of" the address shown. Duplicated here (not
// imported) because this is a pure display concern with no reason to add a
// server round-trip just to phrase a confirm dialog -- keep it textually in
// sync with app.auth.canon_email if that ever changes.
function canonEmailForDisplay(email) {
  const trimmed = email.trim().toLowerCase();
  const at = trimmed.indexOf("@");
  if (at === -1) return trimmed;
  return trimmed.slice(0, at).split("+")[0] + trimmed.slice(at);
}

// App-standard date+time; null/absent → an em dash. Unix seconds in.
const fmtDateTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "—");

// Live-refresh cadence for the Allowlist tab so a request filed by someone else
// (or actioned in another admin session) shows up without a manual reload. The
// poll runs only while the tab is visible; a tick within COOLDOWN of the last
// load() is skipped so a background refresh can never commit its re-render on top
// of a mutation handler's rAF focus restore (the focus-restore-vs-reload race).
const ALLOWLIST_POLL_MS = 15000;
const ALLOWLIST_RELOAD_COOLDOWN_MS = 1500;

function Allowlist({ me }) {
  const [rows, setRows] = useState([]);
  const [reqs, setReqs] = useState([]);
  const [denied, setDenied] = useState([]);
  const [deniedError, setDeniedError] = useState("");
  const [email, setEmail] = useState("");
  const [note, setNote] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [busyEmail, setBusyEmail] = useState(""); // row action in flight
  // The Users table is the reusable <DataTable> (search/sort/paginate/aria-live/
  // focus all live there); this ref reaches its focusSearch()/focusRowAction()
  // imperative handle so a row action that unmounts or swaps its control can hand
  // focus somewhere sensible instead of dropping it to <body>.
  const usersTableRef = useRef(null);
  const pendingTableRef = useRef(null);
  const blockedTableRef = useRef(null);
  // Timestamp of the last committed reload. The background poll (below) skips a
  // tick within ALLOWLIST_RELOAD_COOLDOWN_MS of it so two polls don't stack.
  const lastLoadAt = useRef(0);
  // True while a mutation handler is reloading-then-restoring-focus. A live
  // refresh (poll OR visibility/focus) must NOT fire load() in that window: its
  // setState re-render would land on top of the rAF focus restore and drop focus
  // to <body> (the focus-restore-vs-reload race). Set via reloadThenRestoreFocus.
  const restoringFocus = useRef(false);
  // Action outcomes go to the app-wide toast (announce below). Toasts don't take
  // focus, so an action that UNMOUNTS the control it was fired from must hand
  // focus to a stable element or it drops to <body>: each table action -> that
  // table's own search box.
  const toast = useToast();
  const confirm = useConfirm();
  const addEmailRef = useRef(null);

  // Route an action outcome to the app-wide toast (overlays, auto-fades,
  // announced once via the toast host's live region). kind: "" | "ok" | "error".
  const announce = (text, kind = "") => toast(text, kind);

  // Run a focus move AFTER React has committed the reload's re-render. A single
  // requestAnimationFrame can fire BEFORE the commit under load, focusing a
  // stale/just-removed node and dropping focus to <body> (the
  // focus-restore-vs-reload race); waiting for the frame past the commit (double
  // rAF) lands it reliably. Every reload-then-focus site below goes through this.
  const focusNextCommit = (fn) => requestAnimationFrame(() => requestAnimationFrame(fn));

  // Focus the acting table's search box after a reload commits. If that table
  // just UNMOUNTED because its last row was actioned (pending → zero-state,
  // blocked → the whole section is removed), its ref is null; fall back to the
  // always-present add-email input so focus never drops to <body> (WCAG 2.4.3).
  const focusAfterRowAction = (tableRef) => focusNextCommit(() => {
    if (tableRef.current) tableRef.current.focusSearch();
    else addEmailRef.current?.focus?.();
  });

  // Reload the lists, then run a mutation's focus restoration, with the live
  // refresh (poll + visibility/focus) suppressed for the whole sequence so a
  // background load() can't re-render on top of the focus move and drop focus to
  // <body>. `restore` schedules the focus via focusNextCommit, so release the
  // guard on the same double-rAF horizon (after the focus has landed).
  const reloadThenRestoreFocus = async (restore) => {
    restoringFocus.current = true;
    try {
      await load();
      restore();
    } finally {
      focusNextCommit(() => { restoringFocus.current = false; });
    }
  };

  const load = () => {
    // Return the allowlist fetch so a caller can sequence focus AFTER the table
    // reload commits (the focus-restore-vs-reload race: restoring focus while a
    // reload re-renders the row drops focus to <body>).
    const loaded = api.allowlist().then(setRows);
    api.accessRequests().then(setReqs);
    // Unlike the two loaders above -- where an empty rendered result on
    // failure is indistinguishable from "genuinely nothing yet", which is
    // fine -- a silently-swallowed failure HERE (SEC #3, round-4 security
    // review) would be byte-identical to "nobody is blocked", the one
    // thing this section's entire job is to be able to say with
    // confidence. Render a real error state instead (see the JSX below).
    // Report only what the response actually says (a `detail` field, when
    // the server sent one) rather than inferring a cause -- same principle,
    // and the same JSON.parse(err.message).detail pattern, as
    // toggleAdmin's catch further down: guessing at a cause from a proxy
    // value instead of asking the server directly is exactly the class of
    // bug PR #57/#60 fixed for the invite-email flash.
    api.deniedRequests().then((d) => { setDenied(d); setDeniedError(""); })
      .catch((err) => {
        let detail = "";
        try { detail = JSON.parse(err.message).detail || ""; } catch { /* no JSON body to read */ }
        setDeniedError(detail || "Couldn't load blocked addresses.");
      });
    // Anchor the poll's cooldown to when this reload settles (see lastLoadAt),
    // on both success and failure. Kept off the returned `loaded` chain (which
    // callers await) so this bookkeeping never adds its own unhandled rejection.
    const stamp = () => { lastLoadAt.current = Date.now(); };
    loaded.then(stamp, stamp);
    return loaded;
  };
  useEffect(() => { load(); }, []);

  // Keep the three lists live so a request filed by someone else -- or a change
  // made in another admin session -- appears without a manual page reload:
  // refresh instantly when the admin returns to the tab, and poll lightly while
  // it's visible. Neither path fires while a mutation is restoring focus
  // (restoringFocus), so a refresh can never steal it; load() itself moves no
  // focus. The poll additionally skips a tick within the cooldown so two polls
  // (or a poll right after a just-committed reload) don't stack.
  useEffect(() => {
    const refreshIfVisible = () => {
      if (!document.hidden && !restoringFocus.current) load();
    };
    document.addEventListener("visibilitychange", refreshIfVisible);
    window.addEventListener("focus", refreshIfVisible);
    const id = setInterval(() => {
      if (document.hidden || restoringFocus.current) return;
      if (Date.now() - lastLoadAt.current < ALLOWLIST_RELOAD_COOLDOWN_MS) return;
      load();
    }, ALLOWLIST_POLL_MS);
    return () => {
      document.removeEventListener("visibilitychange", refreshIfVisible);
      window.removeEventListener("focus", refreshIfVisible);
      clearInterval(id);
    };
  }, []);

  // One message per outcome, keyed off the backend's `delivery` value. "No email
  // was sent" has THREE distinct causes needing different reactions, so never
  // collapse them: an earlier version inferred the cause from booleans and told
  // the admin an invite had FAILED when the person was simply already on the
  // allowlist and no mail was ever attempted.
  //
  // Note the dev link never lands in the admin Logs page either: logbuffer.py
  // drops the ipeds.mail logger outright and redacts `token=` everywhere else,
  // deliberately, so an admin browsing logs can't harvest a live sign-in link.
  // It's on the server's stdout/stderr only.
  const INVITE_FLASH = {
    emailed: (a) => `Approved — a sign-in link was emailed to ${a}.`,
    already_allowlisted: (a) =>
      `${a} was already on the allowlist, so no new invite was sent. They can ` +
      `sign in from the sign-in page whenever they like.`,
    failed: (a) =>
      `${a} added, but the invite email FAILED to send — check the Logs tab ` +
      `for the error. Their sign-in link wasn't saved anywhere, so ask them to ` +
      `request one from the sign-in page.`,
    logged_to_console: (a) =>
      `${a} added. No email was sent (no mail key configured) — the sign-in ` +
      `link is in the server console, not the Logs tab.`,
  };

  function inviteFlash(addr, res) {
    // The request itself failed — nothing was added. Saying "added" here (as
    // this did before) sends the admin off to chase a missing email for an
    // account that was never created.
    if (!res?.ok) return `Couldn't add ${addr} — the request failed. Try again.`;
    // Unknown/absent delivery: state only what we know rather than guessing a
    // cause. Silence beats a confident wrong answer here.
    return (INVITE_FLASH[res.delivery] ?? ((a) => `${a} added.`))(addr);
  }

  // Toast color for the invite outcome: red when nothing was added or the email
  // bounced, green when the link actually went out, neutral for the informational
  // "already on the list" / "no mail key" branches.
  function inviteKind(res) {
    if (!res?.ok || res.delivery === "failed") return "error";
    if (res.delivery === "emailed") return "ok";
    return "";
  }

  async function invite(addr, noteText, admin = false) {
    const res = await api.addAllow(addr, noteText, admin).catch(() => ({}));
    // inviteFlash() reports the backend-supplied `delivery` value instead of
    // inferring a cause from proxies (#60); announce() routes it through the
    // toast so a screen reader hears it once.
    announce(inviteFlash(addr, res), inviteKind(res));
    load();
  }

  async function add(e) {
    e.preventDefault();
    await invite(email, note, isAdmin);
    setEmail(""); setNote(""); setIsAdmin(false);
  }

  // --- CSV bulk import: drop a .csv -> parse+preview -> confirm -> report -----
  // Parsing/validation/dedupe all live in csvimport.js (unit-tested); this owns
  // only the drop zone, file read, and the summary/confirm/result flow.
  const [csvFileName, setCsvFileName] = useState("");
  const [csvPlan, setCsvPlan] = useState(null);     // buildImportPlan result (preview)
  const [csvError, setCsvError] = useState("");      // unsupported file / read failure
  const [csvBusy, setCsvBusy] = useState(false);     // bulk POST in flight
  const [csvResult, setCsvResult] = useState(null);  // { added, adminsGranted, report[] }
  const [csvDragging, setCsvDragging] = useState(false);
  const csvDragDepth = useRef(0);  // depth counter so child boundaries don't flicker
  const csvFileRef = useRef(null);
  const csvResultRef = useRef(null);  // focus anchor after a confirmed import

  function resetCsv() {
    setCsvFileName(""); setCsvPlan(null); setCsvError(""); setCsvResult(null);
    if (csvFileRef.current) csvFileRef.current.value = "";
    // Cancel / "Import another" both unmount the region under focus; hand focus
    // back to the drop target instead of dropping it to <body> (WCAG 2.4.3).
    requestAnimationFrame(() => csvFileRef.current?.focus());
  }

  async function onCsvFile(file) {
    if (!file) return;
    setCsvResult(null);
    setCsvFileName(file.name);
    // Don't trust the input's accept filter (a drop bypasses it); check here.
    if (!file.name.toLowerCase().endsWith(".csv")) {
      setCsvPlan(null);
      setCsvError("That's not a .csv file — please choose a CSV.");
      return;
    }
    setCsvError("");
    let text = "";
    try {
      text = await file.text();
    } catch {
      setCsvPlan(null);
      setCsvError("Couldn't read that file.");
      return;
    }
    const p = buildImportPlan(text, rows.map((r) => r.email),
      { today: new Date().toLocaleDateString() });
    setCsvPlan(p);
    // Announce the parse outcome once (the visible summary is the durable copy).
    // A header error is already announced by its inline role="alert" node below,
    // so don't ALSO toast it (that double-announces to a screen reader).
    if (!p.headerError) {
      announce(`CSV read: ${p.ready.length} ready, `
        + `${p.existingOrDuplicate.length} existing or duplicate, ${p.invalid.length} invalid.`);
    }
  }

  async function confirmCsv() {
    if (!csvPlan?.ready?.length) return;
    setCsvBusy(true);
    const res = await api.bulkAllow(
      csvPlan.ready.map(({ email: e, note: n, is_admin }) => ({ email: e, note: n, is_admin })),
    ).catch(() => null);
    setCsvBusy(false);
    if (!res?.ok) {
      setCsvError("Import failed — the request didn't go through. Try again.");
      return;
    }
    // Error report = client-detected invalid + existing/duplicate rows, PLUS any
    // rows the backend additionally skipped (mapped back to their file row via
    // the ready list). Sorted by file row; backend-only skips with no known row
    // sink to the end.
    const rowByEmail = new Map(csvPlan.ready.map((r) => [r.email, r.row]));
    const backendSkips = (res.skipped || []).map((s) => ({
      row: rowByEmail.get(s.email) ?? null, email: s.email, reason: s.reason,
    }));
    const report = [...csvPlan.invalid, ...csvPlan.existingOrDuplicate, ...backendSkips]
      .sort((a, b) => (a.row == null ? 1 : b.row == null ? -1 : a.row - b.row));
    setCsvResult({ added: res.added, adminsGranted: res.admins_granted, report });
    setCsvPlan(null);
    setCsvFileName("");
    if (csvFileRef.current) csvFileRef.current.value = "";
    // Announce BOTH sides — a screen-reader user who hears only "5 added" has no
    // cue the skipped-rows report appeared below (WCAG 4.1.3).
    const skipped = report.length
      ? `, ${report.length} row${report.length === 1 ? "" : "s"} skipped — see the report below` : "";
    announce(`${res.added} user${res.added === 1 ? "" : "s"} added from CSV${skipped}.`,
      res.added ? "ok" : "");
    // The "Add N users" button just unmounted; move focus to the result instead
    // of letting it fall to <body> (WCAG 2.4.3), sequenced AFTER load() commits.
    await reloadThenRestoreFocus(() =>
      focusNextCommit(() => csvResultRef.current?.focus()));
  }

  function onCsvDragEnter(e) { e.preventDefault(); csvDragDepth.current += 1; setCsvDragging(true); }
  function onCsvDragOver(e) { e.preventDefault(); }
  function onCsvDragLeave(e) {
    e.preventDefault();
    csvDragDepth.current = Math.max(0, csvDragDepth.current - 1);
    if (csvDragDepth.current === 0) setCsvDragging(false);
  }
  function onCsvDrop(e) {
    e.preventDefault();
    csvDragDepth.current = 0;
    setCsvDragging(false);
    onCsvFile(e.dataTransfer.files?.[0]);
  }

  // Approve a pending request: neutral confirmation modal (it grants access AND
  // emails a welcome link, so it's confirmed), then the delivery-aware toast.
  function approve(addr) {
    let outcome = null; // stash the backend delivery result for the onSuccess toast
    confirm({
      variant: "neutral",
      title: `Approve access for ${addr}?`,
      body: "This adds them to the allowlist and emails them a sign-in link.",
      confirmLabel: "Approve access",
      onConfirm: async () => {
        const res = await api.addAllow(addr, "approved request", false);
        if (!res?.ok) throw new Error(JSON.stringify({ detail: `Couldn't add ${addr}.` }));
        outcome = res;
      },
      errorToast: `Couldn't approve ${addr}.`,
      onSuccess: async () => {
        // Delivery-aware toast (emailed / already on the list / mail failed /
        // logged to console) — see INVITE_FLASH. The Approve button unmounted
        // with its pending row; hand focus to the pending table's search box.
        announce(inviteFlash(addr, outcome), inviteKind(outcome));
        await reloadThenRestoreFocus(() => focusAfterRowAction(pendingTableRef));
      },
    });
  }

  function reject(addr) {
    // Name the address that will ACTUALLY be blocked (SEC #2) -- canon_email
    // propagates the block toward the BASE address, so for a +tag input like
    // "victim+newsletter@example.edu" the address actually blocked is
    // "victim@example.edu", which the old copy's "+tag variants of THIS
    // address" phrasing had backwards.
    const target = canonEmailForDisplay(addr);
    confirm({
      variant: "danger",
      title: `Reject the request from ${addr}?`,
      body: `This blocks ${target} (and every +tag/case variant of it) from requesting access again.`,
      details: `You can undo the block from the "Blocked users" table below — that only lets them request again, it grants no access.`,
      confirmLabel: "Reject request",
      onConfirm: () => api.denyAccessRequest(addr),
      successToast: `Rejected the access request from ${addr}.`,
      errorToast: `Could not reject ${addr}.`,
      onSuccess: async () => {
        // The reject button just unmounted with its pending row; hand focus to
        // the pending table's search box so it doesn't drop to <body>. Sequence
        // after the reload commits (focus-restore-vs-reload race).
        await reloadThenRestoreFocus(() => focusAfterRowAction(pendingTableRef));
      },
    });
  }

  // Unblock: a neutral confirmation modal explaining that this only lets the
  // address request access again — it grants NO access and sends NO email.
  // `r.canon_email` is what the DELETE keys on; `r.emails` (the ORIGINAL
  // addresses) is what's shown.
  function undo(r) {
    const shown = r.emails.join(", ");
    confirm({
      variant: "neutral",
      title: `Allow ${r.canon_email} to request access again?`,
      body: "This will remove the user from the blocklist. It will not approve access; the user must submit a new request.",
      details: shown !== r.canon_email
        ? `Unblocks the whole mailbox — it was requested as ${shown}.` : undefined,
      confirmLabel: "Allow new request",
      onConfirm: () => api.clearDenial(r.canon_email), // match on canonical
      successToast: `${shown} may request access again — they were not given access, and no email was sent.`,
      errorToast: `Could not unblock ${shown}. They are still blocked from requesting access.`,
      onSuccess: async () => {
        // The unblock button unmounted with its blocked row; hand focus to the
        // blocked table's search box after the reload commits.
        await reloadThenRestoreFocus(() => focusAfterRowAction(blockedTableRef));
      },
    });
  }

  async function toggleAdmin(r) {
    setBusyEmail(r.email);
    try {
      await api.setAdmin(r.email, !r.is_admin);
      announce(r.is_admin
        ? `${r.email} is no longer an admin.`
        : `${r.email} is now an admin.`, "ok");
    } catch (err) {
      let msg = "Could not update admin status.";
      try { msg = JSON.parse(err.message).detail || msg; } catch { /* keep default */ }
      announce(msg, "error");
    } finally {
      setBusyEmail("");
    }
    // The row PERSISTS (only its shield swaps Make<->Remove admin), so return
    // focus to that same row's action button AFTER the reload commits — instead
    // of <body> where the briefly-disabled button dropped it. Sequenced after
    // load() per the focus-restore-vs-reload race rule. (Toasts never take focus,
    // so there's no notice to race here anymore.)
    await reloadThenRestoreFocus(() =>
      focusNextCommit(() => usersTableRef.current?.focusRowAction(r.email)));
  }

  // Destructive: a danger confirmation modal (naming the email), then the
  // app-styled result toast. The modal owns the in-flight/error state, so no
  // setBusyEmail here (the background is inert while it processes). After load()
  // refetches, the derived viewUsers() clamp keeps the admin on their page (or
  // drops to the previous one if this emptied the last page). Self-removal never
  // reaches here — the current admin's row shows no actions (backend also 400s it).
  function removeUser(r) {
    confirm({
      variant: "danger",
      title: `Remove ${r.email} from the allowlist?`,
      body: "This drops any admin access and signs them out. You can re-add them later.",
      confirmLabel: "Remove user",
      onConfirm: () => api.removeAllow(r.email),
      successToast: `Removed ${r.email} from the allowlist.`,
      errorToast: `Couldn't remove ${r.email}.`,
      onSuccess: async () => {
        // The trash button unmounted with its row; hand focus to the table's
        // search box (stable, in-context) after the reload commits so it doesn't
        // drop to <body>. The viewRows() page-clamp keeps the admin on a valid
        // page (or the previous one if this emptied the last page).
        await reloadThenRestoreFocus(() =>
          focusNextCommit(() => usersTableRef.current?.focusSearch()));
      },
    });
  }

  return (
    <div className="panel">
      {/* Action outcomes surface via the app-wide toast (useToast) — overlays,
          auto-fades, announced once — not an in-flow flash box. Pending requests
          get a restrained attention treatment (accent border + tinted header +
          count badge) ONLY while there's something to review, so it draws the eye
          without ever looking like an error. Empty = plain header + a clear
          "nothing awaiting" note (distinct from a search-miss). */}
      <section className={"requests-section" + (reqs.length ? " attention" : "")}>
        <h2 className="section-head">
          Pending requests
          {reqs.length > 0 && (
            <span className="pending-badge">
              <span aria-hidden="true">· {reqs.length}</span>
              <span className="sr-only">{reqs.length} awaiting review</span>
            </span>
          )}
        </h2>
        {reqs.length === 0 ? (
          <p className="empty-note">No access requests are awaiting review.</p>
        ) : (
          <DataTable
            ref={pendingTableRef}
            rows={reqs}
            rowKey={(r) => r.id}
            config={PENDING_CONFIG}
            ariaLabel="Pending access requests"
            searchPlaceholder="Search by email"
            searchLabel="Search pending requests by email"
            sizeLabel="Requests per page"
            emptyNoMatch="No pending requests match your search."
            initialSort={{ key: "requested", dir: "desc" }}
            sortLabels={{ email: "email", requested: "requested" }}
            columns={[
              { key: "email", label: "Email", sortable: true, colClass: "col-req-email",
                cellClass: "cell-trunc", cellTitle: (r) => r.email },
              { key: "requested", label: "Requested", sortable: true, colClass: "col-when",
                render: (r) => fmtDateTime(r.created_at) },
            ]}
            renderActions={(r) => (
              <>
                <button type="button" className="icon-btn tip" data-tip="Approve request"
                        aria-label={`Approve request from ${r.email}`}
                        onClick={() => approve(r.email)}>
                  <IconCheck />
                </button>
                <button type="button" className="icon-btn danger tip" data-tip="Reject request"
                        aria-label={`Reject request from ${r.email}`}
                        onClick={() => reject(r.email)}>
                  <IconClose />
                </button>
              </>
            )}
          />
        )}
      </section>

      <h2>Users</h2>
      <form className="row" onSubmit={add}>
        <label htmlFor="allow-email" className="sr-only">Email</label>
        <input id="allow-email" ref={addEmailRef} type="email" placeholder="email" required value={email}
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

      <details className="csv-import">
        <summary>Import from CSV</summary>
        <div className="csv-import-body">
          <div className="csv-dropwrap">
            <label
              className={"dropzone csv-dropzone" + (csvDragging ? " dragging" : "")}
              htmlFor="csv-file"
              onDragEnter={onCsvDragEnter}
              onDragOver={onCsvDragOver}
              onDragLeave={onCsvDragLeave}
              onDrop={onCsvDrop}
            >
              <IconUpload size={22} />
              <span className="csv-dropzone-hint" aria-hidden="true">
                {csvDragging ? "Drop the CSV file" : "Drop a CSV file here or click to select one"}
              </span>
              <input id="csv-file" ref={csvFileRef} type="file" accept=".csv"
                     className="sr-only" aria-label="Choose a CSV file to import"
                     onChange={(e) => onCsvFile(e.target.files?.[0])} />
            </label>
            <span className="csv-help-slot">
              <HelpPopover label="CSV format help">
                <div className="help-body">
                  <p>Upload a CSV with a <strong>header row</strong>. Only{" "}
                    <code>email</code> is required; <code>note</code> and{" "}
                    <code>admin</code> are optional.</p>
                  <ul>
                    <li>Column names are matched loosely — capitalization,
                      punctuation, and spacing variants all work
                      (<code>Email</code>, <code>E-mail</code>, <code>e_mail</code>).</li>
                    <li>A blank <code>admin</code> value means <em>not</em> an admin.
                      Accepted true values (any case): <code>yes, y, t, true, 1, x</code>.
                      Everything else is false.</li>
                    <li>A blank <code>note</code> becomes <em>Imported on {"{date}"}</em>.</li>
                  </ul>
                  <pre>{`email,note,admin
alex@example.com,Department chair,yes
jamie@example.com,External reviewer,`}</pre>
                </div>
              </HelpPopover>
            </span>
          </div>

          {csvError && <p className="notice error small" role="alert">{csvError}</p>}

          {csvFileName && !csvError && (
            <p className="csv-filename">Selected: <strong>{csvFileName}</strong></p>
          )}

          {csvPlan?.headerError && (
            <p className="notice error small" role="alert">{csvPlan.headerError}</p>
          )}

          {csvPlan && !csvPlan.headerError && (
            <div className="csv-summary">
              <ul>
                <li>Total rows detected: <strong>{csvPlan.totalRows}</strong></li>
                <li>Users ready to add: <strong>{csvPlan.ready.length}</strong></li>
                <li>Existing or duplicate: <strong>{csvPlan.existingOrDuplicate.length}</strong></li>
                <li>Invalid rows: <strong>{csvPlan.invalid.length}</strong></li>
                <li>Receiving administrator access: <strong>{csvPlan.adminCount}</strong></li>
              </ul>
              <div className="row">
                <button type="button" onClick={confirmCsv}
                        disabled={csvBusy || !csvPlan.ready.length} aria-busy={csvBusy}>
                  {csvBusy ? "Adding…"
                    : `Add ${csvPlan.ready.length} user${csvPlan.ready.length === 1 ? "" : "s"}`}
                </button>
                <button type="button" className="link" onClick={resetCsv} disabled={csvBusy}>
                  Cancel
                </button>
              </div>
            </div>
          )}

          {csvResult && (
            <div className="csv-result">
              <p className="notice ok small">
                {csvResult.added} user{csvResult.added === 1 ? "" : "s"} added
                {csvResult.adminsGranted
                  ? ` (${csvResult.adminsGranted} with admin)` : ""}.
              </p>
              {csvResult.report.length > 0 && (
                <table className="grid csv-report">
                  <caption className="csv-report-cap">Skipped rows</caption>
                  <thead>
                    <tr>
                      <th scope="col">Row</th>
                      <th scope="col">Email</th>
                      <th scope="col">Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {csvResult.report.map((s, i) => (
                      <tr key={i}>
                        <td>{s.row ?? "—"}</td>
                        <td>{s.email || "—"}</td>
                        <td>{s.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <button type="button" className="link" ref={csvResultRef} onClick={resetCsv}>
                Import another file
              </button>
            </div>
          )}
        </div>
      </details>

      <DataTable
        ref={usersTableRef}
        rows={rows}
        rowKey={(r) => r.email}
        config={USER_CONFIG}
        tableClass="grid data users"
        ariaLabel="Allowlisted users"
        searchId="user-search"
        searchPlaceholder="Search email or note"
        searchLabel="Search email or note"
        sizeLabel="Users per page"
        emptyNoData="No users yet."
        emptyNoMatch="No users match your search."
        initialSort={{ key: "email", dir: "asc" }}
        sortLabels={{ email: "email", note: "note", admin: "admin status", last_login: "last login" }}
        columns={[
          { key: "email", label: "Email", sortable: true, colClass: "col-email",
            cellClass: "cell-trunc", cellTitle: (r) => r.email },
          { key: "note", label: "Note", sortable: true, colClass: "col-note",
            cellClass: "cell-trunc", cellTitle: (r) => r.note || undefined },
          { key: "admin", label: "Admin", sortable: true, colClass: "col-admin",
            // Ternary, not `&&`: is_admin is a NUMBER (0/1), and `0 && …` would
            // render a literal "0" in a non-admin's cell.
            render: (r) => (r.is_admin ? (
              <span className="admintoggle on">
                {me && r.email === me.email ? "✓ Admin (you)" : "✓ Admin"}
              </span>
            ) : null) },
          { key: "last_login", label: "Last login", sortable: true, colClass: "col-login",
            render: (r) => (r.last_login ? new Date(r.last_login * 1000).toLocaleDateString() : "—") },
        ]}
        renderActions={(r) => {
          const isSelf = me && r.email === me.email;
          if (isSelf) return null;
          const busy = busyEmail === r.email;
          return (
            <>
              {r.is_admin ? (
                <button type="button" className="icon-btn tip" data-tip="Remove admin"
                        aria-label="Remove admin" disabled={busy} onClick={() => toggleAdmin(r)}>
                  <IconShieldMinus />
                </button>
              ) : (
                <button type="button" className="icon-btn tip" data-tip="Make admin"
                        aria-label="Make admin" disabled={busy} onClick={() => toggleAdmin(r)}>
                  <IconShieldPlus />
                </button>
              )}
              <button type="button" className="icon-btn danger tip" data-tip="Remove user"
                      aria-label="Remove user" disabled={busy} onClick={() => removeUser(r)}>
                <IconTrash />
              </button>
            </>
          );
        }}
      />

      {/* Plain subdued section, not the --user tint used above for "needs
          your action" — a block is the opposite: something already handled
          that's merely visible/auditable here. Hidden entirely when there's
          nothing denied AND nothing failed to load — same as the
          pending-requests block above, except a load failure (SEC #3) must
          still show something rather than looking identical to "empty". */}
      {/* Blocked users. Hidden entirely when nothing is denied AND nothing
          failed to load — EXCEPT a load failure (SEC #3) must still show a
          visible error rather than looking identical to "nobody is blocked",
          the one thing this section exists to state with confidence. */}
      {(denied.length > 0 || deniedError) && (
        <section className="blocked-section">
          <h2>Blocked users</h2>
          {deniedError ? (
            // Its own class (not a bare `.notice`): a persistent in-flow error,
            // distinct from transient toasts, and off `.notice` so it doesn't
            // collide with unscoped `.notice`/`.toast` locators elsewhere.
            <p className="denied-error" role="alert">{deniedError}</p>
          ) : (
            <>
              <p className="denied-help">
                Rejecting a request blocks that address from asking again. Allowing
                a blocked user only lets them request access again — it grants no
                access and sends no email.
              </p>
              <DataTable
                ref={blockedTableRef}
                rows={denied}
                rowKey={(r) => r.id}
                config={BLOCKED_CONFIG}
                ariaLabel="Blocked users"
                searchPlaceholder="Search by email"
                searchLabel="Search blocked users by email"
                sizeLabel="Blocked users per page"
                emptyNoMatch="No blocked users match your search."
                initialSort={{ key: "denied", dir: "desc" }}
                sortLabels={{ email: "email", requested: "requested", denied: "denied" }}
                columns={[
                  // SEC #1: canon_email (the ACTUALLY-blocked mailbox — what
                  // is_denied() matches and Undo's DELETE keys on) is the PRIMARY
                  // label, never hidden, with the original addresses as a note when
                  // they differ. Do NOT collapse to just `emails`: a +tag-only
                  // griefing request would then hide the real victim's base address.
                  // `others` excludes canon so it's never rendered as a second text
                  // node (an unscoped getByText(canon_email) must resolve to one).
                  { key: "email", label: "Email", sortable: true, colClass: "col-blocked-email",
                    cellClass: "blocked-email", cellTitle: (r) => r.canon_email,
                    render: (r) => {
                      const others = r.emails.filter((e) => e !== r.canon_email);
                      return (
                        <>
                          <span className="denied-primary">{r.canon_email}</span>
                          {others.length > 0 && (
                            <span className="denied-note">
                              {" "}— requested as {others.join(", ")}; the block covers this whole mailbox
                            </span>
                          )}
                        </>
                      );
                    } },
                  // SEC #4: created_at is when the request was FILED (labeled
                  // "Requested"); denied_at (migration 11) is when it was rejected
                  // ("Denied"). The two are separate columns — neither overwrites
                  // the other. denied_at is null for pre-migration denials → "—".
                  { key: "requested", label: "Requested", sortable: true, colClass: "col-when",
                    render: (r) => fmtDateTime(r.created_at) },
                  { key: "denied", label: "Denied", sortable: true, colClass: "col-when",
                    // A pre-migration denial has no denied_at — a bare "—" reads
                    // as silence/"dash" to a screen reader, so name it.
                    render: (r) => (r.denied_at ? fmtDateTime(r.denied_at) : (
                      <><span aria-hidden="true">—</span><span className="sr-only">Not recorded</span></>
                    )) },
                ]}
                renderActions={(r) => (
                  // Icon-only: the data-tip "Allow new access request" is a prefix
                  // of the accessible name (WCAG 2.5.3 Label in Name); the canonical
                  // address in the name disambiguates rows for speech/SR nav. The
                  // address is only an attribute here, never a duplicate text node.
                  <button type="button" className="icon-btn tip" data-tip="Allow new access request"
                          aria-label={`Allow new access request for ${r.canon_email}`}
                          onClick={() => undo(r)}>
                    <IconUnlock />
                  </button>
                )}
              />
            </>
          )}
        </section>
      )}
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
    let data = {};
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
    notify("");
    setIntegrating(true);
    const years = Array.from(selected);
    try {
      const body = await api.integrateYears(years);
      setActiveYears(years.slice().sort((a, b) => a - b));
      watch(body.job_id);
    } catch (err) {
      let msg = "Could not start the import.";
      try { msg = JSON.parse(err.message).detail || msg; } catch { /* keep default */ }
      notify(msg, "error");
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
          try { msg = JSON.parse(err.message).detail || msg; } catch { /* keep default */ }
          if (/already running/i.test(msg)) {
            const list = await api.importJobs().catch(() => []);
            const runningJob = list.find((j) => !TERMINAL_JOB_STATUSES.includes(j.status));
            // Hand off to the running job: close the modal and show ITS progress
            // (matches the old inline path, which surfaced it immediately).
            if (runningJob) { outcome = { jobId: runningJob.id, message: msg, kind: "error" }; return; }
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
        watch(outcome.jobId);
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
  const toast = useToast();
  const confirm = useConfirm();
  const [rows, setRows] = useState([]);
  const [editingId, setEditingId] = useState(null);   // at most one card at a time
  const [draft, setDraft] = useState({ headline: "", lesson: "", canonical_sql: "" });
  // Focus returns to the "edit" button when the editor closes (a11y). We can't
  // hold the clicked node like Chat.jsx does: opening the editor unmounts that
  // button, so the captured node is detached and focusing it silently no-ops.
  // Keep a per-lesson ref map instead and re-find the freshly mounted button.
  const editBtnRefs = useRef({});   // skill id -> its "edit" button node
  const headlineRef = useRef(null);
  const headingRef = useRef(null);  // focus target after a card is deleted
  const load = () => api.skills().then(setRows);
  useEffect(() => { load(); }, []);
  const pending = rows.filter((r) => !r.verified).length;

  // Action outcomes go to the app-wide toast (visible + announced once) — the
  // Skills tab previously had only an sr-only status region, so sighted admins
  // got no confirmation at all. Focus management (editBtnRefs) is independent
  // and unchanged below.
  const announce = (text, kind = "") => toast(text, kind);

  const setVerified = (s, verified) =>
    api.patchSkill(s.id, { verified }).then(() => {
      announce(verified ? "Lesson verified." : "Lesson moved back to unverified.", "ok");
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
    }).then(async () => {
      announce("Lesson updated.");
      setEditingId(null);
      // Restore focus only AFTER the list reload has re-rendered. Doing it in
      // closeEdit's rAF while load() runs concurrently races the reload's
      // setRows, which remounts the edit button under the just-focused node and
      // drops focus to <body> — a timing-dependent flake under gate load.
      await load();
      requestAnimationFrame(() => editBtnRefs.current[s.id]?.focus?.());
    }).catch(() => announce("Couldn't save that lesson — nothing was changed.", "error"));
  }
  const focusHeading = () => requestAnimationFrame(() => headingRef.current?.focus?.());
  const reject = (s) => {
    // Confirm only when there's curated/used data to lose — a fresh unreviewed
    // proposal (verified=false, no votes/hits) can be dismissed without nagging.
    const risky = s.verified || s.upvotes > 0 || s.hits > 0;
    if (!risky) {
      // No modal, but still route the outcome through a toast + move focus off
      // the card that's about to unmount (it previously dropped to <body>).
      api.deleteSkill(s.id)
        .then(() => { announce("Lesson rejected.", "ok"); return load(); })
        .then(focusHeading)
        .catch(() => announce("Couldn't delete that lesson.", "error"));
      return;
    }
    confirm({
      variant: "danger",
      title: `Delete this ${s.verified ? "verified " : ""}lesson?`,
      body: "This permanently deletes the lesson. This action cannot be undone.",
      details: ruleName(s),
      confirmLabel: "Delete lesson",
      onConfirm: () => api.deleteSkill(s.id),
      successToast: "Lesson rejected.",
      errorToast: "Couldn't delete that lesson.",
      onSuccess: async () => { await load(); focusHeading(); },
    });
  };

  return (
    <div className="panel">
      <h2 ref={headingRef} tabIndex={-1}>Learned lessons ({rows.length})</h2>
      <p className="muted small">
        Rules the assistant applies as guidance. The post-answer critic proposes a
        lesson when it catches a mistake — a short headline plus a longer
        description — and it starts <strong>unverified</strong> until you approve
        it here.
        {pending > 0 && ` ${pending} awaiting review.`}
      </p>
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
