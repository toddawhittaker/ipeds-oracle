/**
 * @typedef {object} ChartModalProps
 * @property {{ x: string, y: string | string[], data: Array<Record<string, string | number>>, type?: "line" | "bar", title?: string, xLabel?: string, yLabel?: string }} spec
 *   Same spec the opener Chart was rendering. Inline, not a named typedef — see
 *   the note in DataTable.jsx.
 * @property {"line" | "bar"} [initialType] Carry the opener's current view so
 *   maximizing doesn't reset it.
 * @property {boolean} [initialTrend]
 * @property {boolean} [initialLabels]
 * @property {() => void} onClose Called on EVERY dismissal path (Close button,
 *   Escape, overlay click). Required — the dialog never closes itself, and focus
 *   returns to the opener on unmount.
 */
/** @param {ChartModalProps} props */
export default function ChartModal({ spec, initialType, initialTrend, initialLabels, onClose }: ChartModalProps): any;
export type ChartModalProps = {
    /**
     *   Same spec the opener Chart was rendering. Inline, not a named typedef — see
     *   the note in DataTable.jsx.
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
     * Carry the opener's current view so
     * maximizing doesn't reset it.
     */
    initialType?: "line" | "bar";
    initialTrend?: boolean;
    initialLabels?: boolean;
    /**
     * Called on EVERY dismissal path (Close button,
     * Escape, overlay click). Required — the dialog never closes itself, and focus
     * returns to the opener on unmount.
     */
    onClose: () => void;
};
