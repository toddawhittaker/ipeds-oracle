import { describe, expect, it } from "vitest";

import {
  canCaptionTruncation,
  csvErrorMessage,
  csvLabel,
  sortNoteTone,
  sortScopeNote,
  tableTrustNote,
  truncationCaption,
} from "./tabletruth.js";

describe("tableTrustNote", () => {
  it("marks a fully reproduced answer, stating the count", () => {
    const note = tableTrustNote({ status: "matched", cellsChecked: 40, cellsMatched: 40 });
    expect(note).not.toBeNull();
    expect(note.tone).toBe("ok");
    expect(note.text).toContain("40");
    // "N values", never "all" — check_table grades MEASURE columns only, so a
    // rank ordinal and every dimension column went ungraded. Claiming "all"
    // would assert coverage the check never had.
    expect(note.text).not.toMatch(/\ball\b/i);
    // Reproduction, not correctness — the tooltip must not promise the query
    // asked the right question.
    expect(note.title).toMatch(/not that the query/i);
  });

  it("asks the reader to CHECK, rather than claiming the numbers are wrong", () => {
    // THE wording contract, and the whole reason a caution can ship at all.
    // Every time this fired on real data it was a gap in the CHECKER, not a
    // model error — four correct answers flagged. A verdict ("could not be
    // reproduced") reads as "don't trust these" and attacks work that was fine;
    // an instruction survives being wrong, because a reader who looks and finds
    // the numbers correct has lost ten seconds and nothing else.
    const note = tableTrustNote({ status: "unmatched", cellsChecked: 15, cellsMatched: 0 });
    expect(note.tone).toBe("warn");
    expect(note.text).toMatch(/^Check\b/);
    // It points at the two real controls on the same answer.
    expect(note.text).toMatch(/SQL/);
    expect(note.text).toMatch(/CSV/);
    // And it never asserts anything about the numbers themselves.
    expect(note.text).not.toMatch(/\b(wrong|incorrect|error|invalid|unverified|failed)\b/i);
    expect(note.title).toMatch(/ground truth/i);
  });

  it("names the EARLIER query when this answer ran none", () => {
    // FOUND LIVE (conversation 23, turn 3): a reshape of the previous table ran
    // no SQL, yet the mark read "reproduced from the query result" — implying a
    // query this answer never made. Grounding IS conversation-scoped on purpose
    // (it's the only reason a transpose can be verified at all), so the claim is
    // sound; the SOURCE clause was not. A reader who opened the SQL disclosure
    // to check found nothing there, which is what made a correct ✓ look suspect.
    const note = tableTrustNote({
      status: "matched", cellsChecked: 15, cellsMatched: 15, hasSql: false });
    expect(note.tone).toBe("ok");
    expect(note.text).toContain("15 values reproduced from the earlier query result");
    expect(note.title).toMatch(/earlier turn/i);
    // Still a claim about reproduction, never about correctness.
    expect(note.title).toMatch(/not that the original query/i);

    // The default is unchanged for an ordinary turn — this must not reword
    // every answer in the app.
    const own = tableTrustNote({ status: "matched", cellsChecked: 15, cellsMatched: 15 });
    expect(own.text).toBe("15 values reproduced from the query result");
  });

  it("points a caution at the earlier answer when this one has no SQL", () => {
    // The instruction has to name a destination that EXISTS. On a reshape turn
    // there is no SQL disclosure to open, and the CSV button exports only the
    // transcribed rows (the server has no query to re-run — see Markdown.jsx's
    // hasSql gate). Sending the reader to "the SQL or CSV" is sending them
    // somewhere that isn't there.
    const note = tableTrustNote({
      status: "partial", cellsChecked: 22, cellsMatched: 9, hasSql: false });
    expect(note.tone).toBe("warn");
    expect(note.text).toBe("Check 13 of 22 values against the earlier answer's SQL or CSV");
    // Still an instruction, still no claim about the numbers.
    expect(note.text).toMatch(/^Check\b/);
    expect(note.text).not.toMatch(/\b(wrong|incorrect|error|invalid|failed)\b/i);
  });

  it("counts what needs checking, and names the total when only some do", () => {
    // 13 needs checking, not the 9 that passed — and naming 22 keeps the scale
    // honest, so "13 of 22" can't be read as "the whole table".
    const some = tableTrustNote({ status: "partial", cellsChecked: 22, cellsMatched: 9 });
    expect(some.text).toContain("13 of 22");
    // When every graded value needs checking there is no useful "of N" to add.
    const all = tableTrustNote({ status: "unmatched", cellsChecked: 15, cellsMatched: 0 });
    expect(all.text).toContain("15 values");
    expect(all.text).not.toContain("of 15");
  });

  it("agrees in number, both when some failed and when all did", () => {
    expect(tableTrustNote({ status: "partial", cellsChecked: 40, cellsMatched: 39 }).text)
      .toBe("Check 1 of 40 values against the SQL or CSV");
    expect(tableTrustNote({ status: "unmatched", cellsChecked: 1, cellsMatched: 0 }).text)
      .toBe("Check 1 value against the SQL or CSV");
    expect(tableTrustNote({ status: "unmatched", cellsChecked: 4, cellsMatched: 0 }).text)
      .toBe("Check 4 values against the SQL or CSV");
  });

  it("stays SILENT on a failure verdict whose counts contradict it", () => {
    // A `partial` that missed nothing, or counts outside the graded set, is a
    // malformed verdict — a bug upstream, not a finding. Cautioning on it would
    // put a warning on an answer nothing is actually wrong with, which is the
    // exact failure this feature was held back to avoid.
    for (const arg of [{ status: "partial", cellsChecked: 22, cellsMatched: 22 },
                       { status: "partial", cellsChecked: 22, cellsMatched: 30 },
                       { status: "partial", cellsChecked: 22, cellsMatched: -1 },
                       { status: "unmatched", cellsChecked: 15 },
                       { status: "partial", cellsChecked: 22, cellsMatched: null }]) {
      expect(tableTrustNote(arg)).toBeNull();
    }
  });

  it("says nothing when nothing was checked", () => {
    // `unchecked` = no retained rows to compare against; `no_table` = no numeric
    // table. Neither is evidence about the numbers, so neither may render.
    for (const status of ["unchecked", "no_table"]) {
      expect(tableTrustNote({ status, cellsChecked: 0, cellsMatched: 0 })).toBeNull();
    }
  });

  it("says nothing for an absent or unrecognised verdict", () => {
    // A pre-migration message, a cache hit, and a refusal all arrive as null —
    // the default must be silence, never a claim.
    for (const arg of [undefined, {}, { status: null }, { status: "matched" },
                       { status: "matched", cellsChecked: 0 },
                       { status: "matched", cellsChecked: null },
                       { status: "wat", cellsChecked: 5 }]) {
      expect(tableTrustNote(arg)).toBeNull();
    }
  });

  it("keeps the count grammatical and readable at any size", () => {
    expect(tableTrustNote({ status: "matched", cellsChecked: 1 }).text).toMatch(/\b1 value\b/);
    expect(tableTrustNote({ status: "matched", cellsChecked: 2 }).text).toMatch(/\b2 values\b/);
    expect(tableTrustNote({ status: "matched", cellsChecked: 1294 }).text)
      .toMatch(/1,294 values/);
  });
});

