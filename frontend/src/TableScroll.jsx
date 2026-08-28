import React from "react";

// The horizontal scroll region a wide table sits in (WCAG 1.4.10 Reflow).
//
// `html, body { overflow: hidden }`, so the page cannot scroll sideways and the
// nearest scroller is the whole `.admin` column: without this wrapper, reaching
// a table's right-hand columns at 320px means scrolling the entire screen in two
// directions, heading and section nav included. That is the defect 1.4.10 exists
// to prevent, and `overflow-x: auto` here is the whole fix — the table keeps its
// desktop geometry, because `width: 100%` still wins whenever it fits.
//
// It is a component rather than a `<div className="table-scroll">` at each call
// site because the FOCUSABLE half is easy to copy wrong, and was: Usage.jsx
// copied the wrapper off DataTable.jsx onto a table of plain <th>/<td> and
// shipped a scroll region no keyboard user could reach (WCAG 2.1.1). A scroll
// region is only reachable if something inside it can take focus and scroll it
// into view; when nothing can, the region itself has to be the tab stop. Passing
// `focusable` makes that an answer every caller must give, and keeps the three
// attributes it takes in one place.
//
// `label` is the table's own accessible name; the ", scrollable" suffix is added
// here so the region never announces the identical name twice on the way in.
// DataTable derives `focusable` (see datatable-region.js); hand-rolled tables
// state it, because there is no column config to derive it from.

/**
 * @param {object} props
 * @param {boolean} [props.focusable] True when nothing inside the table can take focus, so the region must be the tab stop.
 * @param {string} [props.label] The table's own accessible name — ", scrollable" is appended for the region.
 * @param {React.ReactNode} props.children The `<table>` this region scrolls.
 */
export default function TableScroll({ focusable, label, children }) {
  return (
    <div className={"table-scroll" + (focusable ? " table-scroll-region" : "")}
         {...(focusable
           ? { tabIndex: 0, role: "region", "aria-label": `${label || "Table"}, scrollable` }
           : {})}>
      {children}
    </div>
  );
}
