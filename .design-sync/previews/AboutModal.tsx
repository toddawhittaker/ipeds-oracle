import React from "react";
import { AboutModal } from "ipeds-query-web";

// An INFORMATIONAL dialog — deliberately not built on useConfirm, which is
// confirm/cancel shaped. It reuses the .modal-* CSS and the ConfirmModal a11y
// pattern (focus-in, Escape/overlay/Close, return focus to opener).
//
// It renders as an overlay, so the card is configured cardMode "single".

export const UpdateAvailable = () => (
  <AboutModal
    onClose={() => {}}
    isAdmin
    version={{ current: "0.1.0", latest: "0.2.0", update_available: true }}
  />
);

export const UpToDate = () => (
  <AboutModal onClose={() => {}} isAdmin version={{ current: "0.2.0", update_available: false }} />
);

export const NonAdmin = () => (
  <AboutModal onClose={() => {}} version={{ current: "0.2.0", update_available: false }} />
);
