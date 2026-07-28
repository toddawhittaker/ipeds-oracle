export declare function pageHeaderState(pageEligibleIds: any, selected: any): "all" | "none" | "some";
export declare function selectionCount(selection: any, filteredEligibleIds: any): number;
export declare function partitionEligibility(selectedRows: any, action: any): {
    eligible: any[];
    skipped: {
        row: any;
        reason: any;
    }[];
};
export declare function selectedCountLabel(count: any, nouns: any): string;
export declare function pageSelectedNotice(count: any, nouns: any): string;
export declare function selectAllMatchingLabel(count: any, nouns: any): string;
export declare function allMatchingLabel(count: any, nouns: any): string;
export declare function reducedMatchingLabel(selected: any, total: any, nouns: any): string;
export declare function bulkConfirmSummary(action: any, counts: any): string;
export declare function bulkResultToast(action: any, result: any): {
    text: string;
    kind: string;
};
export declare function retainedSelectionAfterBulk(action: any, selectedRowIds: any, result: any, idField: any): any;
