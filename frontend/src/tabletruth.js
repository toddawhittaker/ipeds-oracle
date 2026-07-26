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

// Whether the answer's numbers could be reproduced from the rows the query
// actually returned — the table's equivalent of the hero figure's "✓ verified".
//
// The server grades every numeric MEASURE cell of the answer's tables against
// the retained query results (backend/app/grounding.py check_table) and persists
// the verdict plus two counts on the message. This turns that into the one line
// the reader sees, or null for "say nothing".
//
// POSITIVE-ONLY, and this is the part to read before "improving" it. `partial`
// and `unmatched` return null.
//
// The ORIGINAL reason was that a caution would fire on CORRECT answers: every
// reconciler op ran down a single result column, so a row-wise "% change" column
// ((2024 - 2021) / 2021 for that row) had no route and a correct table graded
// `partial` — or `unmatched` when that column was its only measure. Those shapes
// are FIXED: the reconciler now anchors each table row to the result row it
// describes and derives across it (backend/app/grounding.py, _anchor_row /
// _match_at_row), and the same pass fixed a second one, a "% vs prior year"
// column.
//
// So a caution is no longer disqualified in principle — it is waiting on
// evidence. Every historical partial/unmatched turn was graded by the OLD kernel
// and those messages are gone, so nobody has yet SEEN what a non-match means
// under anchoring. Let some accumulate, read them, then decide. Switching this
// on beforehand would repeat, in the opposite direction, the assumption that
// measuring caught the first time.
//
// ANSWER-scoped, not table-scoped: check_table flattens the cells of EVERY
// table in the answer into one list and returns ONE status, so this renders once
// per answer. Attaching it to a particular table would mis-attribute it, which
// is also why it needs no single-table gate (unlike truncation, whose flag maps
// to one specific query result).
/**
 * @param {object} [verdict] The message's persisted table-grounding verdict.
 * @param {string} [verdict.status] Server verdict: matched|partial|unmatched|no_table|unchecked.
 *   Only `matched` renders — see the positive-only note above before widening this.
 * @param {number} [verdict.cellsChecked] How many numeric MEASURE cells were graded; the
 *   note states this count, so a verdict without it renders nothing.
 * @returns {{tone: string, text: string, title: string} | null} The line to render, or
 *   null for "say nothing" — the default for every non-matched and malformed verdict.
 */
export function tableTrustNote({ status, cellsChecked } = {}) {
  if (status !== "matched") return null;      // see the positive-only note above
  const n = Number(cellsChecked);
  if (!Number.isFinite(n) || n <= 0) return null;
  // Says the COUNT, never "all": check_table grades measure columns only — a
  // rank ordinal and dimension columns (year/unitid/cipcode) are excluded — so
  // "all values verified" would claim coverage the check doesn't have.
  // And "reproduced from", not "correct": it means these numbers came from the
  // rows the query returned, not that the query asked the right question.
  return {
    tone: "ok",
    text: `${n.toLocaleString()} ${n === 1 ? "value" : "values"} reproduced from the query result`,
    title: "Each number was re-derived from the rows this answer's query returned. "
      + "It confirms the values were transcribed faithfully, not that the query "
      + "asked the right question.",
  };
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
