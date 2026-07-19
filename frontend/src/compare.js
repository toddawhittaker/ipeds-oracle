// Compare mode — pick 2+ rows from a result table and chart just those, instantly,
// from the numbers already in the table (no new query). Pure logic on top of
// tabledata.js so the component (Markdown.jsx) stays thin.
import { chartSpecFromTable } from "./tabledata.js";

// A table is "comparable" when it's a CATEGORICAL comparison — one row per entity
// (universities, states, programs…) with a numeric metric — rather than a
// time-series (year rows). We lean entirely on chartSpecFromTable's existing
// classification: it already infers the entity/label column as `spec.x` and marks
// categorical-x tables as `type: "bar"` (time-like x becomes "line"). So:
//   comparable  ⟺  chartSpecFromTable returns a spec with type === "bar".
// Returns { entityCol, labels, spec } or null.
export function comparableTable(headers, rows) {
  const spec = chartSpecFromTable(headers, rows);
  if (!spec || spec.type !== "bar") return null;
  const entityCol = (headers || []).indexOf(spec.x);
  if (entityCol < 0) return null;
  const labels = spec.data.map((d) => d[spec.x]);
  return { entityCol, labels, spec };
}

// Build the snapshot-comparison chart spec: the parent table's spec filtered to the
// selected entity labels, forced to a bar chart (a snapshot, not a trend). x and the
// metric series (y) are kept stable from the parent so the metric columns don't shift
// as the selection changes. Returns null unless at least 2 selected rows match.
export function compareSpec(spec, selectedLabels) {
  if (!spec || !Array.isArray(spec.data)) return null;
  const sel = new Set((selectedLabels || []).map((s) => String(s)));
  const data = spec.data.filter((d) => sel.has(String(d[spec.x])));
  if (data.length < 2) return null;
  return { ...spec, type: "bar", title: "Comparison", data };
}