describe("canCaptionTruncation", () => {
  const base = { truncated: true, tableCount: 1, messageId: 12 };

  it("captions a truncated single-table answer", () => {
    expect(canCaptionTruncation(base)).toBe(true);
  });

  it("says nothing when the result was complete", () => {
    expect(canCaptionTruncation({ ...base, truncated: false })).toBe(false);
  });

  it("stays silent on a MULTI-table answer rather than guessing which table", () => {
    // Mapping N query results onto N rendered tables is a heuristic — the
    // server's own _select_table_sql does it by column-count probing and can
    // pick wrong. A caption pointed at the wrong table is worse than none.
    expect(canCaptionTruncation({ ...base, tableCount: 2 })).toBe(false);
    expect(canCaptionTruncation({ ...base, tableCount: 0 })).toBe(false);
  });

  it("stays silent on a live, not-yet-persisted turn", () => {
    expect(canCaptionTruncation({ ...base, messageId: null })).toBe(false);
    expect(canCaptionTruncation({ ...base, messageId: undefined })).toBe(false);
  });
});

describe("truncationCaption", () => {
  it("NEVER states a total, because nothing computes one", () => {
    // THE REGRESSION GUARD: row_count is the count AFTER the cut and no code
    // path runs COUNT(*). A caption reading "of 3,412" would be invented.
    const caption = truncationCaption(true, 200);
    expect(caption).toContain("200");
    expect(caption).toMatch(/larger/i);
    expect(caption).not.toMatch(/\bof\s+[\d,]+/i);
  });

  it("is empty for a complete result — no caption is the honest default", () => {
    expect(truncationCaption(false, 200)).toBe("");
  });

  it("prints the SERVER's cap, not a hardcoded 200", () => {
    // sql_row_cap_model is env-overridable, so a deployment that changed it was
    // being told a number that was simply wrong.
    expect(truncationCaption(true, 1000)).toContain("1,000");
    expect(truncationCaption(true, 50)).toContain("50");
  });

  it.each([[null], [undefined], [0], [""], ["abc"], [NaN], [-5]])(
    "keeps every claim but drops the number when the cap is %p", (cap) => {
      // Number(null) is 0 and Number.isFinite(0) is true — the trap that has
      // shipped three times here. A missing cap must never render "First 0
      // rows"; it must say less, not something false.
      const caption = truncationCaption(true, cap);
      expect(caption).not.toMatch(/\d/);
      expect(caption).toMatch(/larger/i);
      expect(caption.length).toBeGreaterThan(0);
    });
});

