import React from "react";
export type SearchBoxProps = {
    /**
     * The current query. Controlled — the parent owns it.
     */
    value: string;
    /**
     * Called with the new query, and with
     * "" for both clear paths (the button and the Escape key).
     */
    onChange: (next: string) => void;
    placeholder: string;
    /**
     * Accessible name. Defaults to `placeholder`.
     */
    label?: string;
    /**
     * Applied to the input, for an external <label htmlFor>.
     */
    id?: string;
    /**
     * The parent's handle on
     * the input — DataTable exposes it as focusSearch() on its imperative handle.
     */
    inputRef?: React.RefObject<HTMLInputElement>;
};
/**
 * @typedef {object} SearchBoxProps
 * @property {string} value The current query. Controlled — the parent owns it.
 * @property {(next: string) => void} onChange Called with the new query, and with
 *   "" for both clear paths (the button and the Escape key).
 * @property {string} placeholder
 * @property {string} [label] Accessible name. Defaults to `placeholder`.
 * @property {string} [id] Applied to the input, for an external <label htmlFor>.
 * @property {React.RefObject<HTMLInputElement>} [inputRef] The parent's handle on
 *   the input — DataTable exposes it as focusSearch() on its imperative handle.
 */
/** @param {SearchBoxProps} props */
export default function SearchBox({ value, onChange, placeholder, label, id, inputRef }: SearchBoxProps): React.JSX.Element;
