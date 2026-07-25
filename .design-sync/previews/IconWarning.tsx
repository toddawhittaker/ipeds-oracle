import React from "react";
import { IconWarning } from "ipeds-query-web";

// Stroke is currentColor at 2px on a 24-viewBox, so an icon inherits its
// button's text colour — recolour with `color`, never a fill prop.
// Default size is 15px, which is what the product's inline controls use.

export const Default = () => <IconWarning size={24} />;

export const Sizes = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <IconWarning size={15} />
    <IconWarning size={20} />
    <IconWarning size={28} />
  </span>
);

export const InheritsColor = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <span style={{ color: "var(--accent)" }}><IconWarning size={24} /></span>
    <span style={{ color: "var(--ochre)" }}><IconWarning size={24} /></span>
    <span style={{ color: "var(--danger)" }}><IconWarning size={24} /></span>
    <span style={{ color: "var(--muted)" }}><IconWarning size={24} /></span>
  </span>
);

export const InButton = () => (
  <button type="button" className="link" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
    <IconWarning size={15} />
    Warning
  </button>
);
