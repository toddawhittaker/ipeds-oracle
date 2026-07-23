// Plain-language help copy for each Usage-dashboard statistic, kept out of
// Admin.jsx so the content and the "which way is good" mapping can be unit-tested
// and edited without touching the component. Each entry is pure data:
//   name      — the clean stat name (for the info button's accessible label)
//   what      — one or two sentences: what it measures and why it's here
//   direction — "up" (higher is better) | "down" (lower is better) | "flat" (a
//               plain count, neither high nor low is inherently good)
//   note      — optional extra guidance (a caveat or an action to take)
// The keys are referenced by <Stat info=...> in Admin.jsx.

export const STAT_INFO = {
  queries: {
    name: "Queries",
    what: "The number of questions asked during the selected time window. It's " +
      "here to set the scale for every other number on this screen.",
    direction: "flat",
  },
  tokens: {
    name: "Tokens",
    what: "Total language-model tokens processed (the prompt plus the reply) " +
      "across those questions. It mostly tracks how many questions were asked " +
      "and how large each prompt was.",
    direction: "flat",
    note: "Fewer tokens per question means a tighter, cheaper prompt.",
  },
  spend: {
    name: "Spend",
    what: "Estimated language-model cost for the window — taken from the " +
      "provider's own reported per-request price, or from your fallback prices " +
      "if the provider doesn't report one.",
    direction: "down",
  },
  answerCache: {
    name: "Answer cache",
    what: "How many answers were served straight from the semantic cache: a " +
      "repeat or near-identical question reused a stored answer instead of " +
      "calling the model at all.",
    direction: "up",
    note: "Every hit is a question answered instantly and for free.",
  },
  schemaCache: {
    name: "Schema cache",
    what: "On the FIRST model call of each question, the share of prompt tokens " +
      "the provider served from its own cache. Because the large, fixed data " +
      "schema sits at the front of every prompt, this shows how well that schema " +
      "is being reused across questions.",
    direction: "up",
    note: "High means keeping the whole schema in the prompt is paying off; " +
      "persistently low points to a provider-routing issue, not the schema.",
  },
  promptCache: {
    name: "Prompt cache",
    what: "Across every model call of every question — including the tool-use " +
      "rounds — the share of prompt tokens the provider served from its cache. " +
      "This is the blended, real cost-savings figure.",
    direction: "up",
  },
  escalations: {
    name: "Escalations",
    what: "How many questions fell back from the fast default model to the " +
      "stronger, more expensive one after repeated tool failures.",
    direction: "down",
    note: "A few is normal on hard questions; a lot means many questions are " +
      "struggling on the fast model.",
  },
  failures: {
    name: "Failures",
    what: "Questions that ended in an error instead of an answer.",
    direction: "down",
  },
  groundedFigures: {
    name: "Grounded figures",
    what: "Of the answers that led with a big “hero” figure AND had " +
      "query results to check it against, the share whose number the server " +
      "could reproduce from those results. A data-integrity check — is the " +
      "headline number actually in the data? — not a cost metric.",
    direction: "up",
    note: "Below 100% means a headline number reached someone that we could not " +
      "reproduce from the query rows.",
  },
  groundedCells: {
    name: "Grounded cells",
    what: "Of the numeric cells in answer tables (where there were results to " +
      "check), the share the server could reproduce from the query rows — " +
      "transcription accuracy for the densest block of numbers on the screen.",
    direction: "up",
  },
  answerLeaks: {
    name: "Answer leaks",
    what: "The share of answers where raw formatting or JSON debris had to be " +
      "scrubbed out of the text before it was shown. It proves the " +
      "structured-output path is holding — the debris is caught and " +
      "removed, never shipped to the user.",
    direction: "down",
    note: "It should sit at or near 0%.",
  },
  exhausted: {
    name: "Exhausted",
    what: "Questions that used up the entire tool-call budget before answering " +
      "— the model kept querying and ran out of steps. “N degraded” " +
      "counts the ones where it then invented numbers that we caught and " +
      "replaced with an honest “couldn't finish” message.",
    direction: "down",
    note: "A rising count means questions are hitting the ceiling — consider " +
      "raising LLM_MAX_TOOL_ITERS.",
  },
};

// The "which way is good" one-liner shown at the foot of each info bubble.
export function directionHint(direction) {
  if (direction === "up") return "Higher is better.";
  if (direction === "down") return "Lower is better.";
  return "Just a count — neither high nor low is inherently good.";
}
