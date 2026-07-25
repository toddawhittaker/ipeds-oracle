export function pageHeaderState(pageEligibleIds: any, selected: any): "none" | "all" | "some";
export function selectionCount(selection: any, filteredEligibleIds: any): number;
export function partitionEligibility(selectedRows: any, action: any): {
    eligible: any[];
    skipped: {
        row: any;
        reason: any;
    }[];
};
export function selectedCountLabel(count: any, nouns: any): string;
export function pageSelectedNotice(count: any, nouns: any): string;
export function selectAllMatchingLabel(count: any, nouns: any): string;
export function allMatchingLabel(count: any, nouns: any): string;
export function reducedMatchingLabel(selected: any, total: any, nouns: any): string;
export function bulkConfirmSummary(action: any, counts: any): string;
export function bulkResultToast(action: any, result: any): {
    text: string;
    kind: string;
};
export function retainedSelectionAfterBulk(action: any, selectedRowIds: any, result: any, idField: any): any;