describe("sortScopeNote", () => {
  it("is silent until a sort is actually active", () => {
    expect(sortScopeNote({ truncated: true, sorted: false, rowsShown: 200, rowCap: 200 })).toBe("");
  });

  it("warns that a sorted page is not a ranking of the full result", () => {
    // The sharpest edge in the feature: sorting a page and reading off the top
    // answers "the biggest of the first 200", under an authoritative caret.
    const note = sortScopeNote({ truncated: true, sorted: true, rowsShown: 200, rowCap: 200 });
    expect(note).toMatch(/not a ranking/i);
    expect(note).toContain("200");
    expect(note).toMatch(/csv/i);
  });

  it("prints the server's cap in the sort note too", () => {
    const note = sortScopeNote({ truncated: true, sorted: true, rowsShown: 200, rowCap: 1000 });
    expect(note).toContain("1,000");
  });

  it.each([[null], [undefined], [0], ["abc"]])(
    "keeps the NOT-a-ranking warning without a number when the cap is %p", (cap) => {
      // The warning is the load-bearing half: losing the number must never lose
      // the claim, or a truncated sort silently reads as authoritative again.
      const note = sortScopeNote({ truncated: true, sorted: true, rowsShown: 200, rowCap: cap });
      expect(note).toMatch(/not a ranking/i);
      expect(note).toMatch(/csv/i);
      expect(note).not.toMatch(/first \d/i);
    });

  it("keeps the mild wording when the table is complete", () => {
    const note = sortScopeNote({ truncated: false, sorted: true, rowsShown: 27, rowCap: 200 });
    expect(note).toContain("27");
    expect(note).not.toMatch(/not a ranking/i);
  });

  it("tones the note as a warning only when truncated", () => {
    expect(sortNoteTone(true)).toBe("warn");
    expect(sortNoteTone(false)).toBe("muted");
  });
});

describe("csvLabel", () => {
  it("distinguishes the full server export from the rows on screen", () => {
    // One label meant two different things: a server re-run at the 100k cap, or
    // a dump of what the model transcribed. The difference matters exactly when
    // it is invisible.
    expect(csvLabel({ serverSide: true, rowsShown: 200 })).toMatch(/full result/i);
    expect(csvLabel({ serverSide: false, rowsShown: 40 })).toMatch(/these 40 rows/i);
  });

  it("never claims 'full result' for a client-side dump", () => {
    expect(csvLabel({ serverSide: false, rowsShown: 200 })).not.toMatch(/full/i);
  });
});

describe("csvErrorMessage", () => {
  it.each([
    [504, /too long/i],
    [429, /faster than/i],
    [401, /session expired/i],
  ])("explains %i in a sentence", (status, pattern) => {
    expect(csvErrorMessage(status, "")).toMatch(pattern);
  });

  it("prefers the server's own detail for anything else", () => {
    expect(csvErrorMessage(400, "No query is associated with this answer."))
      .toBe("No query is associated with this answer.");
  });

  it("always returns a sentence, even with nothing to go on", () => {
    // `detail` arrives ALREADY PARSED (downloadServerCsv unwraps {"detail":...}
    // before throwing), so the guard that matters here is the empty case — a
    // status we don't classify and no server message must still say something.
    for (const s of [400, 500, 418, undefined]) {
      const msg = csvErrorMessage(s, "");
      expect(msg).toMatch(/couldn't build that csv/i);
      expect(msg).not.toMatch(/[{}]/);
    }
  });
});
