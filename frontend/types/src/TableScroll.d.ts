import React from "react";
/**
 * @param {object} props
 * @param {boolean} [props.focusable] True when nothing inside the table can take focus, so the region must be the tab stop.
 * @param {string} [props.label] The table's own accessible name — ", scrollable" is appended for the region.
 * @param {React.ReactNode} props.children The `<table>` this region scrolls.
 */
export default function TableScroll({ focusable, label, children }: {
    focusable?: boolean;
    label?: string;
    children: React.ReactNode;
}): React.JSX.Element;
