// Repair the most common way an LLM breaks a Markdown table: a header/delimiter
// column-count mismatch (e.g. a 4-column header over a 3-cell `|:---:|:---|---:|`
// row). GFM silently refuses to render such a table, so it collapses into one
// raw-looking paragraph. We detect a header line followed by a delimiter line
// and rebuild the delimiter to match the header's column count (preserving any
// alignment cells the model did provide), and ensure a blank line precedes it.

function cellCount(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").length;
}

function alignCells(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

// A delimiter row is only dashes/colons/pipes/spaces and has at least one dash.
const DELIM_RE = /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/;

export function normalizeMarkdown(md) {
  if (!md || md.indexOf("|") === -1) return md;
  const lines = md.split("\n");
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const next = lines[i + 1];
    const isHeader = line.includes("|") && !DELIM_RE.test(line);
    if (isHeader && next != null && DELIM_RE.test(next) && next.includes("|")) {
      const cols = cellCount(line);
      const given = alignCells(next);
      const rebuilt = [];
      for (let c = 0; c < cols; c++) {
        const g = given[c];
        rebuilt.push(g && /-/.test(g) ? g : "---");
      }
      // GFM needs a blank line before the table; add one if the previous
      // emitted line is a non-empty, non-table line.
      const prev = out.length ? out[out.length - 1] : "";
      if (prev.trim() !== "" && !prev.includes("|")) out.push("");
      out.push(line);
      out.push("| " + rebuilt.join(" | ") + " |");
      i++; // consumed the delimiter line
    } else {
      out.push(line);
    }
  }
  return out.join("\n");
}
