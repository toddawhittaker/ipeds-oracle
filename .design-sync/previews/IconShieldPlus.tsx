import React from "react";
import { IconShieldPlus } from "ipeds-query-web";

// Stroke is currentColor at 2px on a 24-viewBox, so an icon inherits its
// button's text colour — recolour with `color`, never a fill prop.
// Default size is 15px, which is what the product's inline controls use.

export const Default = () => <IconShieldPlus size={24} />;

export const Sizes = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <IconShieldPlus size={15} />
    <IconShieldPlus size={20} />
    <IconShieldPlus size={28} />
  </span>
);

export const InheritsColor = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <span style={{ color: "var(--accent)" }}><IconShieldPlus size={24} /></span>
    <span style={{ color: "var(--ochre)" }}><IconShieldPlus size={24} /></span>
    <span style={{ color: "var(--danger)" }}><IconShieldPlus size={24} /></span>
    <span style={{ color: "var(--muted)" }}><IconShieldPlus size={24} /></span>
  </span>
);

export const InButton = () => (
  <button type="button" className="link" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
    <IconShieldPlus size={15} />
    Make admin
  </button>
);
