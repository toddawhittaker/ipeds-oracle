/**
 * @typedef {object} WordmarkProps
 * @property {boolean} [showIcon] Draw the Column mark before the type. The wordmark
 *   is inline SVG drawn from the theme tokens — never a PNG pair — so light/dark
 *   comes from one source.
 */
/** @param {WordmarkProps} props */
export default function Wordmark({ showIcon }: WordmarkProps): import("react").JSX.Element;
export type WordmarkProps = {
    /**
     * Draw the Column mark before the type. The wordmark
     * is inline SVG drawn from the theme tokens — never a PNG pair — so light/dark
     * comes from one source.
     */
    showIcon?: boolean;
};
