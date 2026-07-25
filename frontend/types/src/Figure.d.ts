/**
 * @typedef {object} FigureProps
 * @property {{ value: string | number, unit?: string, label: string, source?: string }} spec
 *   The hero statistic. `value` and `label` are both required — a spec missing
 *   either renders nothing at all.
 * @property {"exact" | "rounded" | "derived" | "ungrounded" | "no_figure" | "malformed" | "unchecked"} [grounding]
 *   Server-side grounding verdict. ONLY "exact" | "rounded" | "derived" render the
 *   quiet "✓ verified" mark; every other value (and undefined) renders NO mark and
 *   NO warning. Positive-only by design — never add an "unverified" state.
 */
/** @param {FigureProps} props */
export default function Figure({ spec, grounding }: FigureProps): React.JSX.Element;
export type FigureProps = {
    /**
     *   The hero statistic. `value` and `label` are both required — a spec missing
     *   either renders nothing at all.
     */
    spec: {
        value: string | number;
        unit?: string;
        label: string;
        source?: string;
    };
    /**
     * Server-side grounding verdict. ONLY "exact" | "rounded" | "derived" render the
     * quiet "✓ verified" mark; every other value (and undefined) renders NO mark and
     * NO warning. Positive-only by design — never add an "unverified" state.
     */
    grounding?: "exact" | "rounded" | "derived" | "ungrounded" | "no_figure" | "malformed" | "unchecked";
};
import React from "react";
