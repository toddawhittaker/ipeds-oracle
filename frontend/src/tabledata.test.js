import { describe, it, expect, vi } from "vitest";
import { parseNum, toCsv, extractTable, chartSpecFromTable, countMarkdownTables, columnIsNumeric, sortRows, sortedIndices, downloadServerCsv } from "./tabledata.js";

// Pure helpers behind per-table CSV export and "Chart this". The DOM side-effect
// (downloadCsv wiring an <a> and triggering a real browser download) is browser
// truth exercised by chat-happy-path.spec.js; everything here is input->output.

describe("parseNum", () => {
  const cases = [
    ["1,234", 1234],
    ["$1,234.5", 1234.5],
    ["45%", 45],
    ["  12  ", 12],
    ["3.14", 3.14],
    ["-7", -7],
  ];
  for (const [input, want] of cases) {
    it(`"${input}" -> ${want}`, () => expect(parseNum(input)).toBe(want));
  }

  const nans = ["", "-", "abc", "1.2.3", null, undefined];
  for (const input of nans) {
    it(`${JSON.stringify(input)} -> NaN`, () => expect(parseNum(input)).toBeNaN());
  }
});

describe("toCsv", () => {
  it("joins rows with CRLF and cells with commas", () => {
    expect(toCsv(["a", "b"], [["1", "2"], ["3", "4"]]))
      .toBe("a,b\r\n1,2\r\n3,4");
  });

  it("quotes cells containing comma, quote, or newline and doubles inner quotes", () => {
    expect(toCsv(["x", "y"], [["a,b", 'he said "hi"'], ["line\nbreak", "ok"]]))
      .toBe('x,y\r\n"a,b","he said ""hi"""\r\n"line\nbreak",ok');
  });

  it("renders a null/undefined cell as an empty field", () => {
    expect(toCsv(["a", "b"], [[null, undefined]])).toBe("a,b\r\n,");
  });

  // CSV FORMULA INJECTION GUARD. Every header comes from the model's own
  // Markdown table headers and every cell is model-transcribed text — a real
  // injection channel, not a theoretical one, since the model's input includes
  // the user's question. A cell opened by Excel/Sheets whose first character is
  // one of =, +, @, TAB, or CR is evaluated as a formula rather than shown as
  // text, so each is prefixed with a leading single quote (') to force
  // text-literal interpretation, matching the server-side guard in
  // backend/app/routers/chat.py's `download_csv`. Regression this catches: a
  // row or header cell starting with one of these lands in the exported CSV
  // unmodified and a spreadsheet evaluates it as a formula/DDE command on open.
  it("prefixes a ROW cell beginning with =, +, @, TAB, or CR with a leading single quote", () => {
    expect(toCsv(["a"], [["=1+2"]])).toBe("a\r\n'=1+2");
    expect(toCsv(["a"], [["+1+2"]])).toBe("a\r\n'+1+2");
    expect(toCsv(["a"], [["@SUM(1)"]])).toBe("a\r\n'@SUM(1)");
    expect(toCsv(["a"], [["\tx"]])).toBe("a\r\n'\tx");
    // A leading CR also needs RFC-4180 quoting (see the \r test below), so the
    // guarded cell is quoted too — the apostrophe must land INSIDE the quotes,
    // ahead of the CR, or the guard is stripped/reordered by the quoting step.
    expect(toCsv(["a"], [["\rx"]])).toBe('a\r\n"\'\rx"');
  });

  it("prefixes a HEADER cell (a model-written SQL alias) the same way a row cell is guarded", () => {
    // The header comes from parsing the model's Markdown table, so it is just
    // as attacker-influenced as a row value — a formula-shaped alias must not
    // reach the spreadsheet unguarded either.
    expect(toCsv(["=SUM(A1:A9)", "normal"], [["1", "2"]]))
      .toBe("'=SUM(A1:A9),normal\r\n1,2");
  });

  // JUDGEMENT CALL: a leading "-" is also how an ordinary negative number is
  // written, and IPEDS results legitimately contain negatives (e.g. a
  // year-over-year delta). Blanket-prefixing every "-1234" would put a stray
  // apostrophe in front of completely ordinary data in EVERY export. What
  // distinguishes a dangerous cell from a harmless one isn't the leading
  // character alone — "-1234" parses as nothing but a signed number, so a
  // spreadsheet's formula evaluator has nothing to execute; "-1+cmd|'
  // /C calc'!A0" does not parse as a plain number, and that's exactly the
  // shape a real DDE-injection payload needs (extra tokens after the sign).
  // Decision: guard a leading "-" only when the WHOLE cell isn't a plain
  // signed integer/decimal; "=", "+", "@", TAB, and CR have no legitimate use
  // as a leading character in this app's data (nothing here is ever written
  // with an explicit leading "+"), so those stay unconditionally guarded.
  it("does NOT guard a leading '-' when the whole cell is a plain negative number", () => {
    expect(toCsv(["a"], [["-1234"]])).toBe("a\r\n-1234");
    expect(toCsv(["a"], [["-12.5"]])).toBe("a\r\n-12.5");
  });

  it("DOES guard a leading '-' when the cell is not a plain number (the injection shape)", () => {
    expect(toCsv(["a"], [["-1+cmd|' /C calc'!A0"]]))
      .toBe("a\r\n'-1+cmd|' /C calc'!A0");
  });

  // THE \r GAP: `esc`'s quoting regex only matched comma/quote/"\n", so a cell
  // holding a bare CR (no paired LF) was emitted UNQUOTED — but a lone \r is
  // itself a record separator under RFC 4180, so an unquoted one silently
  // splits what should be one row into two when re-parsed.
  it("quotes a cell containing a bare \\r (RFC-4180 record separator), not just \\n", () => {
    expect(toCsv(["a", "b"], [["line\rbreak", "ok"]]))
      .toBe('a,b\r\n"line\rbreak",ok');
  });

  // REGRESSION GUARD: an ordinary export (strings, a comma-bearing string that
  // already required quoting, a plain negative number, a positive integer, and
  // an empty cell) must come out BYTE-IDENTICAL to today's output — the guard
  // above must never touch a cell that doesn't need it.
  it("leaves an ordinary row untouched (strings/ints/negative number/empty cell)", () => {
    expect(toCsv(
      ["Institution", "Awards", "Delta", "Note"],
      [["Ohio State University", "1,234", "-42", ""]],
    )).toBe('Institution,Awards,Delta,Note\r\n' +
            'Ohio State University,"1,234",-42,');
  });
});

