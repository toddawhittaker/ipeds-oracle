import React from "react";
export type ConfirmProviderProps = {
    /**
     * App subtree. Descendants call `useConfirm()`
     * to await a confirm/cancel modal. NOTE: confirm() is not awaitable by the
     * caller's onConfirm — returning a long promise from it leaves the modal
     * spinning.
     */
    children: React.ReactNode;
};
/**
 * @typedef {object} ConfirmProviderProps
 * @property {React.ReactNode} children App subtree. Descendants call `useConfirm()`
 *   to await a confirm/cancel modal. NOTE: confirm() is not awaitable by the
 *   caller's onConfirm — returning a long promise from it leaves the modal
 *   spinning.
 */
/** @param {ConfirmProviderProps} props */
export declare function ConfirmProvider({ children }: ConfirmProviderProps): React.JSX.Element;
export declare function useConfirm(): any;
