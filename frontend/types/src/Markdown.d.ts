/**
 * @typedef {object} MarkdownProps
 * @property {string} children The Markdown source, as a plain string. Rendered with
 *   GFM. Raw HTML is deliberately NOT enabled (no rehype-raw, default URL sanitizer
 *   intact) because this renders model output — keep it that way.
 * @property {number | string | null} [messageId] Message id used for the
 *   server-side full-result CSV re-run. Honoured only when the answer contains
 *   EXACTLY ONE table; otherwise the button falls back to exporting the
 *   transcribed rows.
 * @property {boolean} [resultsTruncated] True when the stored result rows were cut
 *   at the server row cap. Adds the "First N rows" caption and the warn-toned sort
 *   note.
 */
/** @param {MarkdownProps} props */
export default function Markdown({ children, messageId, resultsTruncated }: MarkdownProps): React.JSX.Element;
export type MarkdownProps = {
    /**
     * The Markdown source, as a plain string. Rendered with
     * GFM. Raw HTML is deliberately NOT enabled (no rehype-raw, default URL sanitizer
     * intact) because this renders model output — keep it that way.
     */
    children: string;
    /**
     * Message id used for the
     * server-side full-result CSV re-run. Honoured only when the answer contains
     * EXACTLY ONE table; otherwise the button falls back to exporting the
     * transcribed rows.
     */
    messageId?: number | string | null;
    /**
     * True when the stored result rows were cut
     * at the server row cap. Adds the "First N rows" caption and the warn-toned sort
     * note.
     */
    resultsTruncated?: boolean;
};
import React from "react";
