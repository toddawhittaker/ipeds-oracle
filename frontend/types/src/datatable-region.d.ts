/**
 * @param {boolean} hasActions whether the table renders a trailing actions cell
 * @param {Array<{sortable?: boolean}>} columns the column definitions
 * @returns {boolean} true when the scroll wrapper must be focusable
 */
export declare function needsScrollRegion(hasActions: boolean, columns: Array<{
    sortable?: boolean;
}>): boolean;
