export type MarkdownTextareaBaseProps = {
    /**
     * Controlled value — always the RAW Markdown string. The
     * highlighting is a colored <pre> mirror behind a transparent <textarea>, never a
     * rich-text model.
     */
    value: string;
    onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
    onKeyDown?: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
    placeholder?: string;
    id?: string;
    className?: string;
    autoFocus?: boolean;
    /**
     * Hard character cap. The composer keeps this in
     * sync with the server's MAX_QUESTION_LEN (4000).
     */
    maxLength?: number;
};
export type MarkdownTextareaProps = MarkdownTextareaBaseProps & {
    "aria-label"?: string;
};
/**
 * @typedef {object} MarkdownTextareaBaseProps
 * @property {string} value Controlled value — always the RAW Markdown string. The
 *   highlighting is a colored <pre> mirror behind a transparent <textarea>, never a
 *   rich-text model.
 * @property {(e: React.ChangeEvent<HTMLTextAreaElement>) => void} onChange
 * @property {(e: React.KeyboardEvent<HTMLTextAreaElement>) => void} [onKeyDown]
 * @property {string} [placeholder]
 * @property {string} [id]
 * @property {string} [className]
 * @property {boolean} [autoFocus]
 * @property {number} [maxLength] Hard character cap. The composer keeps this in
 *   sync with the server's MAX_QUESTION_LEN (4000).
 */
/**
 * `@property` cannot express a hyphenated key, so aria-label is intersected in.
 * @typedef {MarkdownTextareaBaseProps & { "aria-label"?: string }} MarkdownTextareaProps
 */
/** @type {React.ForwardRefExoticComponent<MarkdownTextareaProps & React.RefAttributes<HTMLTextAreaElement>>} */
declare const MarkdownTextarea: React.ForwardRefExoticComponent<MarkdownTextareaProps & React.RefAttributes<HTMLTextAreaElement>>;
export default MarkdownTextarea;
