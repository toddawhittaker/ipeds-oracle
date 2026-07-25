export function filterUsers(rows: any, query: any): any;
export function sortUsers(rows: any, sortKey: any, sortDir: any): any;
export function rangeLabel(pos: any): string;
export function viewUsers(rows: any, state: any): {
    label: string;
    slice: any;
    page: number;
    totalPages: number;
    start: number;
    end: any;
    total: any;
};
export namespace USER_CONFIG {
    export let fields: string[];
    export { COMPARATORS as comparators };
    export function tiebreak(r: any): any;
    export namespace nouns {
        let one: string;
        let many: string;
    }
}
export { paginate };
declare namespace COMPARATORS {
    function email(a: any, b: any): any;
    function note(a: any, b: any): any;
    function admin(a: any, b: any): number;
    function last_login(a: any, b: any): number;
}
import { paginate } from "./datatable.js";
