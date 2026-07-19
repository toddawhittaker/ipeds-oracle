// Helpers for turning a rendered markdown table into data — used for per-table
// CSV export and "Chart this". Extraction walks the hast `node` react-markdown
// hands to the table component, so it works on the actual rendered content.

function hastText(node) {
  if (!node) return "";
  if (node.type === "text") return node.value || "";
  return (node.children || []).map(hastText).join("");
}

// -> { headers: string[], rows: string[][] }
export function extractTable(node) {
  let headers = [];
  const rows = [];
  const walk = (n) => {
    for (const c of n.children || []) {
      if (c.tagName === "tr") {
        const cells = (c.children || [])
          .filter((x) => x.tagName === "th" || x.tagName === "td")
          .map((x) => hastText(x).trim());
        if ((c.children || []).some((x) => x.tagName === "th")) headers = cells;
        else rows.push(cells);
      } else {
        walk(c);
      }
    }
  };
  walk(node);
  return { headers, rows };
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

// A column (by index) is numeric if most of its cells parse as numbers.
function numericCols(headers, rows) {
  const out = [];
  for (let c = 0; c < headers.length; c++) {
    let ok = 0;
    for (const r of rows) if (!Number.isNaN(parseNum(r[c]))) ok++;
    if (rows.length && ok / rows.length >= 0.6) out.push(c);
  }
  return out;
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
