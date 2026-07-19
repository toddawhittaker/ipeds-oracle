// Trend math for a chart series — a least-squares fit (the overlaid trend line)
// and the %-change over the range (the delta badge). Pure: it operates on the
// chart spec's already-numeric `data` rows (see tabledata.js), and only ever uses
// finite values, so a missing/NaN cell can't skew the line or the delta.

// Least-squares fit of y = slope*i + intercept over the point INDEX i (0..n-1),
// using only rows whose value is finite. Returns null with < 2 usable points (or a
// degenerate x-spread), so callers render no trend line rather than a bad one.
export function linearFit(values) {
  const pts = [];
  values.forEach((y, i) => { if (Number.isFinite(y)) pts.push([i, y]); });
  const n = pts.length;
  if (n < 2) return null;
  let sx = 0, sy = 0, sxx = 0, sxy = 0;
  for (const [x, y] of pts) { sx += x; sy += y; sxx += x * x; sxy += x * y; }
  const denom = n * sxx - sx * sx;
  if (denom === 0) return null;
  const slope = (n * sxy - sx * sy) / denom;
  const intercept = (sy - slope * sx) / n;
  return { slope, intercept };
}

// The fitted trend value at every row index (for a computed `__trend` series
// injected alongside the real one). Null when a fit isn't possible.
export function trendValues(data, key) {
  const fit = linearFit((data || []).map((r) => Number(r?.[key])));
  if (!fit) return null;
  return data.map((_row, i) => fit.slope * i + fit.intercept);
}

// %-change from the FIRST to the LAST finite value of `key`. Returns
// { pct, first, last, dir } or null (no change from zero, or < 2 points).
// dir: "up" | "down" | "flat" (flat inside ±0.5%).
export function pctChange(data, key) {
  const vals = (data || []).map((r) => Number(r?.[key])).filter((v) => Number.isFinite(v));
  if (vals.length < 2) return null;
  const first = vals[0], last = vals[vals.length - 1];
  if (first === 0) return null; // % change from zero is undefined
  const pct = ((last - first) / Math.abs(first)) * 100;
  const dir = pct > 0.5 ? "up" : pct < -0.5 ? "down" : "flat";
  return { pct, first, last, dir };
}
