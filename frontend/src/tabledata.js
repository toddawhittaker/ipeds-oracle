// Helpers for turning a rendered markdown table into data — used for per-table
// CSV export and "Chart this". Extraction walks the hast `node` react-markdown
// hands to the table component, so it works on the actual rendered content.

function hastText(node) {
  if (!node) return "";
  if (node.type === "text") return node.value || "";
  return (node.children || []).map(hastText).join("");
}

// -> { headers: string[], rows: string[][], cellNodes: hastNode[][] }.
// `rows` is the trimmed TEXT of each body cell (what sort/CSV/compare use);
// `cellNodes` is the parallel hast <td> node per cell (what the display renders
// through a minimal inline renderer, so a link/bold/code cell keeps its markup).
export function extractTable(node) {
  let headers = [];
  const rows = [];
  const cellNodes = [];
  const walk = (n) => {
    for (const c of n.children || []) {
      if (c.tagName === "tr") {
        const els = (c.children || []).filter((x) => x.tagName === "th" || x.tagName === "td");
        const texts = els.map((x) => hastText(x).trim());
        if (els.some((x) => x.tagName === "th")) headers = texts;
        else { rows.push(texts); cellNodes.push(els); }
      } else {
        walk(c);
      }
    }
  };
  walk(node);
  return { headers, rows, cellNodes };
}

// The th/td cell elements of a rendered <tr> hast node, each with its tag and
// trimmed text — used by compare mode's `tr` override to read a row's entity
// label and to tell a header row (th cells) from a body row (td cells).
export function rowCells(trNode) {
  return (trNode?.children || [])
    .filter((x) => x.tagName === "th" || x.tagName === "td")
    .map((x) => ({ tag: x.tagName, text: hastText(x).trim() }));
}

export function parseNum(s) {
  if (s == null) return NaN;
  const t = String(s).replace(/[$,%\s]/g, "");
  if (t === "" || t === "-") return NaN;
  const n = Number(t);
  return Number.isFinite(n) ? n : NaN;
}

// Whether column `col` should sort numerically (same ≥60%-parse heuristic used
// for chart inference), so "1,234" orders after "999" rather than lexically.
export function columnIsNumeric(rows, col) {
  if (!rows.length) return false;
  let ok = 0;
  for (const r of rows) if (!Number.isNaN(parseNum(r[col]))) ok++;
  return ok / rows.length >= 0.6;
}

// A column (by index) is numeric if most of its cells parse as numbers.
function numericCols(headers, rows) {
  const out = [];
  for (let c = 0; c < headers.length; c++) if (columnIsNumeric(rows, c)) out.push(c);
  return out;
}

// A STABLE, numeric-aware sort ORDER (a row-index permutation) by column `col`
// in `dir` ('asc'|'desc'). col/dir null → identity order. Blank / non-numeric
// cells in a numeric column sort to the END in both directions. Returning the
// permutation (not reordered rows) lets the display reorder BOTH the text rows
// and the parallel cell-nodes in lockstep. Display-only — never the CSV.
export function sortedIndices(rows, col, dir, numeric) {
  const idx = rows.map((_, i) => i);
  if (col == null || (dir !== "asc" && dir !== "desc")) return idx;
  const sign = dir === "desc" ? -1 : 1;
  const cmp = (va, vb) => {
    if (numeric) {
      const na = parseNum(va), nb = parseNum(vb);
      const aN = Number.isNaN(na), bN = Number.isNaN(nb);
      if (aN || bN) return aN && bN ? 0 : (aN ? 1 : -1); // blanks last, both dirs
      return sign * (na - nb);
    }
    return sign * String(va ?? "").localeCompare(
      String(vb ?? ""), undefined, { numeric: true, sensitivity: "base" });
  };
  return idx.sort((a, b) => cmp(rows[a][col], rows[b][col]) || a - b);
}

// The same sort as a NEW array of rows (kept for its unit tests / any text-only
// caller); the display uses sortedIndices directly.
export function sortRows(rows, col, dir, numeric) {
  return sortedIndices(rows, col, dir, numeric).map((i) => rows[i]);
}

