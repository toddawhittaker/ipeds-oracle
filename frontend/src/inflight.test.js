import { describe, expect, it, vi } from "vitest";

import { createInflightRegistry } from "./inflight.js";

// The state machine behind "show me my question while the answer is still
// coming". Every case below names a concrete regression; the store is small, so
// the risk here is not complexity but the handful of transitions where getting
// it backwards silently breaks a pinned contract elsewhere.

const start = (r, q = "how many?", conversationId = 3) =>
  r.startTurn({ question: q, conversationId });

describe("inflight registry", () => {
  it("shows a started turn in its conversation, and nowhere else", () => {
    const r = createInflightRegistry();
    start(r, "q", 3);
    expect(r.pendingFor(3).map((t) => t.question)).toEqual(["q"]);
    expect(r.pendingFor(5)).toEqual([]);
    // A brand-new chat has no id yet, so it belongs to no conversation until
    // attachConversation runs — pendingFor(null) must not match it.
    expect(r.pendingFor(null)).toEqual([]);
  });

  it("keeps the stopped turn's stream armed but stops showing it", () => {
    // THE STOPPED-TURN CONTRACT, both halves.
    //
    // Stop generating is abandon-and-drain: the request keeps running so the
    // server still persists the answer. So the placeholder must go (the user
    // deliberately stopped watching, and Chat.jsx's stopped note takes its
    // place) while the unload guard stays armed — refreshing now would kill the
    // very turn the note promises will be saved.
    const r = createInflightRegistry();
    const k = start(r);
    r.hideTurn(k);
    expect(r.pendingFor(3)).toEqual([]);
    expect(r.hasLiveTurn()).toBe(true);
  });

  it("does not schedule a reload for a turn the user stopped", () => {
    // THE YANK. Without this, a stopped turn settles, bumps the reload counter,
    // the viewer's loader refetches, and the finished answer replaces the
    // "Stopped." note — pulling a full answer under someone who chose to stop,
    // which is the same yank the scroll containment exists to prevent.
    const r = createInflightRegistry();
    const k = start(r);
    r.hideTurn(k);
    const before = r.getSnapshot().reloads[3] ?? 0;
    r.settleTurn(k);
    expect(r.getSnapshot().reloads[3] ?? 0).toBe(before);
    expect(r.hasLiveTurn()).toBe(false);
  });

  it("does not schedule a reload when the owning view already rendered it", () => {
    // The viewer never left, so their own turn painted the answer. Reloading
    // would refetch the conversation the turn just created — which
    // midstream-nav.spec.js pins as `conv7.calls === 0`.
    const r = createInflightRegistry();
    const k = start(r);
    r.settleTurn(k, { rendered: true });
    expect(r.getSnapshot().reloads[3] ?? 0).toBe(0);
    expect(r.pendingFor(3)).toEqual([]);
  });

  it("schedules exactly one reload for an abandoned turn", () => {
    const r = createInflightRegistry();
    const k = start(r);
    r.settleTurn(k);
    expect(r.getSnapshot().reloads[3]).toBe(1);
    // Still shown: the placeholder has to survive until the refetch lands, or
    // it blinks out and back in.
    expect(r.pendingFor(3)).toHaveLength(1);
    // Settling twice must not double-count. The entry is still in the map (it
    // survives as a placeholder until the loader clears it), so this is not
    // covered by the unknown-key guard — a settled turn has to be inert in its
    // own right, or a stray second call refetches the conversation again.
    r.settleTurn(k);
    expect(r.getSnapshot().reloads[3]).toBe(1);
  });

  it("drops a turn that never learned its conversation id", () => {
    // A transport failure before the server's `conversation` event. There is no
    // conversation to draw it in and no reload to schedule, so keeping it would
    // strand an entry that nothing can ever clear.
    const r = createInflightRegistry();
    const k = r.startTurn({ question: "q", conversationId: null });
    r.settleTurn(k);
    expect(r.getSnapshot().turns).toEqual([]);
    expect(r.getSnapshot().reloads).toEqual({});
    expect(r.hasLiveTurn()).toBe(false);
  });

  it("backfills a brand-new chat's id, and refuses to resurrect a dead turn", () => {
    const r = createInflightRegistry();
    const k = r.startTurn({ question: "q", conversationId: null });
    r.attachConversation(k, 9);
    expect(r.pendingFor(9)).toHaveLength(1);

    // A late event must not revive a turn that was already settled — that would
    // put a permanent placeholder in a conversation whose answer is on screen.
    const dead = r.startTurn({ question: "q2", conversationId: null });
    r.settleTurn(dead);
    r.attachConversation(dead, 9);
    expect(r.pendingFor(9)).toHaveLength(1);
  });

  it("clears settled placeholders on load but keeps live ones", () => {
    // THE FLICKER THIS FEATURE EXISTS TO REMOVE. Returning mid-flight fetches
    // the thread as it stands right now; the turn is still running, so its
    // spinner must outlive that fetch. Only a SETTLED placeholder is superseded
    // by a full load.
    const r = createInflightRegistry();
    const liveKey = start(r, "still running", 3);
    const doneKey = start(r, "finished", 3);
    r.settleTurn(doneKey);

    r.clearForConversation(3);
    expect(r.pendingFor(3).map((t) => t.question)).toEqual(["still running"]);
    expect(r.getSnapshot().turns.some((t) => t.key === liveKey)).toBe(true);
  });

  it("refetches on an explicit check, and only then", () => {
    // THE STOPPED NOTE'S WAY OUT, and its counterweight.
    //
    // settleTurn must keep NOT bumping for a stopped turn (the yank test
    // above), which is exactly what left the note's "check in a moment"
    // impossible to act on — nothing in the app produced a refetch short of a
    // page reload, and a reload kills the turn the note promises will be saved.
    // reloadNow is that missing bump, on an explicit click.
    //
    // Both directions are pinned here because a future "fix" that simply makes
    // settleTurn bump for hidden turns would satisfy the first half alone while
    // re-introducing the yank.
    const r = createInflightRegistry();
    const k = start(r);
    r.hideTurn(k);
    r.settleTurn(k);
    expect(r.getSnapshot().reloads[3] ?? 0).toBe(0);

    r.reloadNow(3);
    expect(r.getSnapshot().reloads[3]).toBe(1);
    r.reloadNow(3);
    expect(r.getSnapshot().reloads[3]).toBe(2);
    // Another conversation is untouched — the loader keys on its own id.
    expect(r.getSnapshot().reloads[5]).toBeUndefined();
  });

  it("has nothing to check for a chat whose id never arrived", () => {
    // Stopping before the server's `conversation` event leaves convId null.
    // Chat.jsx withholds the button, and the registry is inert rather than
    // writing a `null` key that no loader will ever read.
    const r = createInflightRegistry();
    const before = r.getSnapshot();
    r.reloadNow(null);
    r.reloadNow(undefined);
    expect(r.getSnapshot()).toBe(before);
    expect(r.getSnapshot().reloads).toEqual({});
  });

  it("reports whether a stopped turn's stream is still open", () => {
    // The gate on offering "Check now". While the stream is open the answer is
    // not on disk, so a refetch returns the thread as it stood BEFORE the
    // question and replaces the stopped note with it — the reader's own
    // question vanishing. Hiding the turn must NOT read as finished: that is
    // the exact state the button is offered in.
    const r = createInflightRegistry();
    const k = start(r);
    r.hideTurn(k);
    expect(r.isTurnLive(k)).toBe(true);
    r.settleTurn(k);
    expect(r.isTurnLive(k)).toBe(false);
    // An unknown key (a turn from a previous page, or never started) reads as
    // finished — the note is only rendered for a turn this session stopped.
    expect(r.isTurnLive(999)).toBe(false);
    expect(r.isTurnLive(undefined)).toBe(false);

    // ...and the flag is the question, not the key. An ABANDONED turn (settled
    // without being stopped) stays in the map as a placeholder until the loader
    // supersedes it, so "is there an entry?" would answer this one wrong.
    const abandoned = start(r, "abandoned", 3);
    r.settleTurn(abandoned);
    expect(r.getSnapshot().turns.some((t) => t.key === abandoned)).toBe(true);
    expect(r.isTurnLive(abandoned)).toBe(false);
  });

  it("never lowers a reload counter", () => {
    // The counter is a useEffect dependency. If clearing could reset it, the dep
    // would oscillate and the loader would refetch forever.
    const r = createInflightRegistry();
    r.settleTurn(start(r));
    expect(r.getSnapshot().reloads[3]).toBe(1);
    r.clearForConversation(3);
    expect(r.getSnapshot().reloads[3]).toBe(1);
    r.settleTurn(start(r));
    expect(r.getSnapshot().reloads[3]).toBe(2);
  });

  it("returns a STABLE snapshot reference until something changes", () => {
    // The useSyncExternalStore footgun, and the cheapest way to hang the page:
    // a getSnapshot that builds a fresh object per call re-renders forever.
    const r = createInflightRegistry();
    const a = r.getSnapshot();
    expect(r.getSnapshot()).toBe(a);
    const k = start(r);
    const b = r.getSnapshot();
    expect(b).not.toBe(a);
    expect(r.getSnapshot()).toBe(b);
    r.settleTurn(k);
    expect(r.getSnapshot()).not.toBe(b);
  });

  it("notifies subscribers on change and stops after unsubscribe", () => {
    const r = createInflightRegistry();
    const seen = vi.fn();
    const off = r.subscribe(seen);
    const k = start(r);
    expect(seen).toHaveBeenCalledTimes(1);
    r.settleTurn(k);
    expect(seen).toHaveBeenCalledTimes(2);
    off();
    r.settleTurn(start(r));
    expect(seen).toHaveBeenCalledTimes(2);
  });

  it("mints keys that cannot collide across component remounts", () => {
    // The counter used to be a useRef seeded at 0 inside Chat. Chat unmounts on
    // /admin, so a remount minted key 1 again while an abandoned turn still
    // held key 1 — harmless while keys were component-local, a real collision
    // now that they index a module-level map.
    const r = createInflightRegistry();
    const keys = [start(r), start(r), start(r)];
    expect(new Set(keys).size).toBe(3);
  });

  it("is inert for unknown keys", () => {
    // Every mutator is called from a long-lived stream closure that may outlive
    // the entry it started. None of them may throw or create state.
    const r = createInflightRegistry();
    expect(() => {
      r.hideTurn(999);
      r.attachConversation(999, 1);
      r.settleTurn(999);
      r.clearForConversation(999);
    }).not.toThrow();
    expect(r.getSnapshot().turns).toEqual([]);
    expect(r.getSnapshot().reloads).toEqual({});
  });

  it("keeps registries independent", () => {
    const a = createInflightRegistry();
    const b = createInflightRegistry();
    start(a);
    expect(a.pendingFor(3)).toHaveLength(1);
    expect(b.pendingFor(3)).toHaveLength(0);
    expect(b.hasLiveTurn()).toBe(false);
  });
});
