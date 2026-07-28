import React from "react";
export type SuggestionsProps = {
    /**
     * Follow-up questions. Non-string and empty entries are
     * dropped; an empty list renders nothing.
     */
    items: string[];
    /**
     * Called with the clicked question —
     * the caller submits it as an ordinary follow-up turn.
     */
    onAsk: (question: string) => void;
    disabled?: boolean;
};
/**
 * @typedef {object} SuggestionsProps
 * @property {string[]} items Follow-up questions. Non-string and empty entries are
 *   dropped; an empty list renders nothing.
 * @property {(question: string) => void} onAsk Called with the clicked question —
 *   the caller submits it as an ordinary follow-up turn.
 * @property {boolean} [disabled]
 */
/** @param {SuggestionsProps} props */
export default function Suggestions({ items, onAsk, disabled }: SuggestionsProps): React.JSX.Element;
