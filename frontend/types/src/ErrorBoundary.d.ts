/**
 * @typedef {object} ErrorBoundaryProps
 * @property {React.ReactNode} children Subtree to guard. Any render error below
 *   this swaps the whole subtree for the reload card.
 */
/** @extends {React.Component<ErrorBoundaryProps, { error: Error | null }>} */
export default class ErrorBoundary extends React.Component<ErrorBoundaryProps, {
    error: Error | null;
}, any> {
    static getDerivedStateFromError(error: any): {
        error: any;
    };
    constructor(props: any);
    state: {
        error: any;
    };
    componentDidCatch(error: any, info: any): void;
    handleReload: () => void;
    render(): string | number | bigint | boolean | React.JSX.Element | Iterable<React.ReactNode> | Promise<string | number | bigint | boolean | React.ReactPortal | React.ReactElement<unknown, string | React.JSXElementConstructor<any>> | Iterable<React.ReactNode>>;
}
export type ErrorBoundaryProps = {
    /**
     * Subtree to guard. Any render error below
     * this swaps the whole subtree for the reload card.
     */
    children: React.ReactNode;
};
import React from "react";
