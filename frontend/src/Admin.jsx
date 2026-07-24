import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { NavLink, Navigate, useNavigate, useParams } from "react-router-dom";
import { api } from "./api.js";
import Chart from "./Chart.jsx";
import { shortZone } from "./datetime.js";
import { estimateIntegrate } from "./estimate.js";
import { USER_CONFIG } from "./userlist.js";
import { PENDING_CONFIG, BLOCKED_CONFIG } from "./accesstables.js";
import DataTable from "./DataTable.jsx";
import SqlBlock from "./SqlBlock.jsx";
import { buildImportPlan } from "./csvimport.js";
import { IconShieldPlus, IconShieldMinus, IconTrash, IconUpload, IconCheck, IconClose, IconUnlock, IconInfo } from "./icons.jsx";
import HelpPopover from "./HelpPopover.jsx";
import { useToast } from "./Toast.jsx";
import { useConfirm } from "./ConfirmModal.jsx";
import { useTableSelection } from "./useTableSelection.js";
import BulkBar from "./BulkBar.jsx";
import { bulkConfirmSummary, bulkResultToast, partitionEligibility, retainedSelectionAfterBulk } from "./selection.js";
import { USER_SUBTABS, DEFAULT_SUBTAB, resolveSubTab, subTabKeyForArrow, pendingBadgeTone } from "./usertabs.js";
import { formatBadge } from "./attention.js";
import { STAT_INFO, directionHint } from "./usageinfo.js";
import {
  exhaustionLabel,
  groundedFigureLabel, groundedFigureRate, groundedTableLabel, groundedTableRate,
  leakLabel, leakRate,
  promptCacheRate, schemaCacheRate,
} from "./usagestats.js";

// Bulk-action button/confirm labels: DIGITS always (never spelled out), unlike
// the prose summary/toast strings selection.js builds — see the architect's
// contract. `n` is the ELIGIBLE count (what will actually happen), not the
// raw selected count.
// Counted labels for the CONFIRM dialog's action button (e.g. "Promote 9
// users") — the count belongs in the dialog, where the exact breakdown is
// spelled out, never on the toolbar button itself.
const BULK_ACTION_LABEL = {
  promote: (n) => `Promote ${n} ${n === 1 ? "user" : "users"}`,
  demote: (n) => `Demote ${n} ${n === 1 ? "administrator" : "administrators"}`,
  delete: (n) => `Remove ${n} ${n === 1 ? "user" : "users"}`,
  approve: (n) => `Approve ${n} ${n === 1 ? "request" : "requests"}`,
  reject: (n) => `Reject and block ${n} ${n === 1 ? "request" : "requests"}`,
  unblock: (n) => `Allow ${n} ${n === 1 ? "user" : "users"} to request again`,
};

// Stable-verb labels for the TOOLBAR action buttons — no counts (they'd churn
// on every selection change); the verb matches the confirm dialog's verb.
const BULK_TOOLBAR_LABEL = {
  promote: "Promote", demote: "Demote", delete: "Remove",
  approve: "Approve", reject: "Reject & block", unblock: "Allow to request again",
};

// Shown as the tooltip on a toolbar action button that's disabled because none
// of the current selection is eligible for it (title only appears on hover, so
// this never has to be screen-reader-reachable — a disabled button is skipped
// by AT anyway, and the always-visible "N selected" count carries the state).
const BULK_DISABLED_REASON = {
  promote: "No selected users are regular users to promote.",
  demote: "No selected users are administrators to demote.",
  delete: "Selected administrators must be demoted before removal.",
  approve: "No selected requests can be approved.",
  reject: "No selected requests can be rejected.",
  unblock: "No selected users can be unblocked.",
};

const BULK_TITLE = {
  promote: (n) => `Promote ${n} ${n === 1 ? "user" : "users"} to admin?`,
  demote: (n) => `Demote ${n} ${n === 1 ? "administrator" : "administrators"}?`,
  delete: (n) => `Remove ${n} ${n === 1 ? "user" : "users"} from the allowlist?`,
  approve: (n) => `Approve ${n} pending ${n === 1 ? "request" : "requests"}?`,
  reject: (n) => `Reject and block ${n} ${n === 1 ? "request" : "requests"}?`,
  unblock: (n) => `Allow ${n} ${n === 1 ? "user" : "users"} to request access again?`,
};

const BULK_VARIANT = {
  promote: "neutral", demote: "warning", delete: "danger",
  approve: "neutral", reject: "danger", unblock: "neutral",
};

const BULK_ICON = {
  promote: IconShieldPlus, demote: IconShieldMinus, delete: IconTrash,
  approve: IconCheck, reject: IconClose, unblock: IconUnlock,
};

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

// Former standalone user-management pages redirect INTO the Users sub-tabs, so
// old bookmarks/links keep working. These segments are deliberately NOT in
// ADMIN_TABS, and the alias map is checked first, so an alias never collides
// with a real tab.
const USERS_TAB_ALIASES = { pending: "pending", blocked: "blocked", allowlist: "current" };

