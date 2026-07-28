import React from "react";
export type SqlBlockProps = {
    /**
     * The SQL text. Every SQL surface in the product renders
     * through this component.
     */
    code: string;
    className?: string;
    /**
     * Pretty-print before highlighting. Pass false for
     * author-written ```sql fences (highlight only, leave their formatting alone).
     */
    format?: boolean;
};
/**
 * @typedef {object} SqlBlockProps
 * @property {string} code The SQL text. Every SQL surface in the product renders
 *   through this component.
 * @property {string} [className]
 * @property {boolean} [format] Pretty-print before highlighting. Pass false for
 *   author-written ```sql fences (highlight only, leave their formatting alone).
 */
/** @param {SqlBlockProps} props */
export default function SqlBlock({ code, className, format }: SqlBlockProps): React.JSX.Element;
