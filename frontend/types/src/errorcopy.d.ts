export declare const CRASH_TITLE = "Something went wrong";
/**
 * Choose the crash card's wording and actions.
 * @param {{ liveTurn?: boolean }} state
 * @returns {{ body: string, primary: { label: string, reload: boolean },
 *             secondary: { label: string, reload: boolean } | null }}
 */
export declare function boundaryFallback({ liveTurn }?: {
    liveTurn?: boolean;
}): {
    body: string;
    primary: {
        label: string;
        reload: boolean;
    };
    secondary: {
        label: string;
        reload: boolean;
    } | null;
};
