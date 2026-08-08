import { describe, expect, it } from "vitest";

import { boundaryFallback, CRASH_TITLE } from "./errorcopy.js";

// THE CONTRADICTION THIS PINS: App.jsx arms a `beforeunload` guard whenever
// inflight.hasLiveTurn() is true — including through a STOPPED turn, because
// that note promises the answer will still be saved, and "a refresh is what
// breaks the promise". Meanwhile the crash card's primary action was
// window.location.reload(), unconditionally. The boundary could not even rely on
// the guard to catch it: by the time the fallback renders, <App/> has been
// replaced and the listener has unmounted with it.
//
// A pure decision, so it needs no browser. The boundary is a class component and
// reads the module-level `inflight` store directly.

describe("boundaryFallback", () => {
  it("never offers a bare reload as the primary action while a turn is live", () => {
    const f = boundaryFallback({ liveTurn: true });
    expect(f.primary.reload).toBe(false);
    // The destructive route stays REACHABLE — the page may genuinely be stuck,
    // and trapping someone in a broken card is its own failure. It is just no
    // longer the recommended one.
    expect(f.secondary).not.toBeNull();
    expect(f.secondary.reload).toBe(true);
  });

  it("says WHY waiting matters, not merely that it does", () => {
    // "Reloading is risky" is unactionable; naming the loss lets the reader
    // decide. The answer really is saved only once the turn finishes.
    expect(boundaryFallback({ liveTurn: true }).body).toMatch(/discard|lost|only once it finishes/i);
  });

  it("keeps the plain reload when nothing is in flight", () => {
    // The regression guard in the other direction: an over-broad fix that always
    // demoted reload would make ordinary crash recovery two clicks for no reason.
    const f = boundaryFallback({ liveTurn: false });
    expect(f.primary.reload).toBe(true);
    expect(f.secondary).toBeNull();
  });

  it("defaults to the settled copy when told nothing", () => {
    // The boundary reads a store that could be empty or unavailable; the safe
    // default is today's behaviour, not the alarming one.
    expect(boundaryFallback()).toEqual(boundaryFallback({ liveTurn: false }));
    expect(boundaryFallback({})).toEqual(boundaryFallback({ liveTurn: false }));
  });

  it("exports a title both states share", () => {
    expect(CRASH_TITLE).toBeTruthy();
  });
});
