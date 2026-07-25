/**
 * @typedef {object} ConfirmProviderProps
 * @property {React.ReactNode} children App subtree. Descendants call `useConfirm()`
 *   to await a confirm/cancel modal. NOTE: confirm() is not awaitable by the
 *   caller's onConfirm — returning a long promise from it leaves the modal
 *   spinning.
 */
/** @param {ConfirmProviderProps} props */
export function ConfirmProvider({ children }: ConfirmProviderProps): React.JSX.Element;
export function useConfirm(): any;
export type ConfirmProviderProps = {
    /**
     * App subtree. Descendants call `useConfirm()`
     * to await a confirm/cancel modal. NOTE: confirm() is not awaitable by the
     * caller's onConfirm — returning a long promise from it leaves the modal
     * spinning.
     */
    children: React.ReactNode;
};
import React from "react";
