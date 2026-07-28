import React from "react";
export type ChartProps = {
    /**
     * Data and axes. Renders null unless `data` is a non-empty array and `x` is set.
     */
    spec: {
        x: string;
        y: string | string[];
        data: Array<Record<string, string | number>>;
        type?: "line" | "bar";
        title?: string;
        xLabel?: string;
        yLabel?: string;
    };
    /**
     * True only inside ChartModal — hides this chart's
     * own maximize control.
     */
    inModal?: boolean;
    /**
     * Initial chart type. Defaults from
     * `spec.type` (bar if it says bar, else line).
     */
    initialType?: "line" | "bar";
    /**
     * Start with the least-squares trend line drawn.
     * It only appears on a LINE chart whose x-axis is time-like
     * (year/date/month/quarter/day) with >= 3 points — a fitted slope across
     * categories is meaningless.
     */
    initialTrend?: boolean;
    /**
     * Start with per-point data labels shown.
     */
    initialLabels?: boolean;
};
/**
 * Sub-shapes are INLINE on purpose — a named @typedef emits into the published
 * design-system contract as a reference the file never defines. See DataTable.jsx.
 *
 * @typedef {object} ChartProps
 * @property {{ x: string, y: string | string[], data: Array<Record<string, string | number>>, type?: "line" | "bar", title?: string, xLabel?: string, yLabel?: string }} spec
 *   Data and axes. Renders null unless `data` is a non-empty array and `x` is set.
 * @property {boolean} [inModal] True only inside ChartModal — hides this chart's
 *   own maximize control.
 * @property {"line" | "bar"} [initialType] Initial chart type. Defaults from
 *   `spec.type` (bar if it says bar, else line).
 * @property {boolean} [initialTrend] Start with the least-squares trend line drawn.
 *   It only appears on a LINE chart whose x-axis is time-like
 *   (year/date/month/quarter/day) with >= 3 points — a fitted slope across
 *   categories is meaningless.
 * @property {boolean} [initialLabels] Start with per-point data labels shown.
 */
/** @param {ChartProps} props */
export default function Chart({ spec, inModal, initialType, initialTrend, initialLabels }: ChartProps): React.JSX.Element;
