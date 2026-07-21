// Normalize the disambiguation "clarify" payload — the model's ```clarify fence
// (parsed server-side) or a persisted message's column: {question, options[]}.
// Returns {question, options[]} (trimmed, de-duplicated, capped at 4 options) or
// null when there's nothing renderable. Pure/testable; the component renders
// nothing on null. Mirrors normalizeSuggestions (suggestions.js).
const MAX_OPTIONS = 4;

export function normalizeClarify(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const question = String(raw.question ?? "").trim();
  if (!question) return null;
  const source = Array.isArray(raw.options) ? raw.options : [];
  const seen = new Set();
  const options = [];
  for (const o of source) {
    const s = String(o ?? "").trim();
    if (s && !seen.has(s)) {
      seen.add(s);
      options.push(s);
      if (options.length >= MAX_OPTIONS) break;
    }
  }
  if (options.length === 0) return null;
  return { question, options };
}
