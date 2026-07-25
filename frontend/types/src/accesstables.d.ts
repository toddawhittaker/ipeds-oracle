export function strCmp(a: any, b: any): any;
export function numCmp(a: any, b: any): number;
export namespace PENDING_CONFIG {
    let fields: string[];
    namespace comparators {
        function email(a: any, b: any): any;
        function requested(a: any, b: any): number;
    }
    function tiebreak(r: any): any;
    namespace nouns {
        let one: string;
        let many: string;
    }
}
export namespace BLOCKED_CONFIG {
    let fields_1: (string | ((r: any) => any))[];
    export { fields_1 as fields };
    export namespace comparators_1 {
        export function email_1(a: any, b: any): any;
        export { email_1 as email };
        export function requested_1(a: any, b: any): number;
        export { requested_1 as requested };
        export function denied(a: any, b: any): number;
    }
    export { comparators_1 as comparators };
    export function tiebreak_1(r: any): any;
    export { tiebreak_1 as tiebreak };
    export namespace nouns_1 {
        let one_1: string;
        export { one_1 as one };
        let many_1: string;
        export { many_1 as many };
    }
    export { nouns_1 as nouns };
}
