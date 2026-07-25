import React from "react";
import { IconPause } from "ipeds-query-web";

// Stroke is currentColor at 2px on a 24-viewBox, so an icon inherits its
// button's text colour — recolour with `color`, never a fill prop.
// Default size is 15px, which is what the product's inline controls use.

export const Default = () => <IconPause size={24} />;

export const Sizes = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <IconPause size={15} />
    <IconPause size={20} />
    <IconPause size={28} />
  </span>
);

export const InheritsColor = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <span style={{ color: "var(--accent)" }}><IconPause size={24} /></span>
    <span style={{ color: "var(--ochre)" }}><IconPause size={24} /></span>
    <span style={{ color: "var(--danger)" }}><IconPause size={24} /></span>
    <span style={{ color: "var(--muted)" }}><IconPause size={24} /></span>
  </span>
);

export const InButton = () => (
  <button type="button" className="link" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
    <IconPause size={15} />
    Pause
  </button>
);
