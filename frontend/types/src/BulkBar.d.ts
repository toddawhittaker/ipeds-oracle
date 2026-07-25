/**
 * The `actions` sub-shape is INLINE on purpose — a named @typedef emits into the
 * published design-system contract as a reference the file never defines. See the
 * note in DataTable.jsx.
 *
 * @typedef {object} BulkBarProps
 * @property {{ one: string, many: string }} nouns Row-type nouns, e.g.
 *   { one: "user", many: "users" }. Drives every count string.
 * @property {"page" | "all"} mode "page" = the current page's selection; "all" =
 *   every matching row (and then `selectedIds` upstream holds EXCLUSIONS, not
 *   selections).
 * @property {number} count Selected row count. The whole toolbar renders nothing at
 *   0 — it is contextual, never a persistent strip of disabled buttons.
 * @property {number} totalEligible
 * @property {number} pageSelectedCount
 * @property {number} pageEligibleCount
 * @property {() => void} onSelectAllMatching Invoked by the "select all N matching"
 *   banner, which appears once a full page is selected and more rows match.
 * @property {() => void} onClear
 * @property {Array<{ key: string, label: string, icon: React.ComponentType<{ size?: number }>, onClick: () => void, variant?: "danger", disabled?: boolean, title?: string }>} actions
 *   Buttons, in order. Labels use STABLE verbs — the count belongs in the confirm dialog. A `variant: "danger"` action splits off past a divider.
 * @property {() => void} [onFocusFallback] Called before clear/escalate so focus
 *   never lands on a control that is about to disappear.
 */
/** @param {BulkBarProps} props */
export default function BulkBar({ nouns, mode, count, totalEligible, pageSelectedCount, pageEligibleCount, onSelectAllMatching, onClear, actions, onFocusFallback, }: BulkBarProps): React.JSX.Element;
/**
 * into the
 * published design-system contract as a reference the file never defines. See the
 * note in DataTable.jsx.
 */
export type emits = any;
/**
 * The `actions` sub-shape is INLINE on purpose — a named
 */
export type BulkBarProps = {
    /**
     * Row-type nouns, e.g.
     * { one: "user", many: "users" }. Drives every count string.
     */
    nouns: {
        one: string;
        many: string;
    };
    /**
     * "page" = the current page's selection; "all" =
     * every matching row (and then `selectedIds` upstream holds EXCLUSIONS, not
     * selections).
     */
    mode: "page" | "all";
    /**
     * Selected row count. The whole toolbar renders nothing at
     * 0 — it is contextual, never a persistent strip of disabled buttons.
     */
    count: number;
    totalEligible: number;
    pageSelectedCount: number;
    pageEligibleCount: number;
    /**
     * Invoked by the "select all N matching"
     * banner, which appears once a full page is selected and more rows match.
     */
    onSelectAllMatching: () => void;
    onClear: () => void;
    /**
     * Buttons, in order. Labels use STABLE verbs — the count belongs in the confirm dialog. A `variant: "danger"` action splits off past a divider.
     */
    actions: Array<{
        key: string;
        label: string;
        icon: React.ComponentType<{
            size?: number;
        }>;
        onClick: () => void;
        variant?: "danger";
        disabled?: boolean;
        title?: string;
    }>;
    /**
     * Called before clear/escalate so focus
     * never lands on a control that is about to disappear.
     */
    onFocusFallback?: () => void;
};
import React from "react";
