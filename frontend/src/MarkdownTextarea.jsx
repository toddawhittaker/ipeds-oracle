import { forwardRef, useCallback, useEffect, useRef } from "react";
import { highlight } from "./mdhighlight.js";

// A Markdown-highlighting text input for the chat composer. It stays a REAL
// <textarea> (so its value is always the plain Markdown source, and undo/redo,
// copy, plain-text paste, IME, and every keyboard behavior come for free) with a
// transparent text color, layered over a <pre> that mirrors the same text with
// dimmed markers / tinted structure (mdhighlight.js). The two share identical
// metrics so the caret sits exactly over the rendered glyphs — which is why the
// highlight is COLOR-only (weight/size would shift glyph widths and drift it).
//
// The forwarded `ref` points at the underlying <textarea>, so the composer's
// focus management, typeahead redirect, and the e2e selectors (getByPlaceholder /
// .fill() / toHaveValue) keep working unchanged.
const MAX_H = 200;

const MarkdownTextarea = forwardRef(function MarkdownTextarea(
  { value, onChange, onKeyDown, placeholder, id, className = "" }, ref) {
  const taRef = useRef(null);
  const preRef = useRef(null);

  const setRefs = useCallback((node) => {
    taRef.current = node;
    if (typeof ref === "function") ref(node);
    else if (ref) ref.current = node;
  }, [ref]);

  // Auto-grow to fit content up to MAX_H, then the textarea scrolls and the
  // overlay follows (syncScroll). Replaces the old Chat.jsx auto-grow effect.
  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, MAX_H) + "px";
  }, [value]);

  const syncScroll = () => {
    const ta = taRef.current;
    const pre = preRef.current;
    if (ta && pre) { pre.scrollTop = ta.scrollTop; pre.scrollLeft = ta.scrollLeft; }
  };

  const segments = highlight(value || "");
  return (
    <div className="md-editor">
      {/* The colored mirror — decorative, hidden from assistive tech (the
          textarea is the real, announced control). A trailing newline keeps the
          last/blank line height in step with the textarea (excess is clipped). */}
      <pre ref={preRef} className="md-editor-hl" aria-hidden="true">
        {segments.map((s, i) => (s.cls
          ? <span key={i} className={"md-hl-" + s.cls}>{s.text}</span>
          : <span key={i}>{s.text}</span>))}
        {"\n"}
      </pre>
      <textarea
        ref={setRefs}
        id={id}
        rows={1}
        className={"md-editor-ta thin-scroll " + className}
        value={value}
        placeholder={placeholder}
        onChange={onChange}
        onKeyDown={onKeyDown}
        onScroll={syncScroll}
        spellCheck="true"
      />
    </div>
  );
});

export default MarkdownTextarea;
