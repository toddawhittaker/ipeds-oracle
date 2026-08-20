import React from "react";
import { IconClose } from "./icons.jsx";

// The admin search field: a `type="search"` input with an in-field clear button.
//
// Extracted from DataTable.jsx when Admin -> Logs needed the same thing. Logs
// had been relying on the BROWSER's native type=search clear control, which is
// not the same control at all: it renders as a bold blue ✕ rather than the app's
// muted glyph, it is not exposed as a button to assistive tech (a role query
// finds nothing), it does not exist in Firefox, and it takes no part in the
// focus contract below. Two screens showing two different clear affordances is
// the kind of drift a shared component prevents and a copy re-opens.
//
// The CSS that suppresses the native control is scoped to `.searchwrap
// .logsearch` (styles.css), so it applies wherever this component renders and
// nowhere else — a bare `type="search"` elsewhere in the app keeps its default.

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
export default function SearchBox({ value, onChange, placeholder, label, id,
                                    inputRef }) {
  const ownRef = React.useRef(null);
  const ref = inputRef || ownRef;
  return (
    <div className="searchwrap">
      <input id={id} ref={ref} type="search" className="logsearch"
             placeholder={placeholder} value={value}
             aria-label={label || placeholder}
             onChange={(e) => onChange(e.target.value)}
             onKeyDown={(e) => {
               // Escape clears an active search in place (the standard
               // search-field affordance); focus stays in the box. Handled
               // explicitly rather than left to the browser, because only
               // Chromium does it natively and only for type=search.
               if (e.key === "Escape" && value) {
                 e.preventDefault();
                 onChange("");
               }
             }} />
      {/* Rendered only when there is something to clear, so the field is not
          carrying a dead control the whole time it is empty. */}
      {value && (
        <button type="button" className="search-clear" aria-label="Clear search"
                onClick={() => { onChange(""); ref.current?.focus(); }}>
          <IconClose size={14} />
        </button>
      )}
    </div>
  );
}
