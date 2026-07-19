import { describe, it, expect } from "vitest";
import { shouldRedirectTyping, targetInfo } from "./typeahead.js";

// The type-anywhere-to-focus-the-composer predicate. Each case names the
// misfire it guards: stealing keystrokes from a focused input, hijacking
// keyboard shortcuts, breaking Space-activation on buttons, or typing
// "through" an open confirm dialog.

const KEY = (key, mods = {}) => ({ key, ctrlKey: false, metaKey: false, altKey: false, ...mods });
const AT = (tag, extra = {}) => ({ tag, editable: false, inDialog: false, ...extra });

describe("shouldRedirectTyping", () => {
  it("redirects a plain letter typed over the page body", () => {
    expect(shouldRedirectTyping(KEY("h"), AT("BODY"))).toBe(true);
  });

  it("redirects Shift-typed capitals (Shift alone is typing, not a shortcut)", () => {
    expect(shouldRedirectTyping(KEY("H", { shiftKey: true }), AT("BODY"))).toBe(true);
  });

  it("redirects a letter typed while a sidebar link has focus (post-click typing)", () => {
    expect(shouldRedirectTyping(KEY("w"), AT("A"))).toBe(true);
  });

  it("REGRESSION: never steals keystrokes from a focused text input", () => {
    expect(shouldRedirectTyping(KEY("a"), AT("INPUT"))).toBe(false);
    expect(shouldRedirectTyping(KEY("a"), AT("TEXTAREA"))).toBe(false);
    expect(shouldRedirectTyping(KEY("a"), AT("SELECT"))).toBe(false);
  });

  it("REGRESSION: never steals from contenteditable", () => {
    expect(shouldRedirectTyping(KEY("a"), AT("DIV", { editable: true }))).toBe(false);
  });

  it("REGRESSION: never hijacks ctrl/meta/alt shortcuts (copy, find, tab-switch)", () => {
    expect(shouldRedirectTyping(KEY("c", { ctrlKey: true }), AT("BODY"))).toBe(false);
    expect(shouldRedirectTyping(KEY("k", { metaKey: true }), AT("BODY"))).toBe(false);
    expect(shouldRedirectTyping(KEY("f", { altKey: true }), AT("BODY"))).toBe(false);
  });

  it("REGRESSION: named keys pass through (they drive scroll/dialogs/browser)", () => {
    for (const k of ["Enter", "Escape", "ArrowDown", "Tab", "F5", "Backspace"]) {
      expect(shouldRedirectTyping(KEY(k), AT("BODY")), k).toBe(false);
    }
  });

  it("REGRESSION: Space on a button/link/summary activates it — never redirected", () => {
    expect(shouldRedirectTyping(KEY(" "), AT("BUTTON"))).toBe(false);
    expect(shouldRedirectTyping(KEY(" "), AT("A"))).toBe(false);
    expect(shouldRedirectTyping(KEY(" "), AT("SUMMARY"))).toBe(false);
    // ...but Space over the plain body is just typing a space.
    expect(shouldRedirectTyping(KEY(" "), AT("BODY"))).toBe(true);
    // ...and a letter over a button IS redirected (inert there).
    expect(shouldRedirectTyping(KEY("q"), AT("BUTTON"))).toBe(true);
  });

  it("REGRESSION: never types through an open dialog's focus trap", () => {
    expect(shouldRedirectTyping(KEY("y"), AT("BUTTON", { inDialog: true }))).toBe(false);
  });

  it("tolerates a non-string key (synthetic events)", () => {
    expect(shouldRedirectTyping(KEY(undefined), AT("BODY"))).toBe(false);
  });
});

describe("targetInfo", () => {
  it("derives tag/editable/inDialog from a DOM-like element", () => {
    const el = {
      tagName: "BUTTON",
      isContentEditable: false,
      closest: (sel) => (sel === "[role=dialog]" ? {} : null),
    };
    expect(targetInfo(el)).toEqual({ tag: "BUTTON", editable: false, inDialog: true });
  });

  it("tolerates a null target (event fired on document)", () => {
    expect(targetInfo(null)).toEqual({ tag: "", editable: false, inDialog: false });
  });
});
