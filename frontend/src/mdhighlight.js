// Cosmetic Markdown highlighter for the composer overlay (MarkdownTextarea.jsx).
//
// PURE and LOSSLESS: highlight(src) -> [{ text, cls? }] whose `text` values
// concatenate back to `src` EXACTLY. The composer's canonical value is always the
// raw textarea string; this layer only colors a mirror of it. So a tokenization
// miss is a purely visual nit — never a data change. The concat-equals-input
// invariant is the key regression guard (mdhighlight.test.js).
//
// It is a best-effort line lexer, not a CommonMark parser: enough to dim markers
// and tint structure while typing, deliberately not exhaustive.

const HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const HEADING = /^(#{1,6}\s+)(.*)$/;              // needs a space, so "#foo" is not one
const QUOTE = /^(\s*>+\s?)(.*)$/;
const LIST = /^(\s*(?:[-*+]|\d{1,9}[.)])\s+)(.*)$/;
const FENCE = /^\s*(?:```|~~~)/;

// Inline scan: emits delimiter segments as "marker" and content as its role class,
// preserving every character exactly. Order = precedence (code, link, strong,
// strike, emph); an unmatched delimiter stays plain (which is exactly how an
// "invalid" construct should read — literal).
function inline(text, push) {
  const n = text.length;
  let i = 0;
  let plain = 0;
  const flush = (upto) => { if (upto > plain) push(text.slice(plain, upto)); };
  while (i < n) {
    const c = text[i];

    if (c === "`") {                                   // `code`
      const j = text.indexOf("`", i + 1);
      if (j > i) {
        flush(i);
        push("`", "marker"); push(text.slice(i + 1, j), "code"); push("`", "marker");
        i = j + 1; plain = i; continue;
      }
    }

    if (c === "[") {                                   // [text](url)
      const lm = /^\[([^\]]*)\]\(([^)\s]*)\)/.exec(text.slice(i));
      if (lm) {
        flush(i);
        push("[", "marker"); push(lm[1], "link"); push("]", "marker");
        push("(", "marker"); push(lm[2], "linkurl"); push(")", "marker");
        i += lm[0].length; plain = i; continue;
      }
    }

    if ((c === "*" || c === "_") && text[i + 1] === c) {  // **strong** / __strong__
      const close = text.indexOf(c + c, i + 2);
      if (close > i + 1) {
        const content = text.slice(i + 2, close);
        if (content.length) {
          flush(i);
          push(c + c, "marker"); push(content, "strong"); push(c + c, "marker");
          i = close + 2; plain = i; continue;
        }
      }
    }

    if (c === "~" && text[i + 1] === "~") {              // ~~strike~~
      const close = text.indexOf("~~", i + 2);
      if (close > i + 1) {
        const content = text.slice(i + 2, close);
        if (content.length) {
          flush(i);
          push("~~", "marker"); push(content, "strike"); push("~~", "marker");
          i = close + 2; plain = i; continue;
        }
      }
    }

    if (c === "*" || c === "_") {                        // *emph* / _emph_
      const close = text.indexOf(c, i + 1);
      if (close > i) {
        const content = text.slice(i + 1, close);
        if (content.length && !/\s/.test(content[0])) {
          flush(i);
          push(c, "marker"); push(content, "emph"); push(c, "marker");
          i = close + 1; plain = i; continue;
        }
      }
    }

    i++;
  }
  flush(n);
}

// -> [{ text, cls? }]; join of every `text` === src exactly.
export function highlight(src) {
  const out = [];
  const push = (text, cls) => { if (text) out.push(cls ? { text, cls } : { text }); };
  if (src == null) return out;
  // split keeps the "\n" tokens so newlines are preserved verbatim.
  const parts = String(src).split(/(\n)/);
  let inFence = false;
  for (const part of parts) {
    if (part === "\n") { push("\n"); continue; }
    if (part === "") continue;
    if (FENCE.test(part)) { push(part, "fence"); inFence = !inFence; continue; }
    if (inFence) { push(part, "code"); continue; }
    if (HR.test(part)) { push(part, "hr"); continue; }
    let m;
    if ((m = HEADING.exec(part))) { push(m[1], "marker"); push(m[2], "heading"); continue; }
    if ((m = QUOTE.exec(part))) { push(m[1], "quote"); inline(m[2], push); continue; }
    if ((m = LIST.exec(part))) { push(m[1], "list"); inline(m[2], push); continue; }
    inline(part, push);
  }
  return out;
}
