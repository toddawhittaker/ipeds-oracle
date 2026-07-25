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
export function csvErrorMessage(status: any, detail: any): any;
export const ROW_CAP: 200;
