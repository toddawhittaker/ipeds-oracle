import React from "react";
import { Wordmark } from "ipeds-query-web";

// The IPEDS Oracle identity, drawn as inline SVG from the theme tokens (never a
// PNG pair) so light/dark comes from one source: mono "IPEDS" · ochre rule ·
// serif "Oracle" · the Column mark.

export const Full = () => <Wordmark />;

export const TypeOnly = () => <Wordmark showIcon={false} />;

// NO dark-theme story here on purpose: the theme tokens are defined on
// :root[data-theme="dark"], so a nested <div data-theme="dark"> inherits the
// LIGHT values and renders a card that lies about the theme. Dark mode is set
// on the document root, which is page-wide and cannot vary per card cell.

export const InTopBar = () => (
  <div
    style={{
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      padding: "10px 16px",
      borderBottom: "1px solid var(--line)",
      background: "var(--panel)",
    }}
  >
    <Wordmark />
  </div>
);
