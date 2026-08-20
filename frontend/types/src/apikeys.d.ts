export declare const KEY_PREFIX = "ipeds_mcp_";
export declare function maskedKey(row: any): string;
export declare function isRevoked(row: any): boolean;
export declare const KEY_CONFIG: {
    fields: string[];
    comparators: {
        created_at: (a: any, b: any) => number;
        email: (a: any, b: any) => any;
        label: (a: any, b: any) => any;
        last_used_at: (a: any, b: any) => number;
        status: (a: any, b: any) => number;
    };
    tiebreak: (r: any) => any;
    nouns: {
        one: string;
        many: string;
    };
};
export declare function sortByNewest(rows: any): any;
