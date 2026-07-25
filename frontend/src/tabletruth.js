// What a result table is allowed to claim about itself.
//
// run_sql cuts every result at 200 rows. Until now the browser had NO structured
// signal that a cut happened — `to_storage` dropped the flag and the
// conversation load never selected it — so a 200-row PAGE of an 834-row result
// was byte-identical on screen to a complete 200-row result, and the only
// disclosure was whatever sentence the model remembered to write.
//
// Two things follow from that, and this module owns the wording for both.

// The row cap run_sql applies (backend config: sql_row_cap_model). Only used for
// display; the server decides what actually truncates.
export const ROW_CAP = 200;

// Whether THIS table may carry a truncation caption.
//
// Deliberately narrow. Mapping N query results onto N rendered markdown tables is
// heuristic — it's exactly what the server's own _select_table_sql does with
// column-count probing, and it can pick wrong. So the caption is scoped to the
// single-table case, reusing the gate that already decides whether the CSV button
// re-runs the query server-side. A multi-table answer keeps today's behaviour
// rather than risking a caption pointed at the wrong table.
export function canCaptionTruncation({ truncated, tableCount, messageId }) {
  return Boolean(truncated) && tableCount === 1 && messageId != null;
}

// The caption above the toolbar. NOTE what it does not say: a total. Nothing in
// the system knows one — QueryResult.row_count is the count AFTER the cut and no
// code path runs a COUNT(*) — so "of 3,412" would be invented. Saying less and
// meaning it beats a confident number nobody computed.
export function truncationCaption(truncated) {
  return truncated
    ? `First ${ROW_CAP} rows · the full result is larger`
    : "";
}

// Shown once a sort is ACTIVE on a truncated table.
//
// This is the sharpest edge in the feature: sorting a page and reading off the
// top is the natural analyst gesture, and on a truncated table it answers a
// different question than the one asked — "the biggest of the first 200",
// presented under a lit accent caret that looks authoritative. The old note
// appeared only AFTER sorting, in 12px muted text, and said "Sorted the N rows
// shown here" where N was however many rows the model transcribed — a number
// unrelated to both the cap and the true total.
export function sortScopeNote({ truncated, sorted, rowsShown }) {
  if (!sorted) return "";
  if (truncated) {
    return `Sorted within the first ${ROW_CAP} rows — this is NOT a ranking of the full result. `
      + "Download the CSV for the complete data.";
  }
  return `Sorted the ${rowsShown} rows shown here.`;
}

// Tone for that note, so a truncated sort reads as a warning rather than a
// footnote. Kept here (not in JSX) so the pairing is testable.
export function sortNoteTone(truncated) {
  return truncated ? "warn" : "muted";
}

// The CSV button's label.
//
// The same "Download CSV" text meant two different things depending on the
// answer: a server-side re-run of the query at the 100k cap (single-table
// answers, which carry a message id), or a client-side dump of just the rows the
// model transcribed on screen (everything else). The difference matters exactly
// when it's invisible — someone exporting a multi-table answer got the
// transcription, not the query's output, with nothing saying so.
export function csvLabel({ serverSide, rowsShown }) {
  if (serverSide) return "Download full result (CSV)";
  return `Download these ${rowsShown} rows (CSV)`;
}

// Why a CSV export failed, in a sentence. The export re-runs the query, so it
// can time out or be rate-limited independently of the answer that produced it —
// and those are the two likeliest failures, not bugs.
export function csvErrorMessage(status, detail) {
  if (status === 504) return "That export took too long to build. Try narrowing the question.";
  if (status === 429) return "You're downloading faster than the server can build exports. Try again in a moment.";
  if (status === 401) return "Your session expired. Sign in again to download.";
  return detail || "Couldn't build that CSV. Try again in a moment.";
}
