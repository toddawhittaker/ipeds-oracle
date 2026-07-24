import React, { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { IconInfo, IconGitHub } from "./icons.jsx";
import Wordmark from "./Wordmark.jsx";

const IPEDS_URL = "https://nces.ed.gov/ipeds/";

const GITHUB_URL = "https://github.com/toddawhittaker/ipeds-oracle";
const LICENSE_URL = "https://github.com/toddawhittaker/ipeds-oracle/blob/main/LICENSE";
// The guides live in the repo and render on GitHub (with screenshots). The Admin
// guide link is shown only to admins (see the `isAdmin` gate below).
const USER_GUIDE_URL = "https://github.com/toddawhittaker/ipeds-oracle/blob/main/docs/USER_GUIDE.md";
const ADMIN_GUIDE_URL = "https://github.com/toddawhittaker/ipeds-oracle/blob/main/docs/ADMIN_GUIDE.md";

// An informational "About" dialog. Deliberately NOT built on useConfirm (that's
// action-shaped — a confirm/cancel button row and an onConfirm callback); this is
// a single-Close dialog whose body is prose + a link. It reuses the .modal-* CSS
// and mirrors ConfirmModal's a11y contract: role="dialog" + aria-modal, focus moves
// in on open and returns to the opener on close, Escape / overlay-click / Close all
// dismiss, and the background is inert while it's open. Pinned in
// frontend/e2e/user-menu.spec.js.
export default function AboutModal({ onClose, isAdmin = false }) {
  const dialogRef = useRef(null);
  const closeRef = useRef(null);
  const openerRef = useRef(null);
  const ids = useId();
  const titleId = `about-title-${ids}`;
  const bodyId = `about-body-${ids}`;

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
    // Minimal focus trap: only two stops (the link and Close), so keep Tab inside.
    if (e.key !== "Tab") return;
    const items = [...dialogRef.current.querySelectorAll("a[href], button")];
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

  return createPortal(
    <div className="modal-overlay" onMouseDown={onOverlayDown}>
      <div
        className="modal neutral"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        ref={dialogRef}
        tabIndex={-1}
        onKeyDown={onKeyDown}
      >
        <div className="modal-head">
          <span className="modal-icon neutral"><IconInfo size={20} /></span>
          <h2 className="modal-title about-title" id={titleId}>
            About <Wordmark showIcon={false} />
          </h2>
        </div>
        <div className="modal-body" id={bodyId}>
          <p>
            <strong>IPEDS Oracle</strong> answers natural-language questions about U.S.
            colleges and universities from the{" "}
            <a href={IPEDS_URL} target="_blank" rel="noreferrer">IPEDS dataset</a> — the
            U.S. Department of Education&rsquo;s <strong>Integrated Postsecondary
            Education Data System</strong>, its annual census of postsecondary
            institutions.
          </p>
          <p>
            Ask a question in plain English and an AI agent turns it into SQL against
            the read-only IPEDS database, then streams back an answer with the figures,
            tables, and charts behind it.
          </p>
          <p>
            <strong>Why &ldquo;Oracle&rdquo;?</strong> Nothing to do with the database
            or cloud company. It&rsquo;s a nod to the <strong>Oracle of Delphi</strong>{" "}
            of Greek mythology — the place you went with a question and came away with
            an answer.
          </p>
          <p className="about-guides">
            <strong>Guides:</strong>{" "}
            <a href={USER_GUIDE_URL} target="_blank" rel="noreferrer">Using IPEDS Oracle</a>
            {isAdmin && (
              <>
                {" · "}
                <a href={ADMIN_GUIDE_URL} target="_blank" rel="noreferrer">Admin guide</a>
              </>
            )}
          </p>
        </div>
        <div className="modal-actions about-actions">
          <a className="about-gh" href={GITHUB_URL} target="_blank" rel="noreferrer"
             aria-label="View the source code on GitHub">
            <IconGitHub size={26} />
          </a>
          <p className="about-copyright muted small">
            &copy; 2026 Todd Whittaker ·{" "}
            <a href={LICENSE_URL} target="_blank" rel="noreferrer">MIT licensed</a>
          </p>
          <button type="button" className="modal-confirm" ref={closeRef} onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
