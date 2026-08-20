import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { loadErrorMessage } from "./authcopy.js";
import { KEY_PREFIX, isRevoked, maskedKey, sortByNewest } from "./apikeys.js";
import { fmtDateTime } from "./admin/format.js";
import { IconTrash } from "./icons.jsx";
import KeyReveal from "./KeyReveal.jsx";
import { useConfirm } from "./ConfirmModal.jsx";
import { useToast } from "./Toast.jsx";

// A user's own MCP API keys, at /keys (reached from the account menu). Keys let
// an MCP client — Claude Code, the Messages API MCP connector — reach the same
// data and the same agent as this web app, authenticated with a bearer token
// instead of the session cookie the browser carries.
//
// A plain list, not a <DataTable>: one person's keys number in the handful, and
// a search box over three rows is furniture. The admin tab, which lists
// everybody's, does use the table (src/admin/Keys.jsx).
//
// fmtDateTime is imported from admin/format.js rather than copied: it is a pure,
// already vitest-pinned helper, and this screen renders the same "date + time in
// the viewer's locale, — when null" cell as the admin tables do. It lives under
// admin/ only because that is where it was first needed.
export default function Keys() {
  const toast = useToast();
  const confirm = useConfirm();
  const [rows, setRows] = useState([]);
  const [err, setErr] = useState("");
  const [label, setLabel] = useState("");
  const [minting, setMinting] = useState(false);
  // The raw key, held only between the mint response and the user dismissing the
  // reveal dialog. Never persisted anywhere — see KeyReveal.jsx.
  const [minted, setMinted] = useState(null);
  const headingRef = useRef(null);

  // A failed load must never render as "you have no keys": that reads as a
  // confirmed empty state, and the fix a user would reach for (mint another) is
  // the wrong one. Same rule as Skills.jsx and the Allowlist tables.
  const load = () => api.apiKeys()
    .then((d) => { setRows(Array.isArray(d) ? d : []); setErr(""); })
    .catch((e) => setErr(loadErrorMessage("your API keys", e?.detail)));
  useEffect(() => { load(); }, []);

  function create(e) {
    e.preventDefault();
    if (minting) return;
    setMinting(true);
    api.createApiKey(label.trim())
      .then((k) => {
        setLabel("");
        setMinted(k);
        load();
      })
      .catch((e2) => toast(e2?.detail || "Couldn't create that key.", "error"))
      .finally(() => setMinting(false));
  }

  function revoke(row) {
    confirm({
      variant: "danger",
      title: "Revoke this key?",
      body: `Any MCP client still using ${maskedKey(row)} stops working immediately. `
        + "This can't be undone — create a new key instead.",
      details: row.label || undefined,
      confirmLabel: "Revoke key",
      busyLabel: "Revoking…",
      successToast: "Key revoked.",
      errorToast: "Couldn't revoke that key.",
      onConfirm: () => api.revokeApiKey(row.id),
      // The revoked row STAYS in the list (it just loses its Revoke button), so
      // there is no removed-row hole to fall into — but the button that opened
      // the modal is gone, and ConfirmModal hands post-success focus to the
      // feature. The heading is the stable landing spot.
      onSuccess: () => { load(); headingRef.current?.focus?.(); },
    });
  }

  return (
    <main className="page thin-scroll">
      <div className="panel">
        <h1 ref={headingRef} tabIndex={-1}>API keys</h1>
        <p className="muted">
          An API key lets an MCP client — Claude Code, or anything else that speaks
          the Model Context Protocol — ask IPEDS Oracle the same questions this app
          does. It carries your access, so treat it like a password.
        </p>

        <form className="row" onSubmit={create}>
          <label htmlFor="key-label" className="sr-only">Label for the new key</label>
          <input id="key-label" placeholder="label (optional) — e.g. work laptop"
                 maxLength={80} value={label}
                 onChange={(e) => setLabel(e.target.value)} />
          {/* aria-disabled, NOT disabled: disabling the focused button blurs it
              to <body>, and the reveal dialog that opens a moment later captures
              whatever is focused as the control to return focus to on close — so
              a real `disabled` here strands the keyboard user at the top of the
              document when they dismiss it. create() early-returns while a mint
              is in flight, so the click is a safe no-op. */}
          <button type="submit" aria-disabled={minting || undefined}
                  aria-busy={minting || undefined}>
            {minting ? "Creating…" : "Create key"}
          </button>
        </form>

        {err ? (
          <p className="denied-error" role="alert">{err}</p>
        ) : rows.length === 0 ? (
          <p className="muted">You don&rsquo;t have any API keys yet.</p>
        ) : (
          <ul className="keylist">
            {sortByNewest(rows).map((k) => {
              const revoked = isRevoked(k);
              return (
                <li key={k.id} className={"keyrow" + (revoked ? " revoked" : "")}>
                  <div className="keyrow-main">
                    <span className="keyrow-label">{k.label || "Unlabelled key"}</span>
                    <code className="keyrow-id">{maskedKey(k)}</code>
                    {revoked && <span className="keyrow-state">Revoked</span>}
                  </div>
                  <div className="keyrow-meta muted small">
                    Created {fmtDateTime(k.created_at)}
                    {k.created_by ? ` by ${k.created_by}` : ""}
                    {" · Last used "}
                    {k.last_used_at ? fmtDateTime(k.last_used_at) : "never"}
                  </div>
                  <div className="keyrow-actions">
                    {!revoked && (
                      // The address is in the accessible name: a screen reader's
                      // element list would otherwise show N identical "Revoke"
                      // buttons on a destructive action.
                      <button type="button" className="icon-btn danger tip" data-tip="Revoke key"
                              aria-label={`Revoke key ${maskedKey(k)}`}
                              onClick={() => revoke(k)}>
                        <IconTrash />
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        <p className="muted small">
          Point your client at <code>/mcp</code> on this server and send the key as
          a bearer token. Keys all begin <code>{KEY_PREFIX}</code>, so one that
          turns up in a log or a config file is recognisable on sight.
        </p>
      </div>

      {minted && (
        <KeyReveal secret={minted.key} label={minted.label}
                   onClose={() => setMinted(null)} />
      )}
    </main>
  );
}
