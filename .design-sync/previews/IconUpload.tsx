import React from "react";
import { IconUpload } from "ipeds-query-web";

// Stroke is currentColor at 2px on a 24-viewBox, so an icon inherits its
// button's text colour — recolour with `color`, never a fill prop.
// Default size is 15px, which is what the product's inline controls use.

export const Default = () => <IconUpload size={24} />;

export const Sizes = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <IconUpload size={15} />
    <IconUpload size={20} />
    <IconUpload size={28} />
  </span>
);

export const InheritsColor = () => (
  <span style={{ display: "inline-flex", gap: 14, alignItems: "center" }}>
    <span style={{ color: "var(--accent)" }}><IconUpload size={24} /></span>
    <span style={{ color: "var(--ochre)" }}><IconUpload size={24} /></span>
    <span style={{ color: "var(--danger)" }}><IconUpload size={24} /></span>
    <span style={{ color: "var(--muted)" }}><IconUpload size={24} /></span>
  </span>
);

export const InButton = () => (
  <button type="button" className="link" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
    <IconUpload size={15} />
    Upload
  </button>
);
