import React from "react";
export type DataTableProps = {
    /**
     * All rows, unpaginated — this component owns search, sort and paging client-side.
     */
    rows: Array<Record<string, unknown>>;
    /**
     * `render` defaults to row[key].
     */
    columns: Array<{
        key: string;
        label: string;
        sortable?: boolean;
        render?: (row: any) => React.ReactNode;
        cellTitle?: (row: any) => string;
        colClass?: string;
        thClass?: string;
        cellClass?: string;
    }>;
    /**
     * A FUNCTION returning a row's unique key, e.g. `(r) => r.email` — NOT a field name. Also the focus target.
     */
    rowKey: (row: any) => string | number;
    /**
     * `comparators` + `tiebreak` are REQUIRED — sortRows does Object.keys(comparators), so omitting them renders a blank table.
     */
    config: {
        fields: string[];
        comparators: Record<string, (a: any, b: any) => number>;
        tiebreak: (row: any) => string | number;
        nouns: {
            one: string;
            many: string;
        };
    };
    searchPlaceholder?: string;
    searchLabel?: string;
    searchId?: string;
    /**
     * Shown when there are no rows at all — keep it DISTINCT from a load failure, which must never look like an empty result.
     */
    emptyNoData?: string;
    /**
     * Shown when the search filtered everything out.
     */
    emptyNoMatch?: string;
    /**
     * Defaults to the first sortable column, ascending.
     */
    initialSort?: {
        key: string;
        dir?: "asc" | "desc";
    };
    pageSizes?: number[];
    defaultPageSize?: number;
    sizeLabel?: string;
    ariaLabel?: string;
    renderActions?: (row: any) => React.ReactNode;
    sortLabels?: Record<string, string>;
    tableClass?: string;
    onSearchChange?: (q: string) => void;
    /**
     * Opt-in bulk selection. When falsy NONE of the selection UI renders and the table behaves exactly as it did before selection existed.
     */
    selectable?: boolean;
    /**
     * Selection-id accessor. Defaults to `rowKey`.
     */
    selectionId?: (row: any) => string | number;
    /**
     * "all" INVERTS the meaning of `selectedIds`: it then holds the EXCLUDED ids, not the selected ones.
     */
    selectionMode?: "page" | "all";
    selectedIds?: Set<string | number>;
    /**
     * Rows this returns false for cannot be selected (e.g. your own account).
     */
    rowSelectable?: (row: any) => boolean;
    rowSelectLabel?: (row: any) => string;
    onToggleRow?: (id: string | number) => void;
    onTogglePage?: () => void;
    /**
     * Renders the contextual selection toolbar (normally BulkBar). Called only while at least one row is selected.
     */
    renderSelectionBar?: (ctx: any) => React.ReactNode;
};
export type DataTableHandle = {
    focusSearch: () => void;
    focusRowAction: (rowKey: string | number) => void;
};
/**
 * NOTE ON SHAPE: prop sub-types are written INLINE, not as named @typedefs. The
 * design-sync converter fully resolves types into the published contract and
 * prints a named alias by name — so a named typedef here emits as a reference to
 * something the published .d.ts never defines, and the design agent sees an
 * unresolvable type. Keep these inline.
 *
 * Per-prop doc comments are capped at 120 chars downstream, so lead with the
 * actionable half of any warning.
 *
 * @typedef {object} DataTableProps
 * @property {Array<Record<string, unknown>>} rows All rows, unpaginated — this component owns search, sort and paging client-side.
 * @property {Array<{ key: string, label: string, sortable?: boolean, render?: (row: any) => React.ReactNode, cellTitle?: (row: any) => string, colClass?: string, thClass?: string, cellClass?: string }>} columns
 *   `render` defaults to row[key].
 * @property {(row: any) => string | number} rowKey A FUNCTION returning a row's unique key, e.g. `(r) => r.email` — NOT a field name. Also the focus target.
 * @property {{ fields: string[], comparators: Record<string, (a: any, b: any) => number>, tiebreak: (row: any) => string | number, nouns: { one: string, many: string } }} config
 *   `comparators` + `tiebreak` are REQUIRED — sortRows does Object.keys(comparators), so omitting them renders a blank table.
 * @property {string} [searchPlaceholder]
 * @property {string} [searchLabel]
 * @property {string} [searchId]
 * @property {string} [emptyNoData] Shown when there are no rows at all — keep it DISTINCT from a load failure, which must never look like an empty result.
 * @property {string} [emptyNoMatch] Shown when the search filtered everything out.
 * @property {{ key: string, dir?: "asc" | "desc" }} [initialSort] Defaults to the first sortable column, ascending.
 * @property {number[]} [pageSizes]
 * @property {number} [defaultPageSize]
 * @property {string} [sizeLabel]
 * @property {string} [ariaLabel]
 * @property {(row: any) => React.ReactNode} [renderActions]
 * @property {Record<string, string>} [sortLabels]
 * @property {string} [tableClass]
 * @property {(q: string) => void} [onSearchChange]
 * @property {boolean} [selectable] Opt-in bulk selection. When falsy NONE of the selection UI renders and the table behaves exactly as it did before selection existed.
 * @property {(row: any) => string | number} [selectionId] Selection-id accessor. Defaults to `rowKey`.
 * @property {"page" | "all"} [selectionMode] "all" INVERTS the meaning of `selectedIds`: it then holds the EXCLUDED ids, not the selected ones.
 * @property {Set<string | number>} [selectedIds]
 * @property {(row: any) => boolean} [rowSelectable] Rows this returns false for cannot be selected (e.g. your own account).
 * @property {(row: any) => string} [rowSelectLabel]
 * @property {(id: string | number) => void} [onToggleRow]
 * @property {() => void} [onTogglePage]
 * @property {(ctx: any) => React.ReactNode} [renderSelectionBar] Renders the contextual selection toolbar (normally BulkBar). Called only while at least one row is selected.
 */
/**
 * @typedef {object} DataTableHandle
 * @property {() => void} focusSearch
 * @property {(rowKey: string | number) => void} focusRowAction
 */
/** @type {React.ForwardRefExoticComponent<DataTableProps & React.RefAttributes<DataTableHandle>>} */
declare const DataTable: React.ForwardRefExoticComponent<DataTableProps & React.RefAttributes<DataTableHandle>>;
export default DataTable;
