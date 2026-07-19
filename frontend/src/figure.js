// Normalize/validate a "figure" spec — the structured hero statistic the model
// emits (parsed out of its ```figure fence server-side) and the frontend renders
// above an answer. Also used when a persisted message's figure is loaded.
//
// Returns a clean spec carrying ONLY the known keys, or null when there's no
// usable figure. `value` and `label` are required: without a headline number and
// a caption there is nothing to typeset, so callers can render unconditionally
// and get null → no figure.
const FIGURE_KEYS = ["value", "unit", "label", "source"];

export function normalizeFigure(raw) {
  if (!raw || typeof raw !== "object") return null;
  const out = {};
  for (const k of FIGURE_KEYS) {
    const v = raw[k];
    if (v == null) continue;
    const s = String(v).trim();
    if (s) out[k] = s;
  }
  if (!out.value || !out.label) return null;
  return out;
}
