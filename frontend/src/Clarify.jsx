import React from "react";
import { normalizeClarify } from "./clarify.js";

// The disambiguation clarify turn's answer-phrase chips. By design the
// clarifying QUESTION prose rides in the assistant bubble (the stripped answer
// text), so this is normally chips-only, structurally identical to
// Suggestions.jsx. Defensive fallback: if the caller reports there's no prose
// to carry the question (the model emitted the ```clarify fence with nothing
// else), the chips would otherwise be undecidable — show `c.question` as the
// group's own heading in that case instead of the generic "Did you mean" label,
// so the chips are never unlabeled. Renders nothing when there's nothing
// renderable.
export default function Clarify({ spec, onAsk, disabled, showQuestion = false }) {
  const c = normalizeClarify(spec);
  if (!c) return null;
  return (
    <div className="suggestions clarify" role="group"
         aria-label="Did you mean — clarify your question">
      <span className="field-label suggestions-label">
        {showQuestion ? c.question : "Did you mean"}
      </span>
      <div className="suggestions-chips">
        {c.options.map((o) => (
          <button key={o} type="button" className="suggestion-chip"
                  disabled={disabled} onClick={() => onAsk(o)}>
            {o}
          </button>
        ))}
      </div>
    </div>
  );
}
