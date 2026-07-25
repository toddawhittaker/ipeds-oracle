import React from "react";
import { IconSun } from "ipeds-query-web";

// Stroke is currentColor at 2px on a 24-viewBox, so an icon inherits its
// button's text colour — recolour with `color`, never a fill prop.
// Default size is 15px, which is what the product's inline controls use.

export const Default = () => <IconSun size={24} />;

export const Sizes = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <IconSun size={15} />
    <IconSun size={20} />
    <IconSun size={28} />
  </span>
);

export const InheritsColor = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <span style={{ color: "var(--accent)" }}><IconSun size={24} /></span>
    <span style={{ color: "var(--ochre)" }}><IconSun size={24} /></span>
    <span style={{ color: "var(--danger)" }}><IconSun size={24} /></span>
    <span style={{ color: "var(--muted)" }}><IconSun size={24} /></span>
  </span>
);

export const InButton = () => (
  <button type="button" className="link" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
    <IconSun size={15} />
    Light theme
  </button>
);
