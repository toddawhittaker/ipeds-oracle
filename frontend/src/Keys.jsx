import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { loadErrorMessage } from "./authcopy.js";
import { KEY_PREFIX, maskedKey, sortByNewest } from "./apikeys.js";
import { fmtDateTime } from "./admin/format.js";
import { IconCopy, IconEdit, IconTrash } from "./icons.jsx";
import { copyText } from "./clipboard.js";
import { COPY_FAILED } from "./announce.js";
import { USER_GUIDE_URL } from "./links.js";
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
  // Inline relabel: which key id is being edited (null = none) + the draft text.
  // Same shape as the chat sidebar's rename (Chat.jsx), including the
  // done-guard: Enter and Escape both settle the edit, and the blur that
  // follows must not commit a second time.
  const [editingId, setEditingId] = useState(null);
  const [editText, setEditText] = useState("");
  const editDone = useRef(false);
  // window.location.origin, not a setting: this page is being SERVED from the
  // host the client has to reach, so the address is already known here — which
  // is exactly what the guides cannot say.
  const endpoint = `${window.location.origin}/mcp`;
  const connectCommand =
    `claude mcp add --transport http ipeds ${endpoint} \\\n`
    + `  --header "Authorization: Bearer ${KEY_PREFIX}…"`;

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

  // --- Inline relabel --------------------------------------------------------
  // Pencil -> the row's label swaps to an input. Enter/blur commit, Escape
  // cancels. The commit is optimistic: the label is cosmetic, so showing it
  // immediately and putting it back with a toast if the PATCH fails beats
  // holding the row in a spinner.
  function startEdit(row) {
    editDone.current = false;
    setEditingId(row.id);
    setEditText(row.label || "");
  }
  // The input unmounts on commit, and focus would drop to <body> (WCAG 2.4.3).
  // Hand it back to the row's own pencil once React has put it back.
  const refocusRow = (id) => requestAnimationFrame(() =>
    document.getElementById(`keyedit-${id}`)?.focus());
  function cancelEdit(row) {
    if (editDone.current) return;
    editDone.current = true;
    setEditingId(null); setEditText("");
    refocusRow(row.id);
  }
  function commitEdit(row) {
    if (editDone.current) return;
    editDone.current = true;
    const label = editText.trim();
    setEditingId(null); setEditText("");
    refocusRow(row.id);
    // Unchanged is a cancel, not an edit — don't round-trip a no-op. Emptied is
    // NOT: clearing a label is a real change the server stores as NULL.
    if (label === (row.label || "")) return;
    const prev = row.label;
    setRows((rs) => rs.map((r) => (r.id === row.id ? { ...r, label: label || null } : r)));
    api.relabelApiKey(row.id, label).catch(() => {
      setRows((rs) => rs.map((r) => (r.id === row.id ? { ...r, label: prev } : r)));
      toast("Couldn't rename that key. Try again.", "error");
    });
  }

  function revoke(row) {
    confirm({
      variant: "danger",
      title: "Revoke this key?",
      body: `Any MCP client still using ${maskedKey(row)} stops working immediately, `
        + "and the key leaves this list. This can't be undone — create a new key "
        + "instead.",
      details: row.label || undefined,
      confirmLabel: "Revoke key",
      busyLabel: "Revoking…",
      successToast: "Key revoked.",
      errorToast: "Couldn't revoke that key.",
      onConfirm: () => api.revokeApiKey(row.id),
      // The row leaves this list on the next load (GET /api/keys returns live
      // keys only), taking the button that opened the modal with it — so there
      // is nothing for ConfirmModal to hand post-success focus back to. The
      // heading is the stable landing spot.
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
        {/* aria-busy is not a live message and a name change on the element that
            already holds focus is not reliably announced, so a screen-reader user
            heard nothing between pressing the button and the dialog opening.
            ConfirmModal.jsx solved the same problem the same way. */}
        <span className="sr-only" aria-live="polite">
          {minting ? "Creating key…" : ""}
        </span>

        {err ? (
          <p className="denied-error" role="alert">{err}</p>
        ) : rows.length === 0 ? (
          <p className="muted">
            No keys yet. Create one above, then give it to your MCP client.
          </p>
        ) : (
          <ul className="keylist" role="list">
            {sortByNewest(rows).map((k) => {
              return (
                <li key={k.id} className="keyrow">
                  <div className="keyrow-main">
                    {editingId === k.id ? (
                      <input
                        className="keyrow-rename" value={editText} autoFocus
                        maxLength={80}
                        aria-label={`Label for key ${maskedKey(k)}`}
                        onFocus={(e) => e.target.select()}
                        onChange={(e) => setEditText(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") { e.preventDefault(); commitEdit(k); }
                          else if (e.key === "Escape") cancelEdit(k);
                        }}
                        onBlur={() => commitEdit(k)}
                      />
                    ) : (
                      <span className="keyrow-label">{k.label || "Unlabelled key"}</span>
                    )}
                    <code className="keyrow-id">{maskedKey(k)}</code>
                  </div>
                  <div className="keyrow-meta muted small">
                    Created {fmtDateTime(k.created_at)}
                    {k.created_by ? ` by an administrator (${k.created_by})` : ""}
                    {" · Last used "}
                    {k.last_used_at ? fmtDateTime(k.last_used_at) : "never"}
                  </div>
                  <div className="keyrow-actions">
                    {/* The address is in each accessible name: a screen reader's
                        element list would otherwise show N identical "Rename"
                        and "Revoke" buttons, one of them destructive. */}
                    <button type="button" id={`keyedit-${k.id}`}
                            className="icon-btn tip" data-tip="Rename key"
                            aria-label={`Rename key ${maskedKey(k)}`}
                            onClick={() => startEdit(k)}>
                      <IconEdit />
                    </button>
                    <button type="button" className="icon-btn danger tip" data-tip="Revoke key"
                            aria-label={`Revoke key ${maskedKey(k)}`}
                            onClick={() => revoke(k)}>
                      <IconTrash />
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        {/* The docs all carry this command with a <host> placeholder, because a
            document cannot know the deployment. This page can: it is the one
            place the real endpoint can be printed, and it is the moment of need
            — the user has just copied a key. The KEY stays a placeholder; a
            secret does not belong in a block that lives on the page forever. */}
        <h2 className="keys-connect-head">Connecting a client</h2>
        <p className="muted small">
          Keys all begin <code>{KEY_PREFIX}</code>, so one that turns up in a log
          or a config file is recognisable on sight.
        </p>
        <div className="endpoint-row">
          <code>{endpoint}</code>
          <button type="button" className="icon-btn tip" data-tip="Copy endpoint"
                  aria-label="Copy the endpoint URL"
                  onClick={() => copyText(endpoint).then(
                    (ok) => toast(ok ? "Endpoint copied." : COPY_FAILED,
                                  ok ? "success" : "error"))}>
            <IconCopy />
          </button>
        </div>
        <pre className="codeblock thin-scroll">{connectCommand}</pre>
        <p className="muted small keys-connect">
          Other clients: the same URL, with the key sent as a bearer token.{" "}
          <a href={USER_GUIDE_URL} target="_blank" rel="noreferrer">
            How to connect a client
          </a>
        </p>
      </div>

      {minted && (
        <KeyReveal secret={minted.key} label={minted.label}
                   onClose={() => setMinted(null)} />
      )}
    </main>
  );
}
