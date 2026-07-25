import React from "react";
import { IconMoon } from "ipeds-query-web";

// Stroke is currentColor at 2px on a 24-viewBox, so an icon inherits its
// button's text colour — recolour with `color`, never a fill prop.
// Default size is 15px, which is what the product's inline controls use.

export const Default = () => <IconMoon size={24} />;

export const Sizes = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <IconMoon size={15} />
    <IconMoon size={20} />
    <IconMoon size={28} />
  </span>
);

export const InheritsColor = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <span style={{ color: "var(--accent)" }}><IconMoon size={24} /></span>
    <span style={{ color: "var(--ochre)" }}><IconMoon size={24} /></span>
    <span style={{ color: "var(--danger)" }}><IconMoon size={24} /></span>
    <span style={{ color: "var(--muted)" }}><IconMoon size={24} /></span>
  </span>
);

export const InButton = () => (
  <button type="button" className="link" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
    <IconMoon size={15} />
    Dark theme
  </button>
);
