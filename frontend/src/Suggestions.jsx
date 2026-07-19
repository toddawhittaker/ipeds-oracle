import React from "react";
import { normalizeSuggestions } from "./suggestions.js";

// "You might also ask" — the model's drill-down follow-up questions as clickable
// chips beneath an answer. Clicking submits the question as a FOLLOW-UP turn (which
// gets its own brief), turning a single answer into an exploration loop. Renders
// nothing when there are no suggestions.
export default function Suggestions({ items, onAsk, disabled }) {
  const qs = normalizeSuggestions(items);
  if (qs.length === 0) return null;
  return (
    <div className="suggestions" role="group" aria-label="Suggested follow-up questions">
      <span className="field-label suggestions-label">You might also ask</span>
      <div className="suggestions-chips">
        {qs.map((q) => (
          <button key={q} type="button" className="suggestion-chip"
                  disabled={disabled} onClick={() => onAsk(q)}>
            {q}
          </button>
        ))}
      </div>
    </div>
  );
}
