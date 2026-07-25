export function extractTable(node: any): {
    headers: any[];
    rows: any[];
    cellNodes: any[];
};
export function rowCells(trNode: any): any;
export function parseNum(s: any): number;
export function columnIsNumeric(rows: any, col: any): boolean;
export function sortedIndices(rows: any, col: any, dir: any, numeric: any): any;
export function sortRows(rows: any, col: any, dir: any, numeric: any): any;
export function chartSpecFromTable(headers: any, rows: any): {
    type: string;
    x: any;
    y: any[];
    data: any;
};
export function toCsv(headers: any, rows: any): string;
export function downloadCsv(headers: any, rows: any, filename?: string): void;
export function countMarkdownTables(src: any): number;
export function downloadServerCsv(messageId: any, cols: any): Promise<void>;
