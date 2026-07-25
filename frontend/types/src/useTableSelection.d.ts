export function useTableSelection(): {
    mode: string;
    selectedIds: Set<any>;
    selection: {
        mode: string;
        selectedIds: Set<any>;
    };
    toggleRow: (id: any, checked: any) => void;
    togglePage: (pageEligibleIds: any, checked: any) => void;
    selectAllMatching: () => void;
    selectExplicit: (ids: any) => void;
    clear: () => void;
    count: (filteredEligibleIds: any) => number;
    effectiveIds: (filteredEligibleIds: any) => Set<any>;
    isAllMatching: boolean;
};
