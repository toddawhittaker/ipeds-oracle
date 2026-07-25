import React from "react";
import { IconUnlock } from "ipeds-query-web";

// Stroke is currentColor at 2px on a 24-viewBox, so an icon inherits its
// button's text colour — recolour with `color`, never a fill prop.
// Default size is 15px, which is what the product's inline controls use.

export const Default = () => <IconUnlock size={24} />;

export const Sizes = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <IconUnlock size={15} />
    <IconUnlock size={20} />
    <IconUnlock size={28} />
  </span>
);

export const InheritsColor = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <span style={{ color: "var(--accent)" }}><IconUnlock size={24} /></span>
    <span style={{ color: "var(--ochre)" }}><IconUnlock size={24} /></span>
    <span style={{ color: "var(--danger)" }}><IconUnlock size={24} /></span>
    <span style={{ color: "var(--muted)" }}><IconUnlock size={24} /></span>
  </span>
);

export const InButton = () => (
  <button type="button" className="link" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
    <IconUnlock size={15} />
    Unblock
  </button>
);