// The active Users sub-tab is remembered per browser session so returning via
// the bare /admin/users link (the outer subtab nav) reopens the tab you left —
// the spec's "...or was previously selected during the current administrative
// session". A :sub present in the URL always wins over this.
const USERS_SUBTAB_STORAGE_KEY = "admin.usersSubTab";
function rememberedSubTab() {
  try { return resolveSubTab(sessionStorage.getItem(USERS_SUBTAB_STORAGE_KEY)); }
  catch { return DEFAULT_SUBTAB; }
}
function rememberSubTab(sub) {
  try { sessionStorage.setItem(USERS_SUBTAB_STORAGE_KEY, sub); } catch { /* storage disabled */ }
}

// Reads the :tab (and, for Users, :sub) route params and renders Admin, or
// redirects: a legacy alias -> its Users sub-tab; an unknown tab -> Users; a
// bare/invalid Users sub -> the remembered-or-default sub, canonicalized into
// the URL so every view has a distinct, bookmarkable address; a stray :sub on a
// non-Users tab -> the bare tab. Kept separate from Admin so Admin stays a
// plain props component.
export function AdminRoute({ me, onDataChanged, attention, onAttentionChanged }) {
  const { tab, sub } = useParams();
  if (Object.prototype.hasOwnProperty.call(USERS_TAB_ALIASES, tab)) {
    return <Navigate to={`/admin/users/${USERS_TAB_ALIASES[tab]}`} replace />;
  }
  if (!ADMIN_TABS.includes(tab)) return <Navigate to="/admin/users/current" replace />;
  if (tab === "users") {
    const resolved = resolveSubTab(sub);
    // Bare /admin/users (sub == null) restores the remembered sub-tab; an
    // invalid sub falls back to the default. Either way, redirect so the URL
    // always names the concrete active tab.
    if (sub !== resolved) {
      return <Navigate to={`/admin/users/${sub == null ? rememberedSubTab() : resolved}`} replace />;
    }
    return <Admin me={me} tab={tab} sub={resolved} onDataChanged={onDataChanged}
                  attention={attention} onAttentionChanged={onAttentionChanged} />;
  }
  if (sub != null) return <Navigate to={`/admin/${tab}`} replace />;
  return <Admin me={me} tab={tab} onDataChanged={onDataChanged}
                attention={attention} onAttentionChanged={onAttentionChanged} />;
}

