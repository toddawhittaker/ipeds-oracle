import { describe, expect, it } from "vitest";

import {
  ROW_CAP,
  canCaptionTruncation,
  csvErrorMessage,
  csvLabel,
  sortNoteTone,
  sortScopeNote,
  truncationCaption,
} from "./tabletruth.js";

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