describe("extractTable", () => {
  // A minimal hast tree of the shape react-markdown hands the table component.
  const cell = (tag, text) => ({ tagName: tag, children: [{ type: "text", value: text }] });
  const node = {
    children: [
      { tagName: "thead", children: [
        { tagName: "tr", children: [cell("th", "Name"), cell("th", "Count")] },
      ] },
      { tagName: "tbody", children: [
        { tagName: "tr", children: [
          // a nested element in the cell exercises hastText's recursion
          { tagName: "td", children: [{ tagName: "strong", children: [{ type: "text", value: "Ohio State" }] }] },
          cell("td", "1,234"),
        ] },
        { tagName: "tr", children: [cell("td", "Miami"), cell("td", "567")] },
      ] },
    ],
  };

  it("pulls headers from the th row and data from the td rows", () => {
    const { headers, rows } = extractTable(node);
    expect({ headers, rows }).toEqual({
      headers: ["Name", "Count"],
      rows: [["Ohio State", "1,234"], ["Miami", "567"]],
    });
  });

  it("also returns the parallel <td> hast nodes for inline rendering", () => {
    const { cellNodes } = extractTable(node);
    // One entry per BODY row (headers excluded), each the row's <td> elements.
    expect(cellNodes.length).toBe(2);
    expect(cellNodes[0].map((td) => td.tagName)).toEqual(["td", "td"]);
    // The first cell keeps its nested <strong>, so the display can render it bold.
    expect(cellNodes[0][0].children[0].tagName).toBe("strong");
  });
});

describe("sortedIndices", () => {
  const rows = [["B", "10"], ["A", "2"], ["C", "100"]];
  it("returns identity order for a null column", () =>
    expect(sortedIndices(rows, null, null, false)).toEqual([0, 1, 2]));
  it("numeric asc orders by value (a permutation, not the rows)", () =>
    expect(sortedIndices(rows, 1, "asc", true)).toEqual([1, 0, 2])); // 2,10,100
  it("string asc orders the label column", () =>
    expect(sortedIndices(rows, 0, "asc", false)).toEqual([1, 0, 2])); // A,B,C
});

