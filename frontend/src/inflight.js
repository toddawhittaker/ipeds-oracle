// Turns that are still streaming, tracked OUTSIDE React.
//
// A chat turn lives entirely in the browser until it finishes: the server writes
// the user AND assistant rows in one transaction at the very END of the stream
// (routers/chat.py `_persist`), so mid-flight there is nothing to fetch — no
// question, no progress, no partial answer.
//
// Meanwhile the client throws its own copy away. Navigating bumps `turnToken`,
// which makes `isMine()` false so every later view write is dropped, and the
// render-time reset clears `messages`. Both are deliberate (see
// e2e/midstream-nav.spec.js — a stale turn must never bleed into the
// conversation you moved to). The result was that leaving a running question and
// coming back showed the thread as it was BEFORE you asked, and — because
// nothing refetches the open thread — it stayed that way even after the answer
// landed.
//
// This module is the small amount of state that has to survive that. It holds
// the question text and enough bookkeeping to (a) draw a placeholder while the
// turn runs, (b) trigger exactly one reload when it lands, and (c) warn before a
// refresh. It deliberately does NOT park the live trace (status/thinking/SQL) —
// a spinner is enough, and replaying a stale "Running query…" would be a lie.
//
// WHY MODULE SCOPE, not React state: Chat UNMOUNTS when you navigate to /admin
// (App.jsx swaps the main content), which is precisely the navigation this
// feature exists for. Component state cannot survive it. This is the first
// module-level store in the app; api.js's `setUnauthenticatedHandler` is the
// nearest precedent.

/**
 * Build an independent registry. The app uses the `inflight` singleton below;
 * tests construct their own so no case can leak state into another — cheaper and
 * clearer than exporting a test-only reset.
 */
