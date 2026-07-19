// Detect the answer "brief" layout: exactly ONE result table AND ONE ```chart
// fence — the single-number brief the agent produces (hero figure + synopsis +
// recent-years table + trend chart). When that pattern holds we PAIR the table and
// chart: render them side by side, drop the table's now-redundant "Chart this"
// toggle, and suppress the standalone chart (the table renders it beside itself).
//
// Anything else (no chart, multiple tables, a chart with no table) returns
// { pair:false } and renders normally. Pure + string-only so it's vitest-tested.
const CHART_FENCE = /```chart[ \t]*\r?\n([\s\S]*?)```/g;

// A GFM table's delimiter row: only pipes / dashes / colons / spaces, with at
// least one dash and a pipe (so a `---` horizontal rule, which has no pipe, and
// prose, which has letters, are both excluded). Counting these ≈ counting tables.
function tableCount(src) {
  return src.split("\n").filter(
    (l) => l.includes("|") && l.includes("-") && /^[\s|:-]+$/.test(l)).length;
}

export function briefLayout(src) {
  if (typeof src !== "string") return { pair: false, chart: null };
  const charts = [...src.matchAll(CHART_FENCE)];
  if (charts.length !== 1 || tableCount(src) !== 1) return { pair: false, chart: null };
  let chart = null;
  try { chart = JSON.parse(charts[0][1].trim()); } catch { chart = null; }
  return chart ? { pair: true, chart } : { pair: false, chart: null };
}
