import React from "react";
import { Clarify } from "ipeds-query-web";

// A clarify BLOCKS the answer, so its chips are accent-filled rather than the
// outline treatment Suggestions uses — the distinction is shape and fill, not
// colour alone. Options are short ANSWER PHRASES, never restated questions.

export const DidYouMean = () => (
  <Clarify
    spec={{
      question: "Which award level did you mean?",
      options: ["Bachelor's only", "All award levels", "Graduate only"],
    }}
    onAsk={() => {}}
  />
);

export const ShowingQuestion = () => (
  <Clarify
    spec={{
      question: "Count first majors only, or every major?",
      options: ["First majors only", "Every major"],
    }}
    onAsk={() => {}}
    showQuestion
  />
);

export const Disabled = () => (
  <Clarify
    spec={{ question: "Which years?", options: ["Most recent year", "Last five years"] }}
    onAsk={() => {}}
    disabled
  />
);
