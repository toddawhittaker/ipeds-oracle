// What the crash card is allowed to offer, given what is in flight.
//
// The boundary's fallback offers "Reload the page" — and a reload is the
// DOCUMENTED data-loss path for a running turn. App.jsx arms a `beforeunload`
// guard whenever inflight.hasLiveTurn() is true, and inflight deliberately keeps
// `live` true through a STOPPED turn as well, because that note promises the
// answer will still be saved. The comment on that guard says it outright: a
// refresh is what breaks the promise.
//
// So the outermost boundary was recommending, in its primary action, the one
// thing the rest of the app spends code preventing — and it can't rely on the
// guard to catch it, because by the time the fallback renders, <App/> has been
// replaced and the listener unmounted with it.
//
// Pure so the decision is testable without a browser; the boundary is a class
// component and reads `inflight` directly (it is a module-level store, readable
// outside React, which is the whole reason it can be reached from there).

export const CRASH_TITLE = "Something went wrong";

// No turn in flight: today's copy, unchanged. Reloading really is the safest
// reset from an unknown broken state.
const SETTLED = {
  body: "The page hit an unexpected error. Reloading usually fixes it. If it "
    + "keeps happening, let an administrator know.",
  primary: { label: "Reload the page", reload: true },
  secondary: null,
};

// A turn IS in flight. Reloading tears down the request, the server's generator
// is cancelled, and a brand-new conversation's row is removed by
// _delete_if_empty — so the answer is genuinely lost, not merely hidden.
// Waiting costs nothing and may cost the user nothing at all.
const LIVE = {
  body: "The page hit an unexpected error while an answer was still being "
    + "written. Reloading now would discard that answer — it is saved only once "
    + "it finishes. Wait a few seconds, then reload if the page is still stuck.",
  primary: { label: "Wait", reload: false },
  secondary: { label: "Reload anyway", reload: true },
};

/**
 * Choose the crash card's wording and actions.
 * @param {{ liveTurn?: boolean }} state
 * @returns {{ body: string, primary: { label: string, reload: boolean },
 *             secondary: { label: string, reload: boolean } | null }}
 */
export function boundaryFallback({ liveTurn = false } = {}) {
  return liveTurn ? LIVE : SETTLED;
}
