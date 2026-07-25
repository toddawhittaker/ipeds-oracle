import React from "react";
import { IconChevronDown } from "ipeds-query-web";

// Stroke is currentColor at 2px on a 24-viewBox, so an icon inherits its
// button's text colour — recolour with `color`, never a fill prop.
// Default size is 15px, which is what the product's inline controls use.

export const Default = () => <IconChevronDown size={24} />;

export const Sizes = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <IconChevronDown size={15} />
    <IconChevronDown size={20} />
    <IconChevronDown size={28} />
  </span>
);

export const InheritsColor = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <span style={{ color: "var(--accent)" }}><IconChevronDown size={24} /></span>
    <span style={{ color: "var(--ochre)" }}><IconChevronDown size={24} /></span>
    <span style={{ color: "var(--danger)" }}><IconChevronDown size={24} /></span>
    <span style={{ color: "var(--muted)" }}><IconChevronDown size={24} /></span>
  </span>
);

export const InButton = () => (
  <button type="button" className="link" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
    <IconChevronDown size={15} />
    More
  </button>
);
