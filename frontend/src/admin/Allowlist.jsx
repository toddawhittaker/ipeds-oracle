// Admin → Users: the three-sub-tab access-management section (Current users /
// Pending requests / Blocked users). Split out of Admin.jsx unchanged.
import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "../api.js";
import { USER_CONFIG } from "../userlist.js";
import { PENDING_CONFIG, BLOCKED_CONFIG } from "../accesstables.js";
import DataTable from "../DataTable.jsx";
import { buildImportPlan } from "../csvimport.js";
import { IconShieldPlus, IconShieldMinus, IconTrash, IconUpload, IconCheck, IconClose, IconUnlock } from "../icons.jsx";
import { useToast } from "../Toast.jsx";
import { useConfirm } from "../ConfirmModal.jsx";
import { useTableSelection } from "../useTableSelection.js";
import BulkBar from "../BulkBar.jsx";
import { bulkConfirmSummary, bulkResultToast, partitionEligibility, retainedSelectionAfterBulk } from "../selection.js";
import { USER_SUBTABS, rememberSubTab, subTabKeyForArrow, pendingBadgeTone } from "../usertabs.js";
import HelpPopover from "../HelpPopover.jsx";
import { canonEmailForDisplay, fmtApprovalDate, fmtDateTime } from "./format.js";
import { loadErrorMessage } from "../authcopy.js";
import { loadNotice } from "../loadstate.js";

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


const ALLOWLIST_POLL_MS = 15000;
const ALLOWLIST_RELOAD_COOLDOWN_MS = 1500;

