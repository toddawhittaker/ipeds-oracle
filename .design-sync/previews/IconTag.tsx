import React from "react";
import { IconTag } from "ipeds-query-web";

// Stroke is currentColor at 2px on a 24-viewBox, so an icon inherits its
// button's text colour — recolour with `color`, never a fill prop.
// Default size is 15px, which is what the product's inline controls use.

export const Default = () => <IconTag size={24} />;

export const Sizes = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <IconTag size={15} />
    <IconTag size={20} />
    <IconTag size={28} />
  </span>
);

export const InheritsColor = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <span style={{ color: "var(--accent)" }}><IconTag size={24} /></span>
    <span style={{ color: "var(--ochre)" }}><IconTag size={24} /></span>
    <span style={{ color: "var(--danger)" }}><IconTag size={24} /></span>
    <span style={{ color: "var(--muted)" }}><IconTag size={24} /></span>
  </span>
);

export const InButton = () => (
  <button type="button" className="link" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
    <IconTag size={15} />
    Rename
  </button>
);
