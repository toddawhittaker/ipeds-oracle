export declare function canCaptionTruncation({ truncated, tableCount, messageId }: {
    messageId: any;
    tableCount: any;
    truncated: any;
}): boolean;
export declare function truncationCaption(truncated: any, rowCap: any): string;
export declare function sortScopeNote({ truncated, sorted, rowsShown, rowCap }: {
    rowCap: any;
    rowsShown: any;
    sorted: any;
    truncated: any;
}): string;
export declare function sortNoteTone(truncated: any): "muted" | "warn";
export declare function csvLabel({ serverSide, rowsShown }: {
    rowsShown: any;
    serverSide: any;
}): string;
/**
 * @param {object} [verdict] The message's persisted table-grounding verdict.
 * @param {string} [verdict.status] Server verdict: matched|partial|unmatched|no_table|unchecked.
 *   `unchecked`/`no_table` render nothing — nothing was compared, so neither tone applies.
 * @param {number} [verdict.cellsChecked] How many numeric MEASURE cells were graded; every
 *   note states a count, so a verdict without it renders nothing.
 * @param {number} [verdict.cellsMatched] How many of those reproduced. Required for the
 *   caution, which leads with the number that did NOT.
 * @param {boolean} [verdict.hasSql] Whether THIS answer ran a query. False for a turn
 *   that reshapes an earlier table from context: same claim, but the rows came from
 *   the earlier turn, so the note says so instead of pointing at absent SQL.
 * @returns {{tone: string, text: string, title: string} | null} The line to render, or
 *   null for "say nothing" — the default for every unrecognised or malformed verdict.
 */
export declare function tableTrustNote({ status, cellsChecked, cellsMatched, hasSql }?: {
    status?: string;
    cellsChecked?: number;
    cellsMatched?: number;
    hasSql?: boolean;
}): {
    tone: string;
    text: string;
    title: string;
} | null;
export declare function csvErrorMessage(status: any, detail: any): any;
