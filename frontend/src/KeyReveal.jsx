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
  // Focus opens on COPY, not on the dismiss button. The dialog exists to get an
  // unrecoverable value out of the screen and into the user's clipboard, and a
  // user who submitted the mint form with Enter is one habitual second Enter
  // away from destroying it. It also restores the app's modal grammar, where the
  // filled button is the action and focus lands on the safe one (ConfirmModal).
  const copyRef = useRef(null);
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
    copyRef.current?.focus();
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
    // Minimal focus trap over the dialog's own stops: the key field (a readonly
    // input, so it can be selected and read without a mouse) and the two
    // buttons. `input,button` rather than `button` alone for that reason.
    if (e.key !== "Tab") return;
    const items = [...dialogRef.current.querySelectorAll("input,button")];
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    // Clicking the dialog's own padding focuses the container (it carries
    // tabIndex={-1}), and from there Shift+Tab walked backwards OUT of the
    // dialog. ConfirmModal.jsx has carried this branch all along; this file
    // diverged from it for no reason.
    if (!dialogRef.current.contains(active)) {
      e.preventDefault();
      first.focus();
    } else if (e.shiftKey && active === first) {
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
      // Held until the dialog closes, deliberately: a tick that reverts after
      // 1.4s is gone by the moment the user reaches for Done, which is exactly
      // when they want to know whether they got it. For an ordinary copy button
      // a flash is right; for the only appearance of an unrecoverable value the
      // answer has to still be on screen at the decision point.
      setCopied(true);
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
          {/* On the admin path this is somebody ELSE's key, and a title reading
              "your API key" told the wrong person they were the owner. */}
          <h2 className="modal-title" id={titleId}>
            {email ? `Copy this key for ${email}` : "Copy your API key now"}
          </h2>
        </div>
        <div className="modal-body" id={bodyId}>
          <p>
            {email
              ? "This is the only time the key will be shown. Nothing stores it, "
                + "so if it is lost the only fix is to revoke it and issue another."
              : "This is the only time this key will be shown. Nothing stores it, "
                + "so if you lose it you will have to revoke it and create another."}
          </p>
          <div className="keyreveal-value">
            {/* A READONLY INPUT, not a <code>: the copy button's own failure
                message tells the user to select the text and copy it by hand
                (the documented plain-http self-host case, where
                navigator.clipboard does not exist), and a <code> gave a
                keyboard-only user no way to do that. As an input it is a tab
                stop that selects itself on focus, and a screen reader can walk
                it character by character. Still selectable by mouse, which was
                the reason the <code> was chosen. */}
            <input className="keyreveal-secret" data-testid="revealed-key"
                   readOnly value={secret} aria-label="Your new API key"
                   onFocus={(e) => e.target.select()} />
          </div>
          {/* aria-live so a screen-reader user hears the copy land — the button's
              own label change is not reliably announced, and there is no toast on
              the success path. */}
          <span className="sr-only" aria-live="polite">{copied ? "API key copied." : ""}</span>
          {email ? (
            <p>
              <strong>Send it to them over a channel you trust</strong> — it lets
              any tool ask questions as them.
            </p>
          ) : null}
          <p className="muted small">
            {label ? <>Labelled &ldquo;{label}&rdquo;. </> : null}
            Give it to an MCP client as a bearer token.
          </p>
        </div>
        {/* Done LEFT and plain, Copy right and filled: ConfirmModal's row, so the
            action sits where every other dialog in the app puts it. */}
        <div className="modal-actions">
          <button type="button" className="modal-cancel" onClick={onClose}>
            Done
          </button>
          <button type="button" ref={copyRef} onClick={copy}
                  className={"modal-confirm" + (copied ? " copied" : "")}>
            {copied ? <IconCheck size={15} /> : <IconCopy size={15} />}
            {copied ? "Copied" : "Copy key"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
