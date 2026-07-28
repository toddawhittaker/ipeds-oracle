export declare function filterRows(rows: any, query: any, fields: any): any;
export declare function sortRows(rows: any, sortKey: any, sortDir: any, { comparators, tiebreak }: {
    comparators: any;
    tiebreak: any;
}): any;
export declare function paginate(rows: any, page: any, perPage: any): {
    slice: any;
    page: number;
    totalPages: number;
    start: number;
    end: any;
    total: any;
};
export declare function rangeLabel({ start, end, total }: {
    end: any;
    start: any;
    total: any;
}, { one, many }: {
    many: any;
    one: any;
}): string;
export declare function viewRows(rows: any, { query, sortKey, sortDir, page, perPage }: {
    page: any;
    perPage: any;
    query: any;
    sortDir: any;
    sortKey: any;
}, config: any): {
    slice: any;
    page: number;
    totalPages: number;
    start: number;
    end: any;
    total: any;
    label: string;
};
