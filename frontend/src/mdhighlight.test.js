import { describe, it, expect } from "vitest";
import { highlight } from "./mdhighlight.js";

const join = (src) => highlight(src).map((s) => s.text).join("");
const classesOf = (src, cls) => highlight(src).filter((s) => s.cls === cls).map((s) => s.text);

describe("highlight — lossless invariant (the regression that matters)", () => {
  // The composer VALUE is the raw string; this layer only colors a mirror of it.
  // If concatenating the segments ever differs from the input, the overlay would
  // desync from the textarea (and worse, imply the source changed). Guard it hard.
  const docs = [
    "",
    "\n",
    "plain text with no markup",
    "# Enrollment trends\n\n- Compare undergraduate enrollment\n- Identify five-year changes\n\n---\n\n**Important:** Use public IPEDS data only.",
    "mix *em* and **strong** and ~~gone~~ and `code` and [a](http://x) here",
    "> quoted **bold**\n1. first\n2) second\n  + nested",
    "```sql\nSELECT 1\n```",
    "unbalanced ** and * and ~~ and ` and [oops](",
    "#nospace is not a heading",
    "trailing newline\n",
    "多字节 **粗体** テスト",   // multibyte stays intact
  ];
  it.each(docs)("round-trips exactly: %o", (src) => {
    expect(join(src)).toBe(src);
  });
});

describe("highlight — construct classification", () => {
  it("marks a heading's # (and spaces) as a dim marker, text as heading", () => {
    expect(classesOf("## Results here", "marker")).toEqual(["## "]);
    expect(classesOf("## Results here", "heading")).toEqual(["Results here"]);
  });

  it("does NOT treat '#' without a following space as a heading", () => {
    expect(classesOf("#nope", "heading")).toEqual([]);
  });

  it("marks list markers (-, *, +, 1., 2)) but keeps the item text", () => {
    expect(classesOf("- item", "list")).toEqual(["- "]);
    expect(classesOf("* item", "list")).toEqual(["* "]);
    expect(classesOf("+ item", "list")).toEqual(["+ "]);
    expect(classesOf("1. item", "list")).toEqual(["1. "]);
    expect(classesOf("2) item", "list")).toEqual(["2) "]);
  });

  it("marks a blockquote prefix", () => {
    expect(classesOf("> hello", "quote")).toEqual(["> "]);
  });

  it("renders an HR line as one hr segment (whole line)", () => {
    expect(classesOf("---", "hr")).toEqual(["---"]);
    expect(classesOf("***", "hr")).toEqual(["***"]);
    // one hyphen short is NOT an HR (character-level: --- backspaced to --)
    expect(classesOf("--", "hr")).toEqual([]);
  });

  it("splits inline emphasis into markers + tinted content", () => {
    expect(classesOf("a **bold** b", "marker")).toEqual(["**", "**"]);
    expect(classesOf("a **bold** b", "strong")).toEqual(["bold"]);
    expect(classesOf("a *em* b", "emph")).toEqual(["em"]);
    expect(classesOf("a ~~no~~ b", "strike")).toEqual(["no"]);
    expect(classesOf("a `c` b", "code")).toEqual(["c"]);
  });

  it("splits a link into bracket/paren markers, text, and url", () => {
    expect(classesOf("see [docs](https://x.y)", "link")).toEqual(["docs"]);
    expect(classesOf("see [docs](https://x.y)", "linkurl")).toEqual(["https://x.y"]);
  });

  it("treats fenced code lines as fence delimiters + code content", () => {
    const src = "```\nkeep raw *literal*\n```";
    expect(classesOf(src, "fence")).toEqual(["```", "```"]);
    expect(classesOf(src, "code")).toEqual(["keep raw *literal*"]);
  });

  it("leaves an unbalanced delimiter as plain (literal reveal)", () => {
    // no 'strong' segment for a lone ** — it reads literally
    expect(classesOf("a ** b", "strong")).toEqual([]);
  });
});
