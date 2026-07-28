import React from "react";
export type ToastProviderProps = {
    /**
     * App subtree. Descendants call `useToast()`
     * to push a transient message.
     */
    children: React.ReactNode;
};
/**
 * @typedef {object} ToastProviderProps
 * @property {React.ReactNode} children App subtree. Descendants call `useToast()`
 *   to push a transient message.
 */
/** @param {ToastProviderProps} props */
export declare function ToastProvider({ children }: ToastProviderProps): React.JSX.Element;
export declare function useToast(): any;
