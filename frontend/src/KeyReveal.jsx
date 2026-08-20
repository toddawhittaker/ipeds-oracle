import React, { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { IconCheck, IconCopy, IconKey } from "./icons.jsx";
import { copyText } from "./clipboard.js";
import { COPY_FAILED } from "./announce.js";
import { useToast } from "./Toast.jsx";

// The one-shot reveal of a freshly minted MCP API key, shared by a user's own
// /keys page and the admin Keys tab.
//
// It exists because the raw key is genuinely unrecoverable: app/apikeys.py
// stores only a SHA-256 hash, so this dialog is the single moment in the key's
// life when the value is on screen. That is why it is NOT built on useConfirm —
// there is no action to confirm and no cancel path. It is AboutModal's shape (a
// single-Close informational dialog reusing the .modal-* CSS) plus a copy
// button, and it mirrors the same a11y contract: role="dialog" + aria-modal,
// focus moves in on open and returns to the opener on close, Escape /
// overlay-click / Close all dismiss, and the background is inert while open.
//
// Deliberately no auto-copy on open: a clipboard write nobody asked for is both
// a surprise and unverifiable (a denied clipboard fails silently), and the whole
// point of the screen is that the user sees they have the value.

/**
 * @typedef {object} KeyRevealProps
 * @property {string} secret The raw key, exactly as minted. Shown once and never
 *   fetched again — the caller must not persist it.
 * @property {string} [label] The label given at mint time, if any.
 * @property {string} [email] Whose key it is. Shown only when an admin minted it
 *   for somebody else, so the reveal names the person to hand it to.
 * @property {() => void} onClose Called on every dismissal path. Required.
 */

/** @param {KeyRevealProps} props */
export default function KeyReveal({ secret, label, email, onClose }) {
  const toast = useToast();
  const dialogRef = useRef(null);
  const closeRef = useRef(null);
  const openerRef = useRef(null);
  const [copied, setCopied] = useState(false);
  const ids = useId();
  const titleId = `keyreveal-title-${ids}`;
  const bodyId = `keyreveal-body-${ids}`;

  useEffect(() => {
    openerRef.current = document.activeElement;
    const appEl = document.querySelector(".app");
    appEl?.setAttribute("inert", "");
    appEl?.setAttribute("aria-hidden", "true");
    closeRef.current?.focus();
    return () => {
      appEl?.removeAttribute("inert");
      appEl?.removeAttribute("aria-hidden");
      const opener = openerRef.current;
      if (opener && document.contains(opener)) {
        requestAnimationFrame(() => opener.focus?.());
      }
    };
  }, []);

  function onKeyDown(e) {
    if (e.key === "Escape") {
      e.stopPropagation();
      onClose();
      return;
    }
    // Minimal focus trap: Copy and Close are the only stops, so keep Tab inside.
    if (e.key !== "Tab") return;
    const items = [...dialogRef.current.querySelectorAll("button")];
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && active === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  }

  function onOverlayDown(e) {
    if (e.target === e.currentTarget) onClose();
  }

  async function copy() {
    // A failure here matters more than anywhere else in the app: this value is
    // not recoverable, so a silently-denied clipboard would leave the user with
    // a key they cannot get back. Say so, and leave the dialog open.
    if (await copyText(secret)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } else {
      toast(COPY_FAILED, "error");
    }
  }

  return createPortal(
    <div className="modal-overlay" onMouseDown={onOverlayDown}>
      <div
        className="modal warning"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        ref={dialogRef}
        tabIndex={-1}
        onKeyDown={onKeyDown}
      >
        <div className="modal-head">
          <span className="modal-icon warning"><IconKey size={20} /></span>
          <h2 className="modal-title" id={titleId}>Copy your API key now</h2>
        </div>
        <div className="modal-body" id={bodyId}>
          <p>
            This is the only time this key will be shown. Nothing stores it, so if
            you lose it you will have to revoke it and create another.
          </p>
          <div className="keyreveal-value">
            {/* The value is selectable text, not an input: a user without a
                working clipboard (plain http on a LAN is the documented
                self-host case) has to be able to select it by hand. */}
            <code className="keyreveal-secret" data-testid="revealed-key">{secret}</code>
            <button type="button" className="icon-btn tip" data-tip="Copy key"
                    aria-label="Copy API key" onClick={copy}>
              {copied ? <IconCheck /> : <IconCopy />}
            </button>
          </div>
          {/* aria-live so a screen-reader user hears the copy land — the icon
              swap alone is silent, and there is no toast on the success path. */}
          <span className="sr-only" aria-live="polite">{copied ? "API key copied." : ""}</span>
          <p className="muted small">
            {email ? <>For <strong>{email}</strong>. Send it to them over a channel you trust. </> : null}
            {label ? <>Labelled &ldquo;{label}&rdquo;. </> : null}
            Give it to an MCP client as a bearer token.
          </p>
        </div>
        <div className="modal-actions">
          <button type="button" className="modal-confirm warning" ref={closeRef} onClick={onClose}>
            Done
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
