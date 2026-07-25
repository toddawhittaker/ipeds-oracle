import React from "react";
import { MarkdownTextarea } from "ipeds-query-web";

// A real <textarea> with a colored <pre> mirror behind it — the value is always
// the RAW Markdown string, so undo/redo, plain paste and IME all still work.
// Highlighting is COLOR-ONLY: never weight or size, which would shift glyph
// widths and drift the caret off the overlay.
//
// Controlled component: these stories pass a static value and a no-op onChange,
// which is what shows the highlighting at rest.

const noop = () => {};

export const Highlighted = () => (
  <MarkdownTextarea
    value={
      "Compare **nursing** and _computer science_ bachelor's degrees\n" +
      "for the last five years, and show the trend for `Ohio` only."
    }
    onChange={noop}
    aria-label="Ask a question"
  />
);

export const WithStructure = () => (
  <MarkdownTextarea
    value={
      "### Scope\n" +
      "- bachelor's only\n" +
      "- first majors\n" +
      "\n" +
      "Use the [IPEDS glossary](https://nces.ed.gov/ipeds) definitions."
    }
    onChange={noop}
    aria-label="Ask a question"
  />
);

export const Placeholder = () => (
  <MarkdownTextarea
    value=""
    onChange={noop}
    placeholder="Ask about U.S. colleges and universities…"
    maxLength={4000}
    aria-label="Ask a question"
  />
);