describe("chartSpecFromTable", () => {
  it("returns null with fewer than two rows or no headers", () => {
    expect(chartSpecFromTable(["A", "B"], [["1", "2"]])).toBeNull();
    expect(chartSpecFromTable([], [])).toBeNull();
  });

  it("returns null when there is no numeric series to plot", () => {
    expect(chartSpecFromTable(["City", "Note"], [["A", "x"], ["B", "y"]])).toBeNull();
  });

  it("plots a bar with a text category as x and the numeric column as the series", () => {
    const spec = chartSpecFromTable(
      ["University", "Degrees"],
      [["Ohio State", "1,234"], ["Miami", "567"]],
    );
    expect(spec).toEqual({
      type: "bar",
      x: "University",
      y: ["Degrees"],
      data: [
        { University: "Ohio State", Degrees: 1234 },
        { University: "Miami", Degrees: 567 },
      ],
    });
  });

  it("uses a time-like dimension column as the x-axis and switches to a line", () => {
    const spec = chartSpecFromTable(
      ["Year", "Count"],
      [["2020", "10"], ["2021", "20"], ["2022", "30"]],
    );
    expect(spec.type).toBe("line");
    expect(spec.x).toBe("Year");
    expect(spec.y).toEqual(["Count"]);
  });

  it("drops a rank/index column (named or a plain 1..n sequence) from the series", () => {
    // "Rank" matches the dimension-name regex; a bare 1..n "Seq" hits the
    // sequence-detection arm. Neither should appear as a plotted series.
    const named = chartSpecFromTable(
      ["Rank", "University", "Degrees"],
      [["1", "Ohio State", "1,234"], ["2", "Miami", "567"]],
    );
    expect(named.x).toBe("University");
    expect(named.y).toEqual(["Degrees"]);

    const seq = chartSpecFromTable(
      ["Seq", "City", "Pop"],
      [["1", "Columbus", "900"], ["2", "Cleveland", "370"], ["3", "Cincinnati", "300"]],
    );
    expect(seq.y).toEqual(["Pop"]);
  });

  it("plots a measure whose name merely ENDS in 'id', like Average Aid", () => {
    // THE REGRESSION: the dimension-name alternative was `.*[ _]?id` with an
    // OPTIONAL separator, which collapses to plain `.*id` — so any header
    // ending in those two letters was treated as an identifier. "Average Aid",
    // "Total Aid", "Paid" and "Grid" all matched, chartSpecFromTable found no
    // series and returned null, and BOTH "Chart this" and compare mode
    // disappeared from every financial-aid answer. Student Financial Aid is a
    // first-class IPEDS survey family, so these are real headers.
    const spec = chartSpecFromTable(
      ["Institution", "Average Aid", "Total Aid"],
      [["Ohio State", "12,345", "9,000,000"], ["Miami", "9,876", "4,000,000"]],
    );
    expect(spec).not.toBeNull();
    expect(spec.x).toBe("Institution");
    expect(spec.y).toEqual(["Average Aid", "Total Aid"]);
  });

  it("still treats a SEPARATED id header as a dimension", () => {
    // The other half of the bound: the fix must not stop recognising genuine
    // identifier columns. A cap that over-corrected would plot unitids as a
    // data series — the failure the regex exists to prevent.
    const spec = chartSpecFromTable(
      ["University", "Unit ID", "Degrees"],
      [["Ohio State", "204796", "1,234"], ["Miami", "204024", "567"]],
    );
    expect(spec.y).toEqual(["Degrees"]);
    expect(spec.y).not.toContain("Unit ID");
  });
});

describe("countMarkdownTables", () => {
  const one = "Intro.\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n";
  const two = one + "\nMore.\n\n| X | Y |\n| :-- | --: |\n| a | b |\n";
  it("counts a single table", () => expect(countMarkdownTables(one)).toBe(1));
  it("counts two tables", () => expect(countMarkdownTables(two)).toBe(2));
  it("is 0 with no table", () => expect(countMarkdownTables("just prose, no pipes")).toBe(0));
  it("ignores a --- horizontal rule (no pipe)", () =>
    expect(countMarkdownTables("above\n\n---\n\nbelow")).toBe(0));
  it("does not count a data row that contains dashes", () =>
    expect(countMarkdownTables("| Name | Note |\n| --- | --- |\n| A-1 | in-state |")).toBe(1));
  it("handles non-strings", () => expect(countMarkdownTables(null)).toBe(0));
});

describe("columnIsNumeric", () => {
  const rows = [["Ohio State", "1,234"], ["Miami", "567"], ["Kent", "n/a"]];
  it("true for a mostly-numeric column", () => expect(columnIsNumeric(rows, 1)).toBe(true));
  it("false for a text column", () => expect(columnIsNumeric(rows, 0)).toBe(false));
  it("false with no rows", () => expect(columnIsNumeric([], 0)).toBe(false));
});

