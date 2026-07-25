/**
 * @typedef {object} CopyMenuProps
 * @property {() => void} onCopyMarkdown
 * @property {() => void} onCopyHtml
 * @property {boolean} [copied] Flips the trigger to a "Copied!" check. The caller
 *   owns the reset timer.
 */
/** @param {CopyMenuProps} props */
export default function CopyMenu({ onCopyMarkdown, onCopyHtml, copied }: CopyMenuProps): React.JSX.Element;
export type CopyMenuProps = {
    onCopyMarkdown: () => void;
    onCopyHtml: () => void;
    /**
     * Flips the trigger to a "Copied!" check. The caller
     * owns the reset timer.
     */
    copied?: boolean;
};
import React from "react";
