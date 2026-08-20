import React, { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { loadErrorMessage } from "../authcopy.js";
import { KEY_CONFIG, isRevoked, maskedKey } from "../apikeys.js";
import { bulkConfirmSummary, bulkResultToast, partitionEligibility,
         retainedSelectionAfterBulk } from "../selection.js";
import { useTableSelection } from "../useTableSelection.js";
import BulkBar from "../BulkBar.jsx";
import { fmtDateTime, fmtDay } from "./format.js";
import { IconTrash } from "../icons.jsx";
import DataTable from "../DataTable.jsx";
import KeyReveal from "../KeyReveal.jsx";
import { useConfirm } from "../ConfirmModal.jsx";
import { useToast } from "../Toast.jsx";

// Admin → Keys: every user's MCP API keys, and minting one on somebody's behalf.
//
// A <DataTable> here and a plain list on the user's own /keys page, deliberately:
// this list grows with the deployment and is the one an admin searches ("who has
// a live key?", "which row is ipeds_mcp_…9f2a from that log line?"), which is
// exactly what the table's search/sort/paging exist for.
//
// Revoked keys stay listed. The row is the only record of what a withdrawn key
// could reach, and an admin asking that question needs it to still be there.
export default function Keys() {
  const toast = useToast();
  const confirm = useConfirm();
  const [rows, setRows] = useState([]);
  const [err, setErr] = useState("");
  const [email, setEmail] = useState("");
  const [label, setLabel] = useState("");
  const [minting, setMinting] = useState(false);
  // The raw key, held only until the admin dismisses the reveal dialog.
  const [minted, setMinted] = useState(null);
  const tableRef = useRef(null);
  const headingRef = useRef(null);
  // Bulk row-selection for this one table. Revoke is its only bulk action:
  // minting needs a recipient per key, and a label belongs to the key's owner
  // (app/routers/keys.py), so neither has a batch form.
  const sel = useTableSelection();

  // A failed load must render a visible error, never an empty table — "nobody
  // has a key" is a dangerous thing to tell an admin auditing access when the
  // truth is that the request failed.
  const load = () => api.allKeys()
    .then((d) => { setRows(Array.isArray(d) ? d : []); setErr(""); })
    .catch((e) => setErr(loadErrorMessage("the API keys", e?.detail)));
  useEffect(() => { load(); }, []);

  function create(e) {
    e.preventDefault();
    if (minting) return;
    setMinting(true);
    api.createKeyFor(email.trim(), label.trim())
      .then((k) => {
        setEmail("");
        setLabel("");
        setMinted(k);
        load();
      })
      // The server's own sentence is the useful one here — it distinguishes "that
      // address has never signed in" from "that address is not on the allowlist",
      // and the fix differs.
      .catch((e2) => toast(e2?.detail || "Couldn't create that key.", "error"))
      .finally(() => setMinting(false));
  }

  function revoke(row) {
    confirm({
      variant: "danger",
      title: "Revoke this key?",
      body: `Any MCP client still using ${maskedKey(row)} stops working immediately. `
        + "This can't be undone — mint a new key instead.",
      details: [row.email, row.label].filter(Boolean).join(" · ") || undefined,
      confirmLabel: "Revoke key",
      busyLabel: "Revoking…",
      successToast: "Key revoked.",
      errorToast: "Couldn't revoke that key.",
      onConfirm: () => api.revokeAnyKey(row.id),
      // The row survives the revoke but loses its only action, so there is no
      // row action left to return focus to. The search box is the table's stable
      // landing spot (the same fallback BulkBar uses).
      // The heading, not the table's search box: `load()` was just kicked off,
      // and if it FAILS the panel swaps the DataTable for a role="alert", which
      // unmounts the element holding focus and drops a keyboard user to <body>.
      // The heading is outside that branch — the same reason src/Keys.jsx uses
      // its <h1>.
      onSuccess: () => { load(); headingRef.current?.focus?.(); },
    });
  }

  // The search changed: clear this table's selection and say why, but only when
  // there was something to clear — an empty-selection keystroke must not
  // allocate a new Set and re-render on every character. Same rule as the
  // Allowlist tables.
  function onSearchChange() {
    if (sel.mode !== "all" && sel.selectedIds.size === 0) return;
    sel.clear();
    toast("Selection cleared because the search changed.");
  }

  // Bulk revoke: confirm with a count the admin can check, then act. Eligibility
  // is computed here for the DIALOG's wording only — the server recomputes it
  // per key against live state, so a key revoked from another tab lands in
  // `skipped` rather than being counted as revoked.
  function bulkRevoke(selectedRows, eligible, skipped) {
    let result = null;
    confirm({
      variant: "danger",
      title: eligible.length === 1 ? "Revoke this key?" : `Revoke ${eligible.length} keys?`,
      body: bulkConfirmSummary("revoke", {
        selected: selectedRows.length, eligible: eligible.length, skipped: skipped.length,
      }),
      confirmLabel: eligible.length === 1 ? "Revoke key" : `Revoke ${eligible.length} keys`,
      busyLabel: "Revoking…",
      errorToast: "Couldn't revoke those keys.",
      onConfirm: async () => {
        const res = await api.bulkKeyAction("revoke", eligible.map((r) => r.id));
        if (!res?.ok) throw new Error(JSON.stringify({ detail: "That didn't work. Please try again." }));
        result = res;
      },
      onSuccess: async () => {
        const { text, kind } = bulkResultToast("revoke", result);
        toast(text, kind);
        // Revoked keys STAY in this table, so the whole selection is retained —
        // the admin can see what happened to every row they acted on.
        sel.selectExplicit(retainedSelectionAfterBulk(
          "revoke", selectedRows.map((r) => r.id), result, "id"));
        await load();
        // The heading, not the search box: a failed reload swaps the table for a
        // role="alert" and would unmount whatever held focus. Same reason as the
        // single-row revoke above.
        headingRef.current?.focus?.();
      },
    });
  }

  return (
    <div className="panel">
      <h2 ref={headingRef} tabIndex={-1}>API keys</h2>
      <p className="muted">
        Keys let an MCP client reach IPEDS Oracle as the person they belong to.
        Minting one requires an allowlisted address that has signed in at least
        once — a key is a credential for an existing account, not a way to create
        one.
      </p>

      <form className="row" onSubmit={create}>
        <label htmlFor="adminkey-email" className="sr-only">Email to mint a key for</label>
        <input id="adminkey-email" type="email" placeholder="email" required
               value={email} onChange={(e) => setEmail(e.target.value)} />
        <label htmlFor="adminkey-label" className="sr-only">Label for the new key</label>
        <input id="adminkey-label" placeholder="label (optional)" maxLength={80}
               value={label} onChange={(e) => setLabel(e.target.value)} />
        {/* aria-disabled, not disabled — see the same button in src/Keys.jsx:
            disabling the focused control blurs it to <body>, and the reveal
            dialog then has nowhere to return focus to. */}
        <button type="submit" aria-disabled={minting || undefined}
                aria-busy={minting || undefined}>
          {minting ? "Creating…" : "Create key"}
        </button>
      </form>
      {/* See the same line in src/Keys.jsx: aria-busy is not a live message. */}
      <span className="sr-only" aria-live="polite">
        {minting ? "Creating key…" : ""}
      </span>

      {err ? (
        <p className="denied-error" role="alert">{err}</p>
      ) : (
        <DataTable
          ref={tableRef}
          rows={rows}
          rowKey={(r) => r.id}
          config={KEY_CONFIG}
          tableClass="grid data keys"
          ariaLabel="API keys"
          searchId="key-search"
          searchPlaceholder="Search email, label or key"
          searchLabel="Search email, label or key"
          sizeLabel="Keys per page"
          emptyNoData="No API keys yet."
          emptyNoMatch="No keys match your search."
          initialSort={{ key: "created_at", dir: "desc" }}
          selectable
          selectionId={(r) => r.id}
          selectionMode={sel.mode}
          selectedIds={sel.selectedIds}
          rowSelectLabel={(r) => `Select key ${maskedKey(r)} for ${r.email}`}
          onToggleRow={(r, checked) => sel.toggleRow(r.id, checked)}
          onTogglePage={(pageRows, checked) =>
            sel.togglePage(pageRows.map((r) => r.id), checked)}
          onSearchChange={onSearchChange}
          renderSelectionBar={({ pageEligibleRows, filteredEligibleRows }) => {
            const effIds = sel.effectiveIds(new Set(filteredEligibleRows.map((r) => r.id)));
            const selectedRows = filteredEligibleRows.filter((r) => effIds.has(r.id));
            const { eligible, skipped } = partitionEligibility(selectedRows, "revoke");
            return (
              <BulkBar
                nouns={KEY_CONFIG.nouns}
                mode={sel.mode}
                count={sel.count(new Set(filteredEligibleRows.map((r) => r.id)))}
                totalEligible={filteredEligibleRows.length}
                pageEligibleCount={pageEligibleRows.length}
                pageSelectedCount={sel.count(new Set(pageEligibleRows.map((r) => r.id)))}
                onSelectAllMatching={sel.selectAllMatching}
                onClear={sel.clear}
                onFocusFallback={() => tableRef.current?.focusSearch()}
                actions={[{
                  key: "revoke",
                  label: "Revoke",
                  icon: IconTrash,
                  variant: "danger",
                  // Every selected key is already revoked — there is nothing to
                  // do, and the reason says so rather than leaving a dead button.
                  disabled: eligible.length === 0,
                  title: "Selected keys are already revoked.",
                  onClick: () => bulkRevoke(selectedRows, eligible, skipped),
                }]}
              />
            );
          }}
          sortLabels={{ email: "owner", label: "label", created_at: "created",
                        last_used_at: "last used", status: "status" }}
          columns={[
            { key: "email", label: "Owner", sortable: true, colClass: "col-email",
              cellClass: "cell-trunc", cellTitle: (r) => r.email },
            { key: "label", label: "Label", sortable: true, colClass: "col-note",
              cellClass: "cell-trunc", cellTitle: (r) => r.label || undefined,
              render: (r) => r.label || null },
            // The masked value, so a key seen in a log or a config file can be
            // traced back to its owner. Never more than the four characters the
            // server returns — see apikeys.js.
            { key: "last4", label: "Key", colClass: "col-key",
              render: (r) => <code className="keycode">{maskedKey(r)}</code> },
            // Date only, with the full stamp in the cell's title. Six columns
            // do not fit two 210px date+time cells inside the 1000px panel, and
            // for a key the audit question is which DAY it was last presented,
            // not which minute — see .col-day in styles.css.
            { key: "created_at", label: "Created", sortable: true, colClass: "col-day",
              cellClass: "cell-trunc",
              // `created_by` is in the payload and had nowhere to be seen. Six
              // columns already fill the panel, so it rides in the title of the
              // cell it describes: self-issued vs admin-issued is a real
              // distinction when reviewing who has access.
              cellTitle: (r) => fmtDateTime(r.created_at)
                + (r.created_by ? ` by ${r.created_by}` : ""),
              render: (r) => fmtDay(r.created_at) },
            { key: "last_used_at", label: "Last used", sortable: true, colClass: "col-day",
              cellClass: "cell-trunc", cellTitle: (r) => fmtDateTime(r.last_used_at),
              render: (r) => fmtDay(r.last_used_at) },
            // An active key used to render an EMPTY cell under a column headed
            // "Status" — a screen reader says "Status, blank", which is what a
            // failed load looks like, and sorting by it reordered rows with
            // nothing visible changing for half of them. "Revoked" now reads as
            // a contrast rather than as an absence, and the title answers the
            // question that always follows: for how long was it live?
            { key: "status", label: "Status", sortable: true, colClass: "col-status",
              cellTitle: (r) => (isRevoked(r)
                ? `Revoked ${fmtDateTime(r.revoked_at)}` : undefined),
              render: (r) => (isRevoked(r)
                ? <span className="keyrow-state">Revoked</span>
                : <span className="keyrow-state active">Active</span>) },
          ]}
          renderActions={(r) => (isRevoked(r) ? null : (
            <button type="button" className="icon-btn danger tip" data-tip="Revoke key"
                    aria-label={`Revoke key ${maskedKey(r)} for ${r.email}`}
                    onClick={() => revoke(r)}>
              <IconTrash />
            </button>
          ))}
        />
      )}

      {minted && (
        <KeyReveal secret={minted.key} label={minted.label} email={minted.email}
                   onClose={() => setMinted(null)} />
      )}
    </div>
  );
}
