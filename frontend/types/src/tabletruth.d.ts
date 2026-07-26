export function canCaptionTruncation({ truncated, tableCount, messageId }: {
    truncated: any;
    tableCount: any;
    messageId: any;
}): boolean;
export function truncationCaption(truncated: any): "" | "First 200 rows · the full result is larger";
export function sortScopeNote({ truncated, sorted, rowsShown }: {
    truncated: any;
    sorted: any;
    rowsShown: any;
}): string;
export function sortNoteTone(truncated: any): "warn" | "muted";
export function csvLabel({ serverSide, rowsShown }: {
    serverSide: any;
    rowsShown: any;
}): string;
/**
 * @param {object} [verdict] The message's persisted table-grounding verdict.
 * @param {string} [verdict.status] Server verdict: matched|partial|unmatched|no_table|unchecked.
 *   `unchecked`/`no_table` render nothing — nothing was compared, so neither tone applies.
 * @param {number} [verdict.cellsChecked] How many numeric MEASURE cells were graded; every
 *   note states a count, so a verdict without it renders nothing.
 * @param {number} [verdict.cellsMatched] How many of those reproduced. Required for the
 *   caution, which leads with the number that did NOT.
 * @returns {{tone: string, text: string, title: string} | null} The line to render, or
 *   null for "say nothing" — the default for every unrecognised or malformed verdict.
 */
export function tableTrustNote({ status, cellsChecked, cellsMatched }?: {
    status?: string;
    cellsChecked?: number;
    cellsMatched?: number;
}): {
    tone: string;
    text: string;
    title: string;
} | null;
export function csvErrorMessage(status: any, detail: any): any;
export const ROW_CAP: 200;
