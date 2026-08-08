import React from "react";
export type ErrorBoundaryProps = {
    /**
     * Subtree to guard. Any render error below
     * this swaps the whole subtree for the reload card.
     */
    children: React.ReactNode;
    /**
     * Changing this CLEARS a caught error, so
     * navigating away from a broken route recovers. Deliberately a prop compared
     * in componentDidUpdate rather than a React `key`: a key would REMOUNT the
     * subtree on every change, and an admin sub-tab switch is a URL change — that
     * would destroy the three DataTables' search/sort/page/selection state, which
     * surviving a tab switch is an explicit contract of that screen. Resetting
     * state re-renders; it does not remount.
     */
    resetKey?: string;
};
/**
 * @typedef {object} ErrorBoundaryProps
 * @property {React.ReactNode} children Subtree to guard. Any render error below
 *   this swaps the whole subtree for the reload card.
 * @property {string} [resetKey] Changing this CLEARS a caught error, so
 *   navigating away from a broken route recovers. Deliberately a prop compared
 *   in componentDidUpdate rather than a React `key`: a key would REMOUNT the
 *   subtree on every change, and an admin sub-tab switch is a URL change — that
 *   would destroy the three DataTables' search/sort/page/selection state, which
 *   surviving a tab switch is an explicit contract of that screen. Resetting
 *   state re-renders; it does not remount.
 */
/** @extends {React.Component<ErrorBoundaryProps, { error: Error | null }>} */
export default class ErrorBoundary extends React.Component<ErrorBoundaryProps, {
    error: Error | null;
}> {
    state: {
        error: any;
    };
    constructor(props: any);
    static getDerivedStateFromError(error: any): {
        error: any;
    };
    componentDidUpdate(prevProps: any): void;
    componentDidCatch(error: any, info: any): void;
    handleReload: () => void;
    handleDismiss: () => void;
    render(): string | number | bigint | boolean | React.JSX.Element | Iterable<React.ReactNode> | Promise<string | number | bigint | boolean | Iterable<React.ReactNode> | React.ReactElement<unknown, string | React.JSXElementConstructor<any>> | React.ReactPortal>;
}
