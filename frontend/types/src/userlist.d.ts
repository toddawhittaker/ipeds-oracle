import { paginate } from "./datatable.js";
export declare const USER_CONFIG: {
    fields: string[];
    comparators: {
        email: (a: any, b: any) => any;
        note: (a: any, b: any) => any;
        admin: (a: any, b: any) => number;
        last_active: (a: any, b: any) => number;
    };
    tiebreak: (r: any) => any;
    nouns: {
        one: string;
        many: string;
    };
};
export declare function filterUsers(rows: any, query: any): any;
export declare function sortUsers(rows: any, sortKey: any, sortDir: any): any;
export declare function rangeLabel(pos: any): string;
export declare function viewUsers(rows: any, state: any): {
    slice: any;
    page: number;
    totalPages: number;
    start: number;
    end: any;
    total: any;
    label: string;
};
export { paginate };
