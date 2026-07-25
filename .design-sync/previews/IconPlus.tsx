import React from "react";
import { IconPlus } from "ipeds-query-web";

// Stroke is currentColor at 2px on a 24-viewBox, so an icon inherits its
// button's text colour — recolour with `color`, never a fill prop.
// Default size is 15px, which is what the product's inline controls use.

export const Default = () => <IconPlus size={24} />;

export const Sizes = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <IconPlus size={15} />
    <IconPlus size={20} />
    <IconPlus size={28} />
  </span>
);

export const InheritsColor = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <span style={{ color: "var(--accent)" }}><IconPlus size={24} /></span>
    <span style={{ color: "var(--ochre)" }}><IconPlus size={24} /></span>
    <span style={{ color: "var(--danger)" }}><IconPlus size={24} /></span>
    <span style={{ color: "var(--muted)" }}><IconPlus size={24} /></span>
  </span>
);

export const InButton = () => (
  <button type="button" className="link" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
    <IconPlus size={15} />
    Add
  </button>
);
