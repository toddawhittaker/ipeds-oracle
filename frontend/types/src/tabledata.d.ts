export declare function extractTable(node: any): {
    headers: any[];
    rows: any[];
    cellNodes: any[];
};
export declare function parseNum(s: any): number;
export declare function columnIsNumeric(rows: any, col: any): boolean;
export declare function sortedIndices(rows: any, col: any, dir: any, numeric: any): any;
export declare function sortRows(rows: any, col: any, dir: any, numeric: any): any;
export declare function chartSpecFromTable(headers: any, rows: any): {
    type: string;
    x: any;
    y: any[];
    data: any;
};
export declare function toCsv(headers: any, rows: any): string;
export declare function downloadCsv(headers: any, rows: any, filename?: string): void;
export declare function countMarkdownTables(src: any): number;
export declare function downloadServerCsv(messageId: any, cols: any): Promise<void>;
