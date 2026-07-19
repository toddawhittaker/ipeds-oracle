// Normalize the "you might also ask" drill-down suggestions — the model's
// followups fence (parsed server-side) or a persisted message's column. Returns up
// to 3 non-empty, trimmed, de-duplicated question strings, or [] when there are
// none. Pure/testable; the component renders nothing on [].
export function normalizeSuggestions(raw) {
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  const out = [];
  for (const q of raw) {
    const s = String(q ?? "").trim();
    if (s && !seen.has(s)) {
      seen.add(s);
      out.push(s);
      if (out.length >= 3) break;
    }
  }
  return out;
}