export default function Admin({ me, tab, sub, onDataChanged, attention, onAttentionChanged }) {
  // Attention counts default to empty so the nav renders unbadged if the Shell
  // hasn't fetched yet (or a test mounts Admin directly). refresh is a no-op
  // fallback for the same reason.
  const counts = attention || {};
  const refreshAttention = onAttentionChanged || (() => {});
  return (
    <main className="admin thin-scroll">
      <h1 className="sr-only">Admin</h1>
      <nav className="subtabs" aria-label="Admin sections">
        {ADMIN_TABS.map((t) => {
          // Only areas with an actionable backlog carry a count (users/skills/
          // logs); imports/usage are absent from `counts` → no badge.
          const badge = formatBadge(counts[t]);
          const n = counts[t] || 0;
          return (
            // Users drops `end` so it stays active across its sub-tab paths
            // (/admin/users/current|pending|blocked, all prefixes of /admin/users);
            // the other tabs match exactly.
            <NavLink key={t} to={`/admin/${t}`} end={t !== "users"}
                     aria-label={n > 0 ? `${t[0].toUpperCase() + t.slice(1)}, ${n} awaiting attention` : undefined}
                     className={({ isActive }) => (isActive ? "on" : "")}>
              {t[0].toUpperCase() + t.slice(1)}
              {badge && <span className="tab-badge attention" aria-hidden="true">{badge}</span>}
            </NavLink>
          );
        })}
      </nav>
      {tab === "users" && <Allowlist me={me} sub={sub} onAttentionChanged={refreshAttention} />}
      {tab === "imports" && <Imports onDataChanged={onDataChanged} />}
      {tab === "usage" && <Usage />}
      {tab === "skills" && <Skills onAttentionChanged={refreshAttention} />}
      {tab === "logs" && <Logs onAttentionChanged={refreshAttention} />}
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

// Local-date string for the audit note stored on a user added/allowlisted
// ("approved|added on <date> by <admin>"). Rendered in the viewer's locale, like
// every other date in the app (last_login, the CSV "Imported on" default, etc.).
const fmtApprovalDate = (d = new Date()) => d.toLocaleDateString();

// Live-refresh cadence for the Allowlist tab so a request filed by someone else
// (or actioned in another admin session) shows up without a manual reload. The
// poll runs only while the tab is visible; a tick within COOLDOWN of the last
// load() is skipped so a background refresh can never commit its re-render on top
// of a mutation handler's rAF focus restore (the focus-restore-vs-reload race).
const ALLOWLIST_POLL_MS = 15000;
const ALLOWLIST_RELOAD_COOLDOWN_MS = 1500;

function Allowlist({ me, sub, onAttentionChanged }) {
  const refreshAttention = onAttentionChanged || (() => {});
  const navigate = useNavigate();
  // Roving-focus refs for the sub-tab buttons: keyboard nav focuses the target.
  const tabRefs = useRef({});
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

  // Bulk row-selection: one independent hook instance per table (spec: "no
  // shared state" — selecting on one table never affects another).
  const usersSel = useTableSelection();
  const pendingSel = useTableSelection();
  const blockedSel = useTableSelection();

  // Route an action outcome to the app-wide toast (overlays, auto-fades,
  // announced once via the toast host's live region). kind: "" | "ok" | "error".
  const announce = (text, kind = "") => toast(text, kind);

  // Where a mutation's focus should land once its reload has COMMITTED. Set after
  // `await load()`, consumed by the layout effect below. Descriptor shapes:
  //   { kind: "tableSearch", tableRef }   -> that table's search box (add-email fallback)
  //   { kind: "rowAction",   tableRef, key } -> that row's action button (search fallback)
  //   { kind: "el",          elRef }      -> a specific element (e.g. CSV result)
  // Each carries a fresh `nonce` so repeating the SAME target (reject two rows in
  // a row) still re-fires the effect (new object identity).
  const [pendingFocus, setPendingFocus] = useState(null);
  const focusNonce = useRef(0);

  // Restore focus AFTER React has committed the reload's re-render — driven by
  // COMMITTED STATE, not a requestAnimationFrame frame count. A layout effect
  // keyed on `pendingFocus` is guaranteed to run after the DOM commit that
  // reflects the reloaded data, so the target is re-derived from LIVE refs against
  // the real post-reload DOM (a row that unmounted is genuinely gone here, not
  // "maybe still there for another frame"). This is the durable fix for the
  // focus-restore-vs-reload race — rAF-counting was fundamentally a guess.
  useLayoutEffect(() => {
    if (!pendingFocus) return;
    const f = pendingFocus;
    if (f.kind === "tableSearch") {
      if (f.tableRef.current) f.tableRef.current.focusSearch();
      else addEmailRef.current?.focus?.();
    } else if (f.kind === "rowAction") {
      // toggleAdmin's row PERSISTS (the shield just swaps Promote<->Demote), so
      // the button is always present post-commit — no fallback needed, matching
      // the prior behavior. focus() returns undefined, so its result can't signal
      // success anyway; the layout-effect timing is what makes this reliable now.
      f.tableRef.current?.focusRowAction(f.key);
    } else if (f.kind === "el") {
      f.elRef.current?.focus?.();
    }
    // Release the poll/visibility guard now that focus has landed (a ref write, so
    // it's allowed in an effect — unlike setState). The request isn't cleared: each
    // reloadThenRestoreFocus sets a fresh object (new nonce), so the effect re-fires
    // per request without a set-state-in-effect (an error under this repo's lint).
    restoringFocus.current = false;
  }, [pendingFocus]);

  // Reload the lists, then restore a mutation's focus once the reload commits,
  // with the live refresh (poll + visibility/focus) suppressed for the whole
  // sequence so a background load() can't re-render on top of the focus move and
  // drop focus to <body>. `focusReq` is a descriptor (see pendingFocus); it's set
  // AFTER load()'s setState so the layout effect fires post-commit. try/catch so a
  // FAILED reload still restores focus and releases the guard.
  const reloadThenRestoreFocus = async (focusReq) => {
    restoringFocus.current = true;
    try {
      await load();
    } catch { /* still restore focus + release the guard below */ }
    setPendingFocus({ ...focusReq, nonce: focusNonce.current++ });
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
    // Keep the Users attention badge in step with this tab's own data — every
    // approve/reject/deny/clear reloads here, so the badge drops immediately
    // instead of waiting out the Shell's 30s poll.
    refreshAttention();
    return loaded;
  };
  useEffect(() => { load(); }, []);

  // Remember the active sub-tab for this browser session so the outer "Users"
  // subtab link (which points at the bare /admin/users) reopens where the admin
  // left off. The URL's :sub always wins when present; this only feeds
  // AdminRoute's bare-path redirect.
  useEffect(() => { rememberSubTab(sub); }, [sub]);

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
    emailed: (a) => `Approved — an approval email was sent to ${a}. They can ` +
      `request a sign-in link from the sign-in page when ready.`,
    already_allowlisted: (a) =>
      `${a} was already on the allowlist, so no new email was sent. They can ` +
      `sign in from the sign-in page whenever they like.`,
    failed: (a) =>
      `${a} approved, but the approval email FAILED to send — check the Logs tab ` +
      `for the error. They can still request a sign-in link from the sign-in page.`,
    logged_to_console: (a) =>
      `${a} approved. No email was sent (no mail key configured) — the approval ` +
      `notice is in the server console. They can request a sign-in link any time.`,
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
    // An empty note defaults to an audit trail: who added the user, and when.
    // A note the admin actually typed is passed through unchanged.
    const noteText = note.trim() || `added on ${fmtApprovalDate()} by ${me?.email || "an admin"}`;
    await invite(email, noteText, isAdmin);
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
    setCsvResult({ added: res.added, adminsGranted: res.admins_granted, report,
                   mailConfigured: res.mail_configured });
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
    await reloadThenRestoreFocus({ kind: "el", elRef: csvResultRef });
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
    // Audit note stored on the allowlisted user: who approved the request, and
    // when. me is the signed-in admin doing the approving.
    const note = `approved on ${fmtApprovalDate()} by ${me?.email || "an admin"}`;
    confirm({
      variant: "neutral",
      title: `Approve access for ${addr}?`,
      body: "This adds them to the allowlist and emails them an approval notice. "
        + "They request their own sign-in link from the sign-in page when ready.",
      confirmLabel: "Approve access",
      onConfirm: async () => {
        const res = await api.addAllow(addr, note, false);
        if (!res?.ok) throw new Error(JSON.stringify({ detail: `Couldn't add ${addr}.` }));
        outcome = res;
      },
      errorToast: `Couldn't approve ${addr}.`,
      onSuccess: async () => {
        // Delivery-aware toast (emailed / already on the list / mail failed /
        // logged to console) — see INVITE_FLASH. The Approve button unmounted
        // with its pending row; hand focus to the pending table's search box.
        announce(inviteFlash(addr, outcome), inviteKind(outcome));
        await reloadThenRestoreFocus({ kind: "tableSearch", tableRef: pendingTableRef });
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
        await reloadThenRestoreFocus({ kind: "tableSearch", tableRef: pendingTableRef });
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
        await reloadThenRestoreFocus({ kind: "tableSearch", tableRef: blockedTableRef });
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
    // The row PERSISTS (only its shield swaps Promote<->Demote admin), so return
    // focus to that same row's action button AFTER the reload commits — instead
    // of <body> where the briefly-disabled button dropped it. Sequenced after
    // load() per the focus-restore-vs-reload race rule. (Toasts never take focus,
    // so there's no notice to race here anymore.)
    await reloadThenRestoreFocus({ kind: "rowAction", tableRef: usersTableRef, key: r.email });
  }

  // Destructive: a danger confirmation modal (naming the email), then the
  // app-styled result toast. The modal owns the in-flight/error state, so no
  // setBusyEmail here (the background is inert while it processes). After load()
  // refetches, the derived viewUsers() clamp keeps the admin on their page (or
  // drops to the previous one if this emptied the last page). Self-removal never
  // reaches here — the current admin's row shows no actions (backend also 400s it).
  function removeUser(r) {
    // You must demote an admin before removing them. The trash button is
    // aria-disabled (not `disabled`, so it stays hoverable to show why; see
    // renderActions), which does NOT block the click, so this early-return makes
    // it a safe no-op against accidental clicks. The backend enforces the same
    // rule authoritatively (DELETE /allowlist 400s a still-admin user) -- this
    // client guard is defense in depth so the modal never even opens.
    if (r.is_admin) return;
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
        await reloadThenRestoreFocus({ kind: "tableSearch", tableRef: usersTableRef });
      },
    });
  }

  // --- Bulk row-selection ------------------------------------------------------
  // The current admin's own row is never selectable for a bulk action — same
  // invariant as the single-row actions above (renderActions renders none for
  // self), enforced here too so it's excluded from "select all matching" and
  // the page tri-state checkbox as well, not just hidden from per-row actions.
  const userRowSelectable = useCallback(
    (r) => (me && r.email === me.email
      ? { ok: false, reason: "You cannot select your own account for bulk actions." }
      : { ok: true }),
    [me],
  );
  const userRowSelectLabel = (r) => `Select user ${r.email}`;
  const pendingRowSelectLabel = (r) => `Select access request from ${r.email}`;
  const blockedRowSelectLabel = (r) => `Select blocked user ${r.canon_email}`;

  // A table's search box changed: clear ITS OWN selection (never a sibling
  // table's — the three are independent) and toast why, but only if there was
  // something to clear -- an empty-selection search keystroke early-returns
  // BEFORE calling sel.clear() (code review #6 / L2), so it neither allocates
  // a new Set nor re-renders Allowlist on every keystroke when there was
  // nothing to clear anyway. This does NOT go through reloadThenRestoreFocus
  // — clearing a selection moves no focus.
  function onTableSearchChange(sel, q) {
    void q; // the new query itself needs no further handling here
    const hadSelection = sel.mode === "all" || sel.selectedIds.size > 0;
    if (!hadSelection) return;
    sel.clear();
    announce("Selection cleared because the search changed.");
  }

  // Shared bulk-confirm-then-act flow for all six actions below: builds the
  // confirm modal from bulkConfirmSummary/BULK_ACTION_LABEL, calls the bulk
  // API on confirm (throwing keeps the modal open for retry), and on success
  // toasts the outcome, then KEEPS THE WHOLE SELECTION (retainedSelectionAfterBulk:
  // promote/demote leave every selected row checked; the removing actions drop
  // only the ids the server processed away and keep skipped/failed rows checked)
  // BEFORE reloading, then reloads (refreshing every table — approve/reject also
  // refresh the OTHER affected table this way, with no extra code) and restores
  // focus to the acting table's search box.
  function runBulkConfirm({ sel, action, idField, selectedRows, eligibleRows, skippedRows, apiCall, focusRef }) {
    let result = null;
    confirm({
      variant: BULK_VARIANT[action],
      title: BULK_TITLE[action](eligibleRows.length),
      body: bulkConfirmSummary(action, {
        selected: selectedRows.length, eligible: eligibleRows.length, skipped: skippedRows.length,
      }),
      confirmLabel: BULK_ACTION_LABEL[action](eligibleRows.length),
      onConfirm: async () => {
        const ids = eligibleRows.map((r) => r[idField]);
        const res = await apiCall(ids);
        if (!res?.ok) throw new Error(JSON.stringify({ detail: "That didn't work. Please try again." }));
        result = res;
      },
      onSuccess: async () => {
        const { text, kind } = bulkResultToast(action, result);
        announce(text, kind);
        // Keep the whole selection (rows still in the table stay checked).
        // Synchronous, BEFORE the reload below.
        const selectedIds = selectedRows.map((r) => r[idField]);
        sel.selectExplicit(retainedSelectionAfterBulk(action, selectedIds, result, idField));
        await reloadThenRestoreFocus({ kind: "tableSearch", tableRef: focusRef });
      },
    });
  }

  // Build one table's BulkBar action descriptors from the rows the admin
  // actually has effectively selected right now (`sel.effectiveIds`), against
  // the table's OWN partitionEligibility rule per action.
  function userBulkActions(filteredEligibleRows) {
    const idSet = new Set(filteredEligibleRows.map((r) => r.email));
    const effIds = usersSel.effectiveIds(idSet);
    const selectedRows = filteredEligibleRows.filter((r) => effIds.has(r.email));
    return ["promote", "demote", "delete"].map((action) => {
      const { eligible, skipped } = partitionEligibility(selectedRows, action);
      return {
        key: action,
        label: BULK_TOOLBAR_LABEL[action],
        icon: BULK_ICON[action],
        variant: BULK_VARIANT[action],
        disabled: eligible.length === 0,
        title: BULK_DISABLED_REASON[action],
        onClick: () => runBulkConfirm({
          sel: usersSel, action, idField: "email",
          selectedRows, eligibleRows: eligible, skippedRows: skipped,
          apiCall: (emails) => api.bulkAllowlistAction(action, emails),
          focusRef: usersTableRef,
        }),
      };
    });
  }

  function pendingBulkActions(filteredEligibleRows) {
    const idSet = new Set(filteredEligibleRows.map((r) => r.id));
    const effIds = pendingSel.effectiveIds(idSet);
    const selectedRows = filteredEligibleRows.filter((r) => effIds.has(r.id));
    return ["approve", "reject"].map((action) => {
      const { eligible, skipped } = partitionEligibility(selectedRows, action);
      return {
        key: action,
        label: BULK_TOOLBAR_LABEL[action],
        icon: BULK_ICON[action],
        variant: BULK_VARIANT[action],
        disabled: eligible.length === 0,
        title: BULK_DISABLED_REASON[action],
        onClick: () => runBulkConfirm({
          sel: pendingSel, action, idField: "id",
          selectedRows, eligibleRows: eligible, skippedRows: skipped,
          apiCall: (ids) => api.bulkAccessRequests(action, ids),
          focusRef: pendingTableRef,
        }),
      };
    });
  }

  function blockedBulkActions(filteredEligibleRows) {
    const idSet = new Set(filteredEligibleRows.map((r) => r.id));
    const effIds = blockedSel.effectiveIds(idSet);
    const selectedRows = filteredEligibleRows.filter((r) => effIds.has(r.id));
    const { eligible, skipped } = partitionEligibility(selectedRows, "unblock");
    return [{
      key: "unblock",
      label: BULK_TOOLBAR_LABEL.unblock,
      icon: BULK_ICON.unblock,
      variant: BULK_VARIANT.unblock,
      disabled: eligible.length === 0,
      title: BULK_DISABLED_REASON.unblock,
      onClick: () => runBulkConfirm({
        sel: blockedSel, action: "unblock", idField: "id",
        selectedRows, eligibleRows: eligible, skippedRows: skipped,
        apiCall: (ids) => api.bulkClearDenials(ids),
        focusRef: blockedTableRef,
      }),
    }];
  }

  // --- Sub-tab navigation ------------------------------------------------------
  // A tab click / arrow key routes to /admin/users/<key>; the URL's :sub is the
  // single source of truth for which panel shows (so Back/Forward + deep links
  // just work). navigate() pushes, so each tab switch is its own history entry.
  const goSub = (key) => navigate(`/admin/users/${key}`);
  // Automatic activation: an arrow/Home/End moves selection immediately and
  // carries focus to the newly-active tab (its node persists across the
  // re-render; the rAF lets the new tabIndex settle first).
  function onTabKeyDown(e) {
    const action = { ArrowLeft: "left", ArrowRight: "right", Home: "home", End: "end" }[e.key];
    if (!action) return;
    e.preventDefault();
    const nextKey = subTabKeyForArrow(sub, action);
    goSub(nextKey);
    requestAnimationFrame(() => tabRefs.current[nextKey]?.focus());
  }
  // Per-tab record totals for the count badges — ALL records in each category,
  // never the DataTable's filtered view. Blocked is null on a load failure so
  // the badge is suppressed rather than falsely reading "0 blocked".
  const SUBTAB_COUNT = {
    current: rows.length,
    pending: reqs.length,
    blocked: deniedError ? null : denied.length,
  };

  return (
    <div className="panel">
      <h2>Users</h2>
      <p className="usertabs-intro muted">
        Manage who can sign in, review pending access requests, and see who is blocked.
      </p>
      {/* The three user tables are TABS, not a stacked page: only the active
          panel shows; inactive panels are `hidden`, so each DataTable's own
          search/sort/page state — and each table's lifted selection — survives a
          tab switch (resetting only when the admin leaves the Users section).
          Each tab's count reflects ALL records in that category (never the
          filtered view); Pending gets a restrained accent badge ONLY while
          requests await review — never an error tone. */}
      <div className="usertabs" role="tablist" aria-label="User management"
           onKeyDown={onTabKeyDown}>
        {USER_SUBTABS.map(({ key, label }) => {
          const count = SUBTAB_COUNT[key];
          const active = sub === key;
          const tone = key === "pending" ? pendingBadgeTone(reqs.length) : "idle";
          return (
            <button key={key} type="button" role="tab" id={`usertab-${key}`}
                    ref={(el) => { tabRefs.current[key] = el; }}
                    aria-controls={`userpanel-${key}`} aria-selected={active}
                    tabIndex={active ? 0 : -1}
                    className={"usertab" + (active ? " on" : "")}
                    onClick={() => goSub(key)}>
              <span className="usertab-label">{label}</span>
              {count != null && <span className={`usertab-badge ${tone}`}>{count}</span>}
            </button>
          );
        })}
      </div>
      {/* Announce the pending workload + its changes to a screen reader (the
          accent badge alone is color/positional). */}
      <span className="sr-only" aria-live="polite">
        {reqs.length > 0
          ? `${reqs.length} access request${reqs.length === 1 ? "" : "s"} awaiting review`
          : ""}
      </span>

      {/* ---- Current users ---- */}
      <div role="tabpanel" id="userpanel-current" aria-labelledby="usertab-current"
           className="usertab-panel" hidden={sub !== "current"}>
      <form className="row" onSubmit={add}>
        <label htmlFor="allow-email" className="sr-only">Email</label>
        <input id="allow-email" ref={addEmailRef} type="email" placeholder="email" required value={email}
               onChange={(e) => setEmail(e.target.value)} />
        <label htmlFor="allow-note" className="sr-only">Note</label>
        <input id="allow-note" placeholder="note (optional)" value={note}
               onChange={(e) => setNote(e.target.value)} />
        <label className="switch">
          <input type="checkbox" role="switch" checked={isAdmin}
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
                {csvResult.added > 0 && csvResult.mailConfigured
                  ? " Each was emailed an approval notice; they request a sign-in link when ready."
                  : ""}
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
        selectable
        selectionId={(r) => r.email}
        selectionMode={usersSel.mode}
        selectedIds={usersSel.selectedIds}
        rowSelectable={userRowSelectable}
        rowSelectLabel={userRowSelectLabel}
        onToggleRow={(r, checked) => usersSel.toggleRow(r.email, checked)}
        onTogglePage={(pageRows, checked) =>
          usersSel.togglePage(pageRows.map((r) => r.email), checked)}
        onSearchChange={(q) => onTableSearchChange(usersSel, q)}
        renderSelectionBar={({ pageEligibleRows, filteredEligibleRows }) => (
          <BulkBar
            nouns={USER_CONFIG.nouns}
            mode={usersSel.mode}
            count={usersSel.count(new Set(filteredEligibleRows.map((r) => r.email)))}
            totalEligible={filteredEligibleRows.length}
            pageEligibleCount={pageEligibleRows.length}
            pageSelectedCount={usersSel.count(new Set(pageEligibleRows.map((r) => r.email)))}
            onSelectAllMatching={usersSel.selectAllMatching}
            onClear={usersSel.clear}
            onFocusFallback={() => usersTableRef.current?.focusSearch()}
            actions={userBulkActions(filteredEligibleRows)}
          />
        )}
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
                <button type="button" className="icon-btn tip" data-tip="Demote admin"
                        aria-label="Demote admin" disabled={busy} onClick={() => toggleAdmin(r)}>
                  <IconShieldMinus />
                </button>
              ) : (
                <button type="button" className="icon-btn tip" data-tip="Promote admin"
                        aria-label="Promote admin" disabled={busy} onClick={() => toggleAdmin(r)}>
                  <IconShieldPlus />
                </button>
              )}
              {/* An admin can't be removed while they hold admin -- demote first.
                  aria-disabled (not `disabled`) keeps the button hoverable/
                  focusable so the tooltip explains WHY; removeUser early-returns
                  on an admin so the click is a safe no-op. */}
              <button type="button" className="icon-btn danger tip"
                      data-tip={r.is_admin ? "Can't remove an admin — demote first" : "Remove user"}
                      aria-label={r.is_admin ? "Can't remove an admin — demote first" : "Remove user"}
                      aria-disabled={r.is_admin ? "true" : undefined}
                      disabled={busy} onClick={() => removeUser(r)}>
                <IconTrash />
              </button>
            </>
          );
        }}
      />
      </div>

      {/* ---- Pending requests ---- */}
      <div role="tabpanel" id="userpanel-pending" aria-labelledby="usertab-pending"
           className="usertab-panel" hidden={sub !== "pending"}>
        <DataTable
          ref={pendingTableRef}
          rows={reqs}
          rowKey={(r) => r.id}
          config={PENDING_CONFIG}
          ariaLabel="Pending access requests"
          searchPlaceholder="Search by email"
          searchLabel="Search pending requests by email"
          sizeLabel="Requests per page"
          emptyNoData="No access requests are awaiting review."
          emptyNoMatch="No pending requests match your search."
          initialSort={{ key: "requested", dir: "desc" }}
          sortLabels={{ email: "email", requested: "requested" }}
          selectable
          selectionId={(r) => r.id}
          selectionMode={pendingSel.mode}
          selectedIds={pendingSel.selectedIds}
          rowSelectLabel={pendingRowSelectLabel}
          onToggleRow={(r, checked) => pendingSel.toggleRow(r.id, checked)}
          onTogglePage={(pageRows, checked) =>
            pendingSel.togglePage(pageRows.map((r) => r.id), checked)}
          onSearchChange={(q) => onTableSearchChange(pendingSel, q)}
          renderSelectionBar={({ pageEligibleRows, filteredEligibleRows }) => (
            <BulkBar
              nouns={PENDING_CONFIG.nouns}
              mode={pendingSel.mode}
              count={pendingSel.count(new Set(filteredEligibleRows.map((r) => r.id)))}
              totalEligible={filteredEligibleRows.length}
              pageEligibleCount={pageEligibleRows.length}
              pageSelectedCount={pendingSel.count(new Set(pageEligibleRows.map((r) => r.id)))}
              onSelectAllMatching={pendingSel.selectAllMatching}
              onClear={pendingSel.clear}
              onFocusFallback={() => pendingTableRef.current?.focusSearch()}
              actions={pendingBulkActions(filteredEligibleRows)}
            />
          )}
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
      </div>

      {/* ---- Blocked users ---- */}
      {/* Always a tab now (so its count + empty-state show even when nobody is
          blocked); a load failure (SEC #3) still renders a visible error rather
          than looking identical to "nobody is blocked". */}
      <div role="tabpanel" id="userpanel-blocked" aria-labelledby="usertab-blocked"
           className="usertab-panel" hidden={sub !== "blocked"}>
          {deniedError ? (
            // Its own class (not a bare `.notice`): a persistent in-flow error,
            // distinct from transient toasts, and off `.notice` so it doesn't
            // collide with unscoped `.notice`/`.toast` locators elsewhere.
            <p className="denied-error" role="alert">{deniedError}</p>
          ) : (
            <>
              {denied.length > 0 && (
                <p className="denied-help">
                  Rejecting a request blocks that address from asking again. Allowing
                  a blocked user only lets them request access again — it grants no
                  access and sends no email.
                </p>
              )}
              <DataTable
                ref={blockedTableRef}
                rows={denied}
                rowKey={(r) => r.id}
                config={BLOCKED_CONFIG}
                ariaLabel="Blocked users"
                searchPlaceholder="Search by email"
                searchLabel="Search blocked users by email"
                sizeLabel="Blocked users per page"
                emptyNoData="No users are currently blocked."
                emptyNoMatch="No blocked users match your search."
                initialSort={{ key: "denied", dir: "desc" }}
                sortLabels={{ email: "email", requested: "requested", denied: "denied" }}
                selectable
                selectionId={(r) => r.id}
                selectionMode={blockedSel.mode}
                selectedIds={blockedSel.selectedIds}
                rowSelectLabel={blockedRowSelectLabel}
                onToggleRow={(r, checked) => blockedSel.toggleRow(r.id, checked)}
                onTogglePage={(pageRows, checked) =>
                  blockedSel.togglePage(pageRows.map((r) => r.id), checked)}
                onSearchChange={(q) => onTableSearchChange(blockedSel, q)}
                renderSelectionBar={({ pageEligibleRows, filteredEligibleRows }) => (
                  <BulkBar
                    nouns={BLOCKED_CONFIG.nouns}
                    mode={blockedSel.mode}
                    count={blockedSel.count(new Set(filteredEligibleRows.map((r) => r.id)))}
                    totalEligible={filteredEligibleRows.length}
                    pageEligibleCount={pageEligibleRows.length}
                    pageSelectedCount={blockedSel.count(new Set(pageEligibleRows.map((r) => r.id)))}
                    onSelectAllMatching={blockedSel.selectAllMatching}
                    onClear={blockedSel.clear}
                    onFocusFallback={() => blockedTableRef.current?.focusSearch()}
                    actions={blockedBulkActions(filteredEligibleRows)}
                  />
                )}
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
      </div>
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
    const zone = shortZone();
    return s.length ? {
      type: "line", x: "t", y: [metric], yLabel: metric === "spend" ? "USD" : metric,
      xLabel: zone ? `Time (${zone})` : "Time",
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
          {u.cost_warning && (
            <p className="notice warn small" role="status">
              <strong>Spend isn’t being recorded.</strong> Your LLM provider isn’t
              reporting per-request cost and no fallback prices are set, so “Spend”
              reads $0 despite real activity. Set <code>LLM_INPUT_COST_PER_MTOK</code>{" "}
              and <code>LLM_OUTPUT_COST_PER_MTOK</code> in the server’s environment
              to estimate it (see the admin guide). This clears once cost data
              appears or those prices are set.
            </p>
          )}
          <p className="usage-privacy muted small">
            Every number here is computed locally, on this server, from its own
            database — and is shown only to signed-in admins. None of it is ever
            sent to a central server, telemetry service, or any third party; your
            usage data never leaves this machine. Hover or tap the ⓘ on any stat
            for what it means and which way is good.
          </p>
          {/* Grouped into three bands so an admin can tell operational
              volume/cost from efficiency from answer-quality at a glance. */}
          <div className="stat-band">
            <div className="field-label">Activity</div>
            <div className="stats">
              <Stat label="Queries" value={(t.queries || 0).toLocaleString()} info={STAT_INFO.queries} />
              <Stat label="Tokens" value={(t.tokens || 0).toLocaleString()} info={STAT_INFO.tokens} />
              <Stat label="Spend" value={money(t.spend)} info={STAT_INFO.spend} />
            </div>
          </div>
          <div className="stat-band">
            <div className="field-label">Efficiency</div>
            <div className="stats">
              <Stat label="Answer cache" value={t.cache_hits || 0} info={STAT_INFO.answerCache} />
              <Stat label="Schema cache" value={schemaCacheRate(t)} info={STAT_INFO.schemaCache} />
              <Stat label="Prompt cache" value={promptCacheRate(t)} info={STAT_INFO.promptCache} />
              <Stat label="Escalations" value={t.escalations || 0} info={STAT_INFO.escalations} />
            </div>
          </div>
          <div className="stat-band">
            <div className="field-label">Answer quality</div>
            <div className="stats">
              <Stat label={groundedFigureLabel(t)} value={groundedFigureRate(t)} info={STAT_INFO.groundedFigures} />
              <Stat label={groundedTableLabel(t)} value={groundedTableRate(t)} info={STAT_INFO.groundedCells} />
              <Stat label={leakLabel(t)} value={leakRate(t)} info={STAT_INFO.answerLeaks} />
              <Stat label="Failures" value={t.failures || 0} info={STAT_INFO.failures} />
              <Stat label={exhaustionLabel(t)} value={(t.exhausted_turns || 0).toLocaleString()} info={STAT_INFO.exhausted} />
            </div>
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
          <table className="grid" aria-label="Top users">
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

function Stat({ label, value, info }) {
  // Label BEFORE value in the DOM so a screen reader hears the name then the
  // number ("Queries … 1,234"), not the reverse; `.stat` is column-reverse so the
  // big value still sits visually on top.
  return (
    <div className="stat">
      <div className="l">
        <span>{label}</span>
        {info && (
          <HelpPopover label={`What “${info.name}” measures`} icon={IconInfo}
                       className="help-compact">
            <div className="help-body statinfo">
              <p>{info.what}</p>
              {info.note && <p className="statinfo-note">{info.note}</p>}
              <p className={"statinfo-dir dir-" + info.direction}>
                {directionHint(info.direction)}
              </p>
            </div>
          </HelpPopover>
        )}
      </div>
      <div className="v">{value}</div>
    </div>
  );
}

function ruleName(s) {
  return s.headline || s.lesson || s.notes || s.question || "untitled lesson";
}

function Skills({ onAttentionChanged }) {
  const toast = useToast();
  const confirm = useConfirm();
  const refreshAttention = onAttentionChanged || (() => {});
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

  // Which "edit" button to focus once a save's reload has COMMITTED, as a fresh
  // `{ id }` object each time so the layout effect re-fires per save (even re-saving
  // the same id) without a set-state-in-effect (an error under this repo's lint).
  // The effect runs after the DOM commit, so the freshly-mounted button is focused
  // deterministically — never a bare rAF racing load()'s setRows (which remounts the
  // button under the just-focused node and drops focus to <body>).
  const [pendingEditFocus, setPendingEditFocus] = useState(null);
  useLayoutEffect(() => {
    if (!pendingEditFocus) return;
    editBtnRefs.current[pendingEditFocus.id]?.focus?.();
  }, [pendingEditFocus]);
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
      refreshAttention();  // verified count changed → update the Skills badge now
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
      // Restore focus only AFTER the list reload has committed. Requesting it via
      // pendingEditFocus (consumed by the layout effect post-commit) is
      // deterministic — a bare rAF here races the reload's setRows, which remounts
      // the edit button under the just-focused node and drops focus to <body>
      // (the focus-restore-vs-reload race; a single rAF isn't enough under load).
      await load();
      setPendingEditFocus({ id: s.id });
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
        .then(() => { focusHeading(); refreshAttention(); })
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
      onSuccess: async () => { await load(); focusHeading(); refreshAttention(); },
    });
  };

  return (
    <div className="panel">
      <h2 ref={headingRef} tabIndex={-1}>Learned lessons ({rows.length})</h2>
      <p className="muted small">
        Rules the assistant applies as guidance. The post-answer critic proposes a
        lesson when it catches a mistake, and a user’s corrective feedback on a
        follow-up turn proposes one too — each a short headline plus a longer
        description — and it starts <strong>unverified</strong> until you approve
        it here.
        {pending > 0 && ` ${pending} awaiting review.`}
      </p>
      {rows.length === 0 && (
        <p className="muted small">
          No lessons yet — they’ll appear here as the critic or a user’s
          corrective feedback proposes them.
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
                       onChange={(e) => { const v = e.target.value; setDraft((d) => ({ ...d, headline: v })); }} />
              </label>
              <label className="lesson-field">
                <span className="muted small">Description</span>
                <textarea rows={4} maxLength={4000} value={draft.lesson}
                          onChange={(e) => { const v = e.target.value; setDraft((d) => ({ ...d, lesson: v })); }} />
              </label>
              <label className="lesson-field">
                <span className="muted small">Example query</span>
                <textarea rows={6} maxLength={8000} className="mono" value={draft.canonical_sql}
                          onChange={(e) => { const v = e.target.value; setDraft((d) => ({ ...d, canonical_sql: v })); }} />
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
              <SqlBlock code={s.canonical_sql} />
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

function Logs({ onAttentionChanged }) {
  const [records, setRecords] = useState([]);
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
        <label className="switch">
          <input type="checkbox" role="switch" checked={auto}
            onChange={(e) => setAuto(e.target.checked)} />
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
