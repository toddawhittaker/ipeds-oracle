// Pure logic for the Usage dashboard's derived stats. Kept out of Admin.jsx so
// the divide-by-zero / no-data cases can be unit-tested under vitest; the fetch
// and rendering stay in the (Playwright-covered) component.

const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : 0);

// Format a cached/total token ratio as a display string: "—" when there's no
// denominator to divide by (empty window, or a provider that reports no cache
// stats), else a whole-percent like "87%". Never divides by zero, never NaN.
function rate(cached, total) {
  const t = num(total);
  if (t <= 0) return "—";
  return `${Math.round((num(cached) / t) * 100)}%`;
}

// BLENDED prompt-cache-hit rate: across every LLM call of every turn, what share
// of prompt tokens the provider served from ITS OWN prefix cache. This is the
// COST metric — it reflects the actual billing discount, whatever got cached
// (the static schema prefix AND the growing in-turn tool-call conversation).
// Distinct from the "Answer cache" stat, which counts our own semantic
// query_cache short-circuits (no LLM call at all).
export function promptCacheRate(totals) {
  const t = totals || {};
  return rate(t.cached_prompt_tokens, t.prompt_tokens);
}

// SCHEMA-PREFIX cache rate: the FIRST LLM call of each turn only. That call's
// prompt is schema-prefix + prior-turn history + question, with no in-turn tool
// rounds accumulated yet — so this isolates cross-question reuse of the big
// static prefix, the number that actually speaks to "is keeping the whole schema
// in the prompt paying off?" (The blended rate above is inflated by later tool
// rounds re-caching the in-turn conversation, so it can't answer that cleanly.)
export function schemaCacheRate(totals) {
  const t = totals || {};
  return rate(t.first_call_cached_prompt_tokens, t.first_call_prompt_tokens);
}

// GROUNDED-FIGURE rate: of the answers that led with a hero figure and had query
// results to check it against, what share carried a number the server could
// reproduce from those results — verbatim, at the figure's own rounding, or via
// a derivation the prompt actually asks for (sum / mean / % change / share /
// max / min). See backend/app/grounding.py.
//
// This is a DATA-INTEGRITY metric, not a cost one: the figure is the most
// prominent number in an answer and, until this landed, the least verified — it
// was re-typed by the model out of a Markdown table with nothing comparing it
// back to the rows. A rate below ~100% means figures are reaching users that the
// server cannot reproduce from its own data. Turns with no figure, and turns
// with no retained results, are excluded from the denominator server-side (they
// are not evidence either way), so an empty window shows "—" rather than a
// falsely perfect 100%.
export function groundedFigureRate(totals) {
  const t = totals || {};
  const checked = num(t.figures_checked);
  return rate(checked - num(t.figures_ungrounded), checked);
}

// The stat's sub-label, carrying its own SAMPLE SIZE: "7/7 Grounded figures".
// A bare percentage hides how much it rests on — "100%" off a single checked
// figure and "100%" off four hundred are the same string but not the same
// evidence, and during the observe-only period that difference is the point.
// With nothing measured yet the counts are dropped rather than shown as "0/0",
// which reads like a failure instead of an empty window (the rate itself
// already shows "—" there).
export function groundedFigureLabel(totals) {
  const t = totals || {};
  const checked = num(t.figures_checked);
  if (checked <= 0) return "Grounded figures";
  return `${checked - num(t.figures_ungrounded)}/${checked} Grounded figures`;
}

// TABLE grounding: the cell-level companion to the figure rate. The results table
// is the model re-typing the query rows one-for-one — the densest block of
// numbers on screen — so this is the fraction of its numeric CELLS the server can
// reproduce from the retained rows (verbatim, display-rounded, or via a
// derivation, same rule as the figure). A transcription-accuracy signal, also a
// data-integrity metric, not a cost one. Turns with no table and turns with no
// retained results carry 0 cells server-side, so they self-exclude from the
// denominator and an empty window shows "—", not a false 100%.
export function groundedTableRate(totals) {
  const t = totals || {};
  return rate(t.table_cells_matched, t.table_cells_checked);
}

// Sub-label carrying the sample size, mirroring groundedFigureLabel: "312/318
// Grounded cells". Bare label until anything's been checked (rather than "0/0",
// which reads as failure instead of an empty window — the rate already shows "—").
export function groundedTableLabel(totals) {
  const t = totals || {};
  const checked = num(t.table_cells_checked);
  if (checked <= 0) return "Grounded cells";
  return `${num(t.table_cells_matched)}/${checked} Grounded cells`;
}

// LEAK rate: of the real agent turns, what share shipped residual fence/JSON
// debris in the prose (the sentinel). This is the metric that proves structured
// emission works — it should fall to 0 as `structured_turns` rises. "—" on an
// empty window (no real agent turns). See backend/app/llm.py `_leak_flag`.
export function leakRate(totals) {
  const t = totals || {};
  return rate(num(t.leaked_turns), num(t.emit_turns));
}

// Its sub-label carries the sample + the structured-emission share, so the
// dark-ship rollout is legible: "2/50 leaked · 100% structured". Drops to a bare
// label when nothing's been measured yet.
export function leakLabel(totals) {
  const t = totals || {};
  const turns = num(t.emit_turns);
  if (turns <= 0) return "Answer leaks";
  const pct = Math.round((num(t.structured_turns) / turns) * 100);
  return `${num(t.leaked_turns)}/${turns} leaked · ${pct}% structured`;
}
