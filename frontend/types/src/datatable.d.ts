export function filterRows(rows: any, query: any, fields: any): any;
export function sortRows(rows: any, sortKey: any, sortDir: any, { comparators, tiebreak }: {
    comparators: any;
    tiebreak: any;
}): any;
export function paginate(rows: any, page: any, perPage: any): {
    slice: any;
    page: number;
    totalPages: number;
    start: number;
    end: any;
    total: any;
};
export function rangeLabel({ start, end, total }: {
    start: any;
    end: any;
    total: any;
}, { one, many }: {
    one: any;
    many: any;
}): string;
export function viewRows(rows: any, { query, sortKey, sortDir, page, perPage }: {
    query: any;
    sortKey: any;
    sortDir: any;
    page: any;
    perPage: any;
}, config: any): {
    label: string;
    slice: any;
    page: number;
    totalPages: number;
    start: number;
    end: any;
    total: any;
};