export function createInflightRegistry() {
  /** @type {Map<number, {key:number,question:string,convId:number|null,startedAt:number,live:boolean,show:boolean}>} */
  const turns = new Map();
  /** Monotonic per-conversation reload counter. NEVER decremented or pruned —
   *  it is a useEffect dependency, so a value that could go back down would make
   *  the loader refetch in a loop. One integer per conversation that ever had an
   *  abandoned turn, bounded by the life of the page. */
  let reloads = {};
  let seq = 0;
  const subs = new Set();

  // Cached snapshot. useSyncExternalStore calls getSnapshot on every render and
  // compares by IDENTITY: returning a freshly-built object each time is an
  // infinite render loop ("The result of getSnapshot should be cached"). So the
  // snapshot is rebuilt on mutation and only on mutation.
  let snap = { turns: [], reloads };
  let live = false;

  function emit() {
    snap = { turns: [...turns.values()], reloads };
    live = false;
    for (const t of turns.values()) if (t.live) { live = true; break; }
    for (const fn of subs) fn();
  }

  return {
    subscribe(fn) {
      subs.add(fn);
      return () => subs.delete(fn);
    },
    getSnapshot: () => snap,
    /** True while ANY stream is still open — what the unload guard keys on. A
     *  primitive, so useSyncExternalStore compares it cleanly. */
    hasLiveTurn: () => live,

    /** Register a turn at submit time. `conversationId` is null for the first
     *  turn of a brand-new chat, where the id only arrives with the server's
     *  `conversation` event (see attachConversation). Returns the turn key,
     *  which Chat.jsx also stamps onto the two messages it appends. */
    startTurn({ question, conversationId = null }) {
      const key = ++seq;
      turns.set(key, {
        key, question, convId: conversationId ?? null,
        startedAt: Date.now(), live: true, show: true,
      });
      emit();
      return key;
    },

    /** Backfill the conversation id for a brand-new chat's first turn.
     *  A no-op on an unknown key, which is what stops a turn that was already
     *  settled or hidden from being resurrected by a late event. */
    attachConversation(key, convId) {
      const t = turns.get(key);
      if (!t || t.convId != null) return;
      t.convId = convId;
      emit();
    },

    /** Stop-generating: drop the UI representation but KEEP the stream live.
     *
     *  The two flags exist for exactly this moment. The stopped note promises
     *  "the answer will be saved to this chat", so:
     *    - `show=false` — no placeholder, and settling must not yank the
     *      finished answer in under the user, who deliberately stopped watching;
     *    - `live` untouched — the unload guard stays armed, because refreshing
     *      now would break that promise by killing the turn. */
    hideTurn(key) {
      const t = turns.get(key);
      if (!t || !t.show) return;
      t.show = false;
      emit();
    },

    /** The stream finished (or threw). `rendered` means the owning view already
     *  displayed the result itself, so nothing needs reloading.
     *
     *  Dropped outright when there is nothing left to do: the owner rendered it,
     *  the user stopped it, or no conversation id ever arrived (an early
     *  transport failure — keeping it would strand a placeholder that can never
     *  be cleared). Otherwise the entry survives as a settled placeholder and
     *  bumps the conversation's reload counter, so the viewer's loader refetches
     *  exactly once and replaces the placeholder with the real answer. */
    settleTurn(key, { rendered = false } = {}) {
      const t = turns.get(key);
      // Unknown key, or ALREADY settled. The second guard matters: a settled
      // entry survives as a placeholder until the loader clears it, so a second
      // settleTurn for the same key would bump the reload counter again and
      // refetch the conversation twice.
      if (!t || !t.live) return;
      if (rendered || !t.show || t.convId == null) {
        turns.delete(key);
      } else {
        t.live = false;
        reloads = { ...reloads, [t.convId]: (reloads[t.convId] ?? 0) + 1 };
      }
      emit();
    },

    /** Refetch this conversation now, because the reader explicitly asked to.
     *
     *  The stopped note promises the answer will be saved, and settleTurn
     *  deliberately does NOT schedule a reload for a stopped turn (that is the
     *  no-yank above). Re-clicking the conversation you are already looking at
     *  is not a route change either, so nothing else in the app can produce a
     *  refetch — which left the note's "reopen it to check" pointing at the
     *  page reload that KILLS the turn it is promising to save.
     *
     *  Bumps the same monotonic counter settleTurn uses, so there is one
     *  refetch mechanism rather than a second one to keep in step. A null
     *  conversation is a brand-new chat whose id never arrived; there is
     *  nothing to fetch, so this is inert rather than an error. */
    reloadNow(convId) {
      if (convId == null) return;
      reloads = { ...reloads, [convId]: (reloads[convId] ?? 0) + 1 };
      emit();
    },

    /** Is this turn's stream still open?
     *
     *  Chat.jsx asks before it offers "Check now" on a stopped bubble. The
     *  answer only reaches the database when the drained stream finishes, so
     *  checking earlier fetches the thread as it stood BEFORE the question and
     *  replaces the stopped note with it — the reader's question vanishing is a
     *  worse outcome than the wait. Pure over the snapshot, like pendingFor, so
     *  the caller re-derives during render instead of holding a second copy. */
    isTurnLive(key, s = snap) {
      return s.turns.some((t) => t.key === key && t.live);
    },

    /** A full server load of this conversation supersedes any settled
     *  placeholder for it. Called from the loader so the placeholder disappears
     *  in the SAME commit the real rows arrive — no flicker.
     *
     *  LIVE entries survive on purpose: returning mid-flight fetches the thread
     *  as it currently stands, and the turn is still running, so its spinner
     *  must outlive that fetch. */
    clearForConversation(convId) {
      let changed = false;
      for (const [key, t] of turns) {
        if (t.convId === convId && !t.live) { turns.delete(key); changed = true; }
      }
      if (changed) emit();
    },

    /** Turns to draw in this conversation. Pure over the snapshot, so callers
     *  re-derive during render rather than holding a second copy. */
    pendingFor(convId, s = snap) {
      if (convId == null) return [];
      return s.turns.filter((t) => t.convId === convId && t.show);
    },
  };
}

export const inflight = createInflightRegistry();
