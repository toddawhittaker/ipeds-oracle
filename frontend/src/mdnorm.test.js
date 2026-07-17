import { describe, it, expect } from "vitest";
import { normalizeMarkdown } from "./mdnorm.js";

// normalizeMarkdown repairs the most common way an LLM breaks a GFM table: a
// header/delimiter column-count mismatch that makes the whole table refuse to
// render. Pure string -> string; the actual rendering (react-markdown turning
// the repaired source into a <table>) is browser truth in chat-happy-path.
describe("normalizeMarkdown", () => {
  const cases = [
    {
      name: "no pipes at all is returned untouched",
      in: "Just a sentence.\nAnd another.",
      out: "Just a sentence.\nAnd another.",
    },
    {
      name: "null passes through (guard)",
      in: null,
      out: null,
    },
    {
      name: "a pipe line NOT followed by a delimiter is left alone",
      in: "| a | b |\nplain text",
      out: "| a | b |\nplain text",
    },
    {
      name: "delimiter shorter than the header is rebuilt to the header's column count",
      in: "| Year | State | Count |\n|---|---|",
      out: "| Year | State | Count |\n| --- | --- | --- |",
    },
    {
      name: "alignment cells the model DID provide are preserved during the rebuild",
      in: "| A | B | C |\n|:---:|:---|",
      out: "| A | B | C |\n| :---: | :--- | --- |",
    },
    {
      name: "a well-formed delimiter is normalized to spaced pipes",
      in: "| A | B |\n|---|---|",
      out: "| A | B |\n| --- | --- |",
    },
    {
      name: "a blank line is inserted before a table that abuts a text paragraph",
      in: "Intro paragraph.\n| A | B |\n| --- | --- |",
      out: "Intro paragraph.\n\n| A | B |\n| --- | --- |",
    },
  ];

  for (const c of cases) {
    it(c.name, () => {
      expect(normalizeMarkdown(c.in)).toBe(c.out);
    });
  }
});
