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
