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