// A "dimension" column — a rank/index (1..n or named like one) or an identifier
// (year, id, code, cip, unitid, zip, fips). These are never real metrics, so we
// never plot them as a data series; a year/etc. can still serve as the x-axis
// (handled below) but must not appear as a bogus second line/bar.
function isDimensionCol(header, vals) {
  const h = (header || "").trim();
  if (/^(rank|#|no\.?|num|index|row|position|place|year|yr|fy|id|.*[ _]?id|code|.*code|cip|unitid|opeid|ipeds|zip|fips)$/i.test(h)) {
    return true;
  }
  const nums = vals.map(parseNum);
  if (nums.some(Number.isNaN)) return false;
  const sorted = [...nums].sort((a, b) => a - b);
  return sorted.every((v, i) => v === i + 1); // plain 1..n sequence
}

// Chartable when there are >=2 rows, a good label (x) column, and >=1 numeric
// series. Prefers a text category with the most distinct values for x, and
// drops rank/index columns from both the axis and the plotted series.
export function chartSpecFromTable(headers, rows) {
  if (!headers.length || rows.length < 2) return null;
  const nums = new Set(numericCols(headers, rows));
  const colVals = (c) => rows.map((r) => r[c]);
  const distinct = (c) => new Set(colVals(c).map((v) => String(v))).size;

  const dimCols = new Set();
  for (let c = 0; c < headers.length; c++) {
    if (isDimensionCol(headers[c], colVals(c))) dimCols.add(c);
  }

  // x: the non-numeric, non-dimension column with the most distinct values (a
  // real category like a university name); else a dimension col (e.g. Year, good
  // for a time axis); else column 0.
  const nonNumeric = [];
  for (let c = 0; c < headers.length; c++) if (!nums.has(c)) nonNumeric.push(c);
  const labels = nonNumeric.filter((c) => !dimCols.has(c));
  let xIdx;
  if (labels.length) xIdx = labels.reduce((b, c) => (distinct(c) > distinct(b) ? c : b), labels[0]);
  else if (nonNumeric.length) xIdx = nonNumeric[0];
  else if (dimCols.size) xIdx = [...dimCols][0];
  else xIdx = 0;

  // series: numeric columns that aren't the x-axis and aren't dimensions.
  const seriesIdx = [];
  for (let c = 0; c < headers.length; c++) {
    if (c !== xIdx && nums.has(c) && !dimCols.has(c)) seriesIdx.push(c);
  }
  if (seriesIdx.length === 0) return null;

  const xKey = headers[xIdx] || "x";
  const data = rows.map((r) => {
    const o = { [xKey]: r[xIdx] };
    for (const s of seriesIdx) o[headers[s] || `col${s}`] = parseNum(r[s]);
    return o;
  });
  const timeLike = /year|date|month|quarter|day/i.test(xKey);
  return {
    type: timeLike ? "line" : "bar",
    x: xKey,
    y: seriesIdx.map((s) => headers[s] || `col${s}`),
    data,
  };
}

export function toCsv(headers, rows) {
  const esc = (s) => (/[",\n]/.test(s) ? `"${String(s).replace(/"/g, '""')}"` : String(s ?? ""));
  return [headers, ...rows].map((r) => r.map(esc).join(",")).join("\r\n");
}

export function downloadCsv(headers, rows, filename = "table.csv") {
  const blob = new Blob([toCsv(headers, rows)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// Count GFM tables in a markdown string via their delimiter rows — a line made
// up only of |, :, -, and spaces that contains a run of 3+ dashes. A `---`
// horizontal rule has no pipe (not counted); a data row has letters (not
// counted). Used to gate server-side full-dataset CSV to single-table answers,
// where the "re-run the answer's final SQL" endpoint is unambiguous.
export function countMarkdownTables(src) {
  if (typeof src !== "string") return 0;
  return src.split("\n").filter((l) => {
    if (!l.includes("|") || !l.includes("-")) return false;
    // Every pipe-delimited cell must be a GFM alignment spec (:?-+:?) — this
    // matches `---`, `:--`, `--:`, `:-:` alike, but not a data row (has letters)
    // or a `---` horizontal rule (has no pipe).
    return l.split("|").every((c) => c.trim() === "" || /^:?-+:?$/.test(c.trim()));
  }).length;
}

// Download the FULL result set (not just the ≤200 rows the model transcribed
// into the visible table) by hitting the server endpoint, which re-runs the
// answer's final SQL at the large download row cap and streams a CSV. The
// session cookie rides the same-origin GET; the server sets the filename via
// Content-Disposition, so the browser downloads without navigating away.
// `cols` is the displayed table's column count — the server uses it to pick
// WHICH of the answer's queries produced this table (the last query is often a
// scalar COUNT(*), not the listing), so the download is the table, not a total.
export function downloadServerCsv(messageId, cols) {
  const a = document.createElement("a");
  const q = Number.isInteger(cols) && cols > 0 ? `?cols=${cols}` : "";
  a.href = `/api/chat/messages/${messageId}/download.csv${q}`;
  a.rel = "noreferrer";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}
