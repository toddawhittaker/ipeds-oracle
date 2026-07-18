import { describe, it, expect } from "vitest";
import { deleteAnnouncement } from "./announce.js";

// The delete-announcer wording — the pure branch/plural logic that used to be
// pinned as exact strings inside web/e2e/delete-focus.spec.js. The e2e spec
// still owns the browser truth (focus lands right, the live region actually
// re-announces); this owns WHAT it says.
describe("deleteAnnouncement", () => {
  const cases = [
    // open === true ignores remaining entirely -> composer-focus branch.
    { name: "open conversation", in: { title: "Chat Two", open: true, remaining: 5 },
      out: 'Deleted "Chat Two". Started a new chat.' },
    { name: "last one removed", in: { title: "Solo Chat", open: false, remaining: 0 },
      out: 'Deleted "Solo Chat". No chats remaining.' },
    // The singular/plural boundary — 1 is "chat", not "chats".
    { name: "exactly one remaining (singular)", in: { title: "Untitled", open: false, remaining: 1 },
      out: 'Deleted "Untitled". 1 chat remaining.' },
    { name: "many remaining (plural)", in: { title: "Chat Two", open: false, remaining: 2 },
      out: 'Deleted "Chat Two". 2 chats remaining.' },
  ];

  for (const c of cases) {
    it(c.name, () => {
      expect(deleteAnnouncement(c.in)).toBe(c.out);
    });
  }

  // Two deletes of same-titled conversations yield different strings because the
  // remaining-count is in the wording. (The toast host now re-announces each push
  // structurally regardless — delete-focus.spec.js proves that — so this is a UX
  // distinctness property, not a re-announce prerequisite.)
  it("consecutive same-title deletes differ because the count is in the text", () => {
    const first = deleteAnnouncement({ title: "Untitled", open: false, remaining: 1 });
    const second = deleteAnnouncement({ title: "Untitled", open: false, remaining: 0 });
    expect(first).not.toBe(second);
  });
});
