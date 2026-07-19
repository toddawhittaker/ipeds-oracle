// Pure predicate for the chat "type anywhere" behavior: a printable character
// typed while nothing editable has focus should land in the composer (the
// ChatGPT/Claude convention), so a user never has to click the box before
// asking. Deterministic input->output, so it lives here under vitest per
// CLAUDE.md's test pyramid; Chat.jsx owns the DOM listener that feeds it.
//
// The contract (each clause guards a real misfire):
//  - Only single printable characters. Named keys ("Enter", "Escape",
//    "ArrowDown", "F5"...) have key.length > 1 and must pass through -- they
//    drive scrolling, dialogs, and the browser itself.
//  - Never with ctrl/meta/alt held: those are shortcuts (copy, find, switch
//    tab), not typing. Shift alone is fine -- that's how capitals are typed.
//  - Never when focus is already somewhere editable (input, textarea, select,
//    contenteditable): the user is typing THERE. Redirecting would steal
//    mid-word keystrokes from the admin search box or the inline title editor.
//  - Never from inside an open dialog (ConfirmModal): its focus trap owns the
//    keyboard; yanking focus out of a confirm prompt would break WCAG 2.4.3
//    and could land a stray "y" in the composer behind the modal.
//  - Space is special-cased: on a focused button/link/summary, Space
//    ACTIVATES the control -- redirecting it would both steal the activation
//    and scroll-jack. Every other printable char is inert on those elements,
//    so redirecting it is safe (and what users expect after clicking a
//    sidebar chat: just start typing).
const EDITABLE_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT"]);
const SPACE_ACTIVATES = new Set(["BUTTON", "A", "SUMMARY"]);

export function shouldRedirectTyping({ key, ctrlKey, metaKey, altKey },
                                     { tag, editable, inDialog }) {
  if (typeof key !== "string" || key.length !== 1) return false;
  if (ctrlKey || metaKey || altKey) return false;
  if (inDialog) return false;
  if (editable || EDITABLE_TAGS.has(tag)) return false;
  if (key === " " && SPACE_ACTIVATES.has(tag)) return false;
  return true;
}

// DOM adapter: derive the predicate's target facts from an event target.
// Kept tiny (and separately testable via jsdom if ever needed) so the
// decision logic above stays 100% pure.
export function targetInfo(el) {
  return {
    tag: el?.tagName || "",
    editable: !!el?.isContentEditable,
    inDialog: !!(el?.closest && el.closest("[role=dialog]")),
  };
}