export default function Allowlist({ me, sub, onAttentionChanged }) {
  const refreshAttention = onAttentionChanged || (() => {});
  const navigate = useNavigate();
  // Roving-focus refs for the sub-tab buttons: keyboard nav focuses the target.
  const tabRefs = useRef({});
  const [rows, setRows] = useState([]);
  const [rowsError, setRowsError] = useState("");
  const [reqs, setReqs] = useState([]);
  const [reqsError, setReqsError] = useState("");
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
    //
    // The two-argument `.then(onOk, onErr)` form -- not `.catch` -- is
    // deliberate: `.catch` also swallows a throw from the SUCCESS handler
    // (e.g. a bug in a later `.then`), which would silently look like the
    // request itself failed. It's also what keeps this promise chain from
    // ever REJECTING for an ordinary load failure, which is the actual fix:
    // a bare `.then(setRows)` with no error branch used to reject `loaded`
    // on a failed fetch, and the plain `useEffect(() => { load(); }, [])`
    // call below never awaits or catches it -- an unhandled promise
    // rejection on every failed load, on top of leaving stale/no rows on
    // screen with nothing said (see error-visibility.spec.js).
    const loaded = api.allowlist().then(
      (d) => { setRows(d); setRowsError(""); },
      (err) => setRowsError(loadErrorMessage("the allowlist", err?.detail)),
    );
    api.accessRequests().then(
      (d) => { setReqs(d); setReqsError(""); },
      (err) => setReqsError(loadErrorMessage("access requests", err?.detail)),
    );
    // Unlike the two loaders above -- where an empty rendered result on
    // failure is indistinguishable from "genuinely nothing yet", which is
    // fine -- a silently-swallowed failure HERE (SEC #3, round-4 security
    // review) would be byte-identical to "nobody is blocked", the one
    // thing this section's entire job is to be able to say with
    // confidence. Render a real error state instead (see the JSX below).
    // Report only what the response actually says (a `detail` field, when
    // the server sent one) rather than inferring a cause -- same principle,
    // and the same server-detail-first pattern (now err.detail), as
    // toggleAdmin's catch further down: guessing at a cause from a proxy
    // value instead of asking the server directly is exactly the class of
    // bug PR #57/#60 fixed for the invite-email flash.
    api.deniedRequests().then((d) => { setDenied(d); setDeniedError(""); })
      .catch((err) => {
        const detail = err?.detail || "";
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
      msg = err?.detail || msg;   // ApiError carries the server's own wording
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
  // The tab a keypress must move FROM, tracked synchronously.
  //
  // `sub` is the committed prop, and it LAGS a navigation: navigate() updates the
  // URL immediately, but react-router commits the re-render later (v8 routes
  // through startTransition — the same deferral behind [[react-router7-
  // streaming-urlflip]]). So a second keypress inside that window computed from
  // the tab BEFORE the first one, and two quick presses from Current landed on
  // Pending instead of Blocked — exactly what a keyboard user gets holding an
  // arrow key. Reproduces 100% of the time with back-to-back presses; the
  // existing spec only passed because it waited for the URL between keys.
  //
  // goSub owns the update so the click path keeps the same invariant (the effect
  // alone would leave a window between navigate and commit). That path is NOT
  // separately tested: a Playwright click carries enough latency that `sub`
  // commits first, so the test passed with and without the fix — a test that
  // cannot fail is noise, so it was dropped rather than shipped.
  const activeKeyRef = useRef(sub);
  useEffect(() => { activeKeyRef.current = sub; }, [sub]);
  const goSub = (key) => {
    activeKeyRef.current = key;
    navigate(`/admin/users/${key}`);
  };
  // Automatic activation: an arrow/Home/End moves selection immediately and
  // carries focus to the newly-active tab (its node persists across the
  // re-render; the rAF lets the new tabIndex settle first).
  function onTabKeyDown(e) {
    const action = { ArrowLeft: "left", ArrowRight: "right", Home: "home", End: "end" }[e.key];
    if (!action) return;
    e.preventDefault();
    const nextKey = subTabKeyForArrow(activeKeyRef.current, action);
    goSub(nextKey);
    requestAnimationFrame(() => tabRefs.current[nextKey]?.focus());
  }
  // Per-tab record totals for the count badges — ALL records in each category,
  // never the DataTable's filtered view. Every tab is null on a load failure
  // (not just Blocked) so the badge is suppressed rather than falsely reading
  // "0 users"/"0 pending" -- the same lie in badge form as an empty table.
  const SUBTAB_COUNT = {
    current: rowsError ? null : rows.length,
    pending: reqsError ? null : reqs.length,
    blocked: deniedError ? null : denied.length,
  };
  // The two-sided load notice for each table (see loadstate.js): a first
  // load with no rows yet REPLACES that tab's content with the error; a
  // refresh failure on an already-populated tab keeps the rows and adds a
  // stale-data notice above them instead.
  const rowsNotice = loadNotice({ error: rowsError, hasRows: rows.length > 0 });
  const reqsNotice = loadNotice({ error: reqsError, hasRows: reqs.length > 0 });

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
      {/* A first load with no rows yet REPLACES the panel with the error (an
          empty table here would read as "nobody can sign in" -- the
          dangerous lie); a refresh failure on an already-populated table
          keeps everything below and just adds a stale-data notice. */}
      {rowsNotice && (
        <p className={rowsNotice.replace ? "denied-error" : "notice warn small"} role="alert">
          {rowsNotice.text}
        </p>
      )}
      {!rowsNotice?.replace && (
      <>
      <form className="row" onSubmit={add}>
        <label htmlFor="allow-email" className="sr-only">Email</label>
        <input id="allow-email" ref={addEmailRef} type="email" placeholder="email" required value={email}
               onChange={(e) => setEmail(e.target.value)} />
        <label htmlFor="allow-note" className="sr-only">Note</label>
        <input id="allow-note" placeholder="note (optional)" value={note}
               onChange={(e) => setNote(e.target.value)} />
        <label className="switch">
          <input type="checkbox" role="switch" checked={isAdmin}
                 onChange={(e) => setIsAdmin(e.target.checked)} /> Admin
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
                  {/* .help-body pre scrolls; nothing inside is focusable, so a
                      keyboard user could not scroll it (WCAG 2.1.1, Level A). */}
                  <pre tabIndex={0} role="region"
                       aria-label="CSV format example">{`email,note,admin
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
        sortLabels={{ email: "email", note: "note", admin: "admin status", last_active: "last active" }}
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
          // Last ACTIVITY, not last sign-in: the server derives it as the
          // latest of the sign-in stamp, the user's most recent conversation,
          // and their most recent question, so a colleague who signed in months
          // ago and has been asking questions since reads as current. Rendered
          // through the shared fmtDateTime (date + time, viewer locale, "—" on
          // null) — the date alone couldn't answer "are they using it today?".
          // cell-trunc (nowrap + ellipsis) is not cosmetic here: a timestamp is
          // one atomic value, and letting it wrap grows the row past its 49px
          // floor, which breaks the pixel-exact pagination height invariant on
          // whichever font happens to be wider. See .col-active in styles.css.
          { key: "last_active", label: "Last active", sortable: true, colClass: "col-active",
            cellClass: "cell-trunc", cellTitle: (r) => fmtDateTime(r.last_active),
            render: (r) => fmtDateTime(r.last_active) },
        ]}
        renderActions={(r) => {
          const isSelf = me && r.email === me.email;
          if (isSelf) return null;
          const busy = busyEmail === r.email;
          return (
            <>
              {r.is_admin ? (
                <button type="button" className="icon-btn tip" data-tip="Demote admin"
                        aria-label={`Demote admin: ${r.email}`} disabled={busy}
                        onClick={() => toggleAdmin(r)}>
                  <IconShieldMinus />
                </button>
              ) : (
                <button type="button" className="icon-btn tip" data-tip="Promote admin"
                        aria-label={`Promote admin: ${r.email}`} disabled={busy}
                        onClick={() => toggleAdmin(r)}>
                  <IconShieldPlus />
                </button>
              )}
              {/* An admin can't be removed while they hold admin -- demote first.
                  aria-disabled (not `disabled`) keeps the button hoverable/
                  focusable so the tooltip explains WHY; removeUser early-returns
                  on an admin so the click is a safe no-op.
                  The row's address is in the NAME, as Pending and Blocked
                  already do it. Without it a screen reader's element list shows
                  25 identical "Remove user" buttons on a destructive action. */}
              <button type="button" className="icon-btn danger tip"
                      data-tip={r.is_admin ? "Can't remove an admin — demote first" : "Remove user"}
                      aria-label={r.is_admin
                        ? `Can't remove an admin — demote first: ${r.email}`
                        : `Remove user: ${r.email}`}
                      aria-disabled={r.is_admin ? "true" : undefined}
                      disabled={busy} onClick={() => removeUser(r)}>
                <IconTrash />
              </button>
            </>
          );
        }}
      />
      </>
      )}
      </div>

      {/* ---- Pending requests ---- */}
      <div role="tabpanel" id="userpanel-pending" aria-labelledby="usertab-pending"
           className="usertab-panel" hidden={sub !== "pending"}>
        {/* Same replace-vs-stale rule as Current users, above. */}
        {reqsNotice && (
          <p className={reqsNotice.replace ? "denied-error" : "notice warn small"} role="alert">
            {reqsNotice.text}
          </p>
        )}
        {!reqsNotice?.replace && (
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
            // cell-trunc (nowrap + ellipsis) for the same reason as Current
            // users' "Last active": a timestamp is one atomic value, and letting
            // it wrap makes the row height depend on font metrics and locale.
            { key: "requested", label: "Requested", sortable: true, colClass: "col-when",
              cellClass: "cell-trunc", cellTitle: (r) => fmtDateTime(r.created_at),
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
                    cellClass: "cell-trunc", cellTitle: (r) => fmtDateTime(r.created_at),
                    render: (r) => fmtDateTime(r.created_at) },
                  { key: "denied", label: "Denied", sortable: true, colClass: "col-when",
                    cellClass: "cell-trunc",
                    // Only a real stamp gets a tooltip: titling the "—" placeholder
                    // would put a dash in a hover label that says nothing.
                    cellTitle: (r) => (r.denied_at ? fmtDateTime(r.denied_at) : undefined),
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

