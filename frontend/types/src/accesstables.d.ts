export declare const strCmp: (a: any, b: any) => any;
export declare const numCmp: (a: any, b: any) => number;
export declare const PENDING_CONFIG: {
    fields: string[];
    comparators: {
        email: (a: any, b: any) => any;
        requested: (a: any, b: any) => number;
    };
    tiebreak: (r: any) => any;
    nouns: {
        one: string;
        many: string;
    };
};
export declare const BLOCKED_CONFIG: {
    fields: (string | ((r: any) => any))[];
    comparators: {
        email: (a: any, b: any) => any;
        requested: (a: any, b: any) => number;
        denied: (a: any, b: any) => number;
    };
    tiebreak: (r: any) => any;
    nouns: {
        one: string;
        many: string;
    };
};
