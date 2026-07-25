import React from "react";
import { IconClose } from "ipeds-query-web";

// Stroke is currentColor at 2px on a 24-viewBox, so an icon inherits its
// button's text colour — recolour with `color`, never a fill prop.
// Default size is 15px, which is what the product's inline controls use.

export const Default = () => <IconClose size={24} />;

export const Sizes = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <IconClose size={15} />
    <IconClose size={20} />
    <IconClose size={28} />
  </span>
);

export const InheritsColor = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <span style={{ color: "var(--accent)" }}><IconClose size={24} /></span>
    <span style={{ color: "var(--ochre)" }}><IconClose size={24} /></span>
    <span style={{ color: "var(--danger)" }}><IconClose size={24} /></span>
    <span style={{ color: "var(--muted)" }}><IconClose size={24} /></span>
  </span>
);

export const InButton = () => (
  <button type="button" className="link" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
    <IconClose size={15} />
    Close
  </button>
);
