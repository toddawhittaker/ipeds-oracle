import React from "react";
import { IconShield } from "ipeds-query-web";

// Stroke is currentColor at 2px on a 24-viewBox, so an icon inherits its
// button's text colour — recolour with `color`, never a fill prop.
// Default size is 15px, which is what the product's inline controls use.

export const Default = () => <IconShield size={24} />;

export const Sizes = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <IconShield size={15} />
    <IconShield size={20} />
    <IconShield size={28} />
  </span>
);

export const InheritsColor = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <span style={{ color: "var(--accent)" }}><IconShield size={24} /></span>
    <span style={{ color: "var(--ochre)" }}><IconShield size={24} /></span>
    <span style={{ color: "var(--danger)" }}><IconShield size={24} /></span>
    <span style={{ color: "var(--muted)" }}><IconShield size={24} /></span>
  </span>
);

export const InButton = () => (
  <button type="button" className="link" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
    <IconShield size={15} />
    Admin
  </button>
);
