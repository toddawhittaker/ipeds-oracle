// Preview-only environment for the claude.ai/design sync (see /.design-sync/).
//
// NOT imported by the app. It exists because UserMenu renders a react-router
// <Link>, which throws outside a Router — so every preview card has to be
// wrapped in one. It lives here rather than in /.design-sync/ so that
// "react-router" resolves from frontend/node_modules; it lives outside src/ so
// the converter's component scan never picks PreviewRouter up as a component.
//
// Wired via cfg.extraEntries + cfg.provider in .design-sync/config.json.
import React from "react";
import { MemoryRouter } from "react-router";

export function PreviewRouter({ children }) {
  return React.createElement(MemoryRouter, { initialEntries: ["/"] }, children);
}
