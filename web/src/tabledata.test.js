import { describe, it, expect } from "vitest";
import { parseNum, toCsv, extractTable, chartSpecFromTable } from "./tabledata.js";

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
    expect(extractTable(node)).toEqual({
      headers: ["Name", "Count"],
      rows: [["Ohio State", "1,234"], ["Miami", "567"]],
    });
  });
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
