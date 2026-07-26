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
// TWO-SIDED: `matched` reassures, `partial`/`unmatched` caution. What it must
// NEVER do is speak when nothing was checked — `unchecked` (no retained rows to
// compare against) and `no_table` return null, because they are not evidence
// about the numbers in either direction. Warning there would be a false alarm by
// construction, not a finding.
//
// The caution was deliberately withheld at first, and the reason is worth
// keeping: every reconciler op used to run down a single result column, so a
// row-wise "% change" column ((2024 - 2021) / 2021 for that row) had no route
// and a table whose every number was CORRECT graded `partial` — or `unmatched`
// when that column was its only measure. A caution then would have called
// correct answers wrong. Those shapes are fixed: the reconciler anchors each
// table row to the result row it describes and derives across it
// (backend/app/grounding.py, _anchor_row / _match_at_row), which also closed a
// second one, a "% vs prior year" column.
//
// The residual risk is real and the WORDING is what manages it. No checker
// reproduces every legitimate derivation, so a non-match is evidence that THIS
// CHECK could not re-derive the number — not a verdict that the number is wrong.
// Every string below says so, and the title says it outright. Keep it that way:
// the damage from a confident "these are wrong" on a correct answer is far worse
// than the mild under-claim of "could not be reproduced".
//
// ANSWER-scoped, not table-scoped: check_table flattens the cells of EVERY
// table in the answer into one list and returns ONE status, so this renders once
// per answer. Attaching it to a particular table would mis-attribute it, which
// is also why it needs no single-table gate (unlike truncation, whose flag maps
// to one specific query result).
const _CHECK_CAVEAT =
  "A number lands here when this check could not re-derive it from the rows the "
  + "query returned. That is usually a transcription slip, but a column computed "
  + "in a way the check doesn't recognize will also land here — treat it as "
  + "'worth verifying', not as proof of an error.";

/**
 * @param {object} [verdict] The message's persisted table-grounding verdict.
 * @param {string} [verdict.status] Server verdict: matched|partial|unmatched|no_table|unchecked.
 *   `unchecked`/`no_table` render nothing — nothing was compared, so neither tone applies.
 * @param {number} [verdict.cellsChecked] How many numeric MEASURE cells were graded; every
 *   note states a count, so a verdict without it renders nothing.
 * @param {number} [verdict.cellsMatched] How many of those reproduced. Required for the
 *   caution, which leads with the number that did NOT.
 * @returns {{tone: string, text: string, title: string} | null} The line to render, or
 *   null for "say nothing" — the default for every unrecognised or malformed verdict.
 */
export function tableTrustNote({ status, cellsChecked, cellsMatched } = {}) {
  // Guard the empties BEFORE Number(): Number(null) is 0 and 0 is finite, so a
  // message whose counts are NULL — a pre-migration row, a cache hit, a verdict
  // written before these columns existed — would read as "0 matched" and produce
  // a caution accusing an answer of numbers that were never checked at all.
  // Exactly the false alarm this whole feature was held back to avoid, and the
  // same Number(null) trap that once rendered a missing year bound as year zero
  // (years.js). Caught by a test, not by review.
  if (cellsChecked == null) return null;
  const n = Number(cellsChecked);
  if (!Number.isFinite(n) || n <= 0) return null;   // nothing graded, nothing to say
  if (status === "matched") {
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
  if (status !== "partial" && status !== "unmatched") return null;
  if (cellsMatched == null) return null;            // see the Number(null) note above
  const matched = Number(cellsMatched);
  if (!Number.isFinite(matched) || matched < 0 || matched > n) return null;
  const missed = n - matched;
  if (missed <= 0) return null;   // a "failure" with nothing failing is malformed
  // Leads with the count that did NOT reproduce, because that is the actionable
  // half — and names the total so the scale is honest ("3 of 40" is not "40").
  return {
    tone: "warn",
    text: missed === n
      ? `${missed === 1 ? "1 value" : `${missed.toLocaleString()} values`} in this answer `
        + "could not be reproduced from the query result"
      : `${missed.toLocaleString()} of ${n.toLocaleString()} values `
        + "could not be reproduced from the query result",
    title: _CHECK_CAVEAT,
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
