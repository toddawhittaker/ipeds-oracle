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
  it("builds the message CSV URL, adding ?cols only for a positive integer", () => {
    const anchors = [];
    const make = document.createElement.bind(document);
    const spy = vi.spyOn(document, "createElement").mockImplementation((tag) => {
      const el = make(tag);
      if (tag === "a") { el.click = () => {}; anchors.push(el); }
      return el;
    });
    downloadServerCsv(7, 4);
    downloadServerCsv(7);        // no column hint
    downloadServerCsv(7, 0);     // non-positive → no ?cols
    spy.mockRestore();
    expect(anchors.map((a) => a.getAttribute("href"))).toEqual([
      "/api/chat/messages/7/download.csv?cols=4",
      "/api/chat/messages/7/download.csv",
      "/api/chat/messages/7/download.csv",
    ]);
  });
});