describe("sortRows", () => {
  const rows = [["B", "10"], ["A", "2"], ["C", "100"]];
  it("numeric asc orders by value, not lexically (100 after 10)", () =>
    expect(sortRows(rows, 1, "asc", true).map((r) => r[1])).toEqual(["2", "10", "100"]));
  it("numeric desc reverses", () =>
    expect(sortRows(rows, 1, "desc", true).map((r) => r[1])).toEqual(["100", "10", "2"]));
  it("string asc sorts the label column", () =>
    expect(sortRows(rows, 0, "asc", false).map((r) => r[0])).toEqual(["A", "B", "C"]));
  it("null column returns original order (a fresh copy)", () => {
    const out = sortRows(rows, null, null, false);
    expect(out).toEqual(rows);
    expect(out).not.toBe(rows);
  });
  it("is stable on ties (equal keys keep input order)", () => {
    const tied = [["x", "5"], ["y", "5"], ["z", "5"]];
    expect(sortRows(tied, 1, "asc", true).map((r) => r[0])).toEqual(["x", "y", "z"]);
  });
  it("sorts blank/non-numeric cells to the end in both directions", () => {
    const withBlank = [["a", "3"], ["b", ""], ["c", "1"]];
    expect(sortRows(withBlank, 1, "asc", true).map((r) => r[0])).toEqual(["c", "a", "b"]);
    expect(sortRows(withBlank, 1, "desc", true).map((r) => r[0])).toEqual(["a", "c", "b"]);
  });
});

describe("downloadServerCsv", () => {
  // It now FETCHES rather than clicking a bare <a href>. That is the whole
  // point: the old form had no `download` attribute and no `target`, so a
  // 400/404/429/504 from the export endpoint replaced the chat view with a raw
  // JSON error page — and a slow export timing out is the likeliest failure.
  const okResponse = () => ({
    ok: true,
    blob: async () => new Blob(["a,b\n1,2\n"], { type: "text/csv" }),
  });

  function stubDom() {
    const anchors = [];
    const make = document.createElement.bind(document);
    const spy = vi.spyOn(document, "createElement").mockImplementation((tag) => {
      const el = make(tag);
      if (tag === "a") { el.click = () => {}; anchors.push(el); }
      return el;
    });
    if (!URL.createObjectURL) URL.createObjectURL = () => "blob:stub";
    if (!URL.revokeObjectURL) URL.revokeObjectURL = () => {};
    return { anchors, restore: () => spy.mockRestore() };
  }

  it("requests the message CSV URL, adding ?cols only for a positive integer",
    async () => {
      const calls = [];
      globalThis.fetch = vi.fn(async (url) => { calls.push(url); return okResponse(); });
      const dom = stubDom();
      await downloadServerCsv(7, 4);
      await downloadServerCsv(7);        // no column hint
      await downloadServerCsv(7, 0);     // non-positive → no ?cols
      dom.restore();
      expect(calls).toEqual([
        "/api/chat/messages/7/download.csv?cols=4",
        "/api/chat/messages/7/download.csv",
        "/api/chat/messages/7/download.csv",
      ]);
    });

  it("names the downloaded file instead of navigating to it", async () => {
    globalThis.fetch = vi.fn(async () => okResponse());
    const dom = stubDom();
    await downloadServerCsv(42, 3);
    dom.restore();
    // A `download` attribute is what makes this a download rather than a
    // navigation — its absence is why an error page used to replace the app.
    expect(dom.anchors[0].getAttribute("download")).toBe("ipeds_result_42.csv");
  });

  it("THROWS on a failed export instead of silently navigating away", async () => {
    globalThis.fetch = vi.fn(async () => ({
      ok: false,
      status: 504,
      text: async () => JSON.stringify({ detail: "The query took too long to export." }),
    }));
    const dom = stubDom();
    await expect(downloadServerCsv(7, 2)).rejects.toMatchObject({
      status: 504,
      detail: "The query took too long to export.",
    });
    dom.restore();
    // and nothing was handed to the browser to open
    expect(dom.anchors).toHaveLength(0);
  });

  it("still throws usefully when the error body isn't JSON", async () => {
    globalThis.fetch = vi.fn(async () => ({
      ok: false, status: 502, text: async () => "<html>bad gateway</html>",
    }));
    await expect(downloadServerCsv(7)).rejects.toMatchObject({ status: 502 });
  });
});
