import { describe, expect, it } from "vitest";

import {
  ROW_CAP,
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

  it("cautions when some values did not reproduce, leading with the misses", () => {
    const note = tableTrustNote({ status: "partial", cellsChecked: 22, cellsMatched: 9 });
    expect(note.tone).toBe("warn");
    // 13 failed, not 9 — leading with the count that PASSED would bury the
    // actionable half under a reassuring number.
    expect(note.text).toContain("13");
    // …and it names the total, so "13 of 22" can't read as "everything".
    expect(note.text).toContain("22");
  });

  it("cautions when nothing reproduced, without claiming the numbers are wrong", () => {
    const note = tableTrustNote({ status: "unmatched", cellsChecked: 15, cellsMatched: 0 });
    expect(note.tone).toBe("warn");
    expect(note.text).toContain("15");
    // THE wording contract, and the reason a caution was safe to ship at all: no
    // checker reproduces every legitimate derivation, so a non-match means THIS
    // CHECK could not re-derive the number — not that the number is wrong. A
    // confident accusation on a correct answer costs far more than this
    // under-claim. Assert the promise is about reproduction, never correctness.
    expect(note.text).toMatch(/could not be reproduced/i);
    expect(note.text).not.toMatch(/\b(wrong|incorrect|error|invalid)\b/i);
    expect(note.title).toMatch(/not as proof of an error/i);
  });

  it("agrees in number, both when some failed and when all did", () => {
    expect(tableTrustNote({ status: "partial", cellsChecked: 40, cellsMatched: 39 }).text)
      .toMatch(/^1 of 40 values\b/);
    expect(tableTrustNote({ status: "unmatched", cellsChecked: 1, cellsMatched: 0 }).text)
      .toMatch(/^1 value in this answer\b/);
    expect(tableTrustNote({ status: "unmatched", cellsChecked: 4, cellsMatched: 0 }).text)
      .toMatch(/^4 values in this answer\b/);
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
    const caption = truncationCaption(true);
    expect(caption).toContain(String(ROW_CAP));
    expect(caption).toMatch(/larger/i);
    expect(caption).not.toMatch(/\bof\s+[\d,]+/i);
  });

  it("is empty for a complete result — no caption is the honest default", () => {
    expect(truncationCaption(false)).toBe("");
  });
});

describe("sortScopeNote", () => {
  it("is silent until a sort is actually active", () => {
    expect(sortScopeNote({ truncated: true, sorted: false, rowsShown: 200 })).toBe("");
  });

  it("warns that a sorted page is not a ranking of the full result", () => {
    // The sharpest edge in the feature: sorting a page and reading off the top
    // answers "the biggest of the first 200", under an authoritative caret.
    const note = sortScopeNote({ truncated: true, sorted: true, rowsShown: 200 });
    expect(note).toMatch(/not a ranking/i);
    expect(note).toContain(String(ROW_CAP));
    expect(note).toMatch(/csv/i);
  });

  it("keeps the mild wording when the table is complete", () => {
    const note = sortScopeNote({ truncated: false, sorted: true, rowsShown: 27 });
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
