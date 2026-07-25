/**
 * @typedef {object} ToastProviderProps
 * @property {React.ReactNode} children App subtree. Descendants call `useToast()`
 *   to push a transient message.
 */
/** @param {ToastProviderProps} props */
export function ToastProvider({ children }: ToastProviderProps): React.JSX.Element;
export function useToast(): any;
export type ToastProviderProps = {
    /**
     * App subtree. Descendants call `useToast()`
     * to push a transient message.
     */
    children: React.ReactNode;
};
import React from "react";
