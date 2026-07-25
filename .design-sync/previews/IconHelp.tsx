import React from "react";
import { IconHelp } from "ipeds-query-web";

// Stroke is currentColor at 2px on a 24-viewBox, so an icon inherits its
// button's text colour — recolour with `color`, never a fill prop.
// Default size is 15px, which is what the product's inline controls use.

export const Default = () => <IconHelp size={24} />;

export const Sizes = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <IconHelp size={15} />
    <IconHelp size={20} />
    <IconHelp size={28} />
  </span>
);

export const InheritsColor = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <span style={{ color: "var(--accent)" }}><IconHelp size={24} /></span>
    <span style={{ color: "var(--ochre)" }}><IconHelp size={24} /></span>
    <span style={{ color: "var(--danger)" }}><IconHelp size={24} /></span>
    <span style={{ color: "var(--muted)" }}><IconHelp size={24} /></span>
  </span>
);

export const InButton = () => (
  <button type="button" className="link" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
    <IconHelp size={15} />
    Help
  </button>
);
