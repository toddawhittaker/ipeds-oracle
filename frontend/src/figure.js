// Normalize/validate a "figure" spec — the structured hero statistic the model
// emits (parsed out of its ```figure fence server-side) and the frontend renders
// above an answer. Also used when a persisted message's figure is loaded.
//
// Returns a clean spec carrying ONLY the known keys, or null when there's no
// usable figure. `value` and `label` are required: without a headline number and
// a caption there is nothing to typeset, so callers can render unconditionally
// and get null → no figure.
const FIGURE_KEYS = ["value", "unit", "label", "source"];

// Did the server reproduce this figure's number from the query results?
//
// POSITIVE-ONLY BY DESIGN. A reproduced figure earns a quiet mark; one that
// wasn't reproduced shows NOTHING — no warning, no "unverified". The check is
// observe-only precisely because its kernel has had false negatives (PR #212 was
// a CORRECT figure graded `ungrounded`, found by reading real answers after the
// unit tests and the eval were both green). A false negative that merely omits a
// mark is recoverable; one that casts doubt on a correct number is not.
//
// The statuses are grounding.py's, and this field carries the BARE status only:
// exact | rounded | derived reproduced the value; ungrounded did not;
// no_figure/malformed/unchecked mean the check never reached a verdict (no
// figure, an unparseable one, or no retained results). An absent value means a
// message written before the column existed.
//
// Note the neighbouring field it is easy to confuse this with: llm.py also
// records `figure_derivation` — the composed provenance string like
// `retry:ctx:sum(q3.awards)`. That one is backend-only telemetry and never
// reaches the browser; `figure_grounding` is only ever one of the constants
// above (see llm.py: every assignment is `check.status` or a bare constant). So
// this matches whole-string — no prefix parsing, because there are no prefixes.
const VERIFIED_STATUSES = new Set(["exact", "rounded", "derived"]);

export function isFigureVerified(grounding) {
  if (typeof grounding !== "string") return false;
  return VERIFIED_STATUSES.has(grounding.trim().toLowerCase());
}

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
