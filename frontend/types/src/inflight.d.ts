/**
 * Build an independent registry. The app uses the `inflight` singleton below;
 * tests construct their own so no case can leak state into another — cheaper and
 * clearer than exporting a test-only reset.
 */
export declare function createInflightRegistry(): {
    subscribe(fn: any): () => boolean;
    getSnapshot: () => {
        turns: any[];
        reloads: {};
    };
    /** True while ANY stream is still open — what the unload guard keys on. A
     *  primitive, so useSyncExternalStore compares it cleanly. */
    hasLiveTurn: () => boolean;
    /** Register a turn at submit time. `conversationId` is null for the first
     *  turn of a brand-new chat, where the id only arrives with the server's
     *  `conversation` event (see attachConversation). Returns the turn key,
     *  which Chat.jsx also stamps onto the two messages it appends. */
    startTurn({ question, conversationId }: {
        conversationId?: any;
        question: any;
    }): number;
    /** Backfill the conversation id for a brand-new chat's first turn.
     *  A no-op on an unknown key, which is what stops a turn that was already
     *  settled or hidden from being resurrected by a late event. */
    attachConversation(key: any, convId: any): void;
    /** Stop-generating: drop the UI representation but KEEP the stream live.
     *
     *  The two flags exist for exactly this moment. The stopped note promises
     *  "the answer will be saved to this chat", so:
     *    - `show=false` — no placeholder, and settling must not yank the
     *      finished answer in under the user, who deliberately stopped watching;
     *    - `live` untouched — the unload guard stays armed, because refreshing
     *      now would break that promise by killing the turn. */
    hideTurn(key: any): void;
    /** The stream finished (or threw). `rendered` means the owning view already
     *  displayed the result itself, so nothing needs reloading.
     *
     *  Dropped outright when there is nothing left to do: the owner rendered it,
     *  the user stopped it, or no conversation id ever arrived (an early
     *  transport failure — keeping it would strand a placeholder that can never
     *  be cleared). Otherwise the entry survives as a settled placeholder and
     *  bumps the conversation's reload counter, so the viewer's loader refetches
     *  exactly once and replaces the placeholder with the real answer. */
    settleTurn(key: any, { rendered }?: {
        rendered?: boolean;
    }): void;
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
    reloadNow(convId: any): void;
    /** Is this turn's stream still open?
     *
     *  Chat.jsx asks before it offers "Check now" on a stopped bubble. The
     *  answer only reaches the database when the drained stream finishes, so
     *  checking earlier fetches the thread as it stood BEFORE the question and
     *  replaces the stopped note with it — the reader's question vanishing is a
     *  worse outcome than the wait. Pure over the snapshot, like pendingFor, so
     *  the caller re-derives during render instead of holding a second copy. */
    isTurnLive(key: any, s?: {
        turns: any[];
        reloads: {};
    }): boolean;
    /** A full server load of this conversation supersedes any settled
     *  placeholder for it. Called from the loader so the placeholder disappears
     *  in the SAME commit the real rows arrive — no flicker.
     *
     *  LIVE entries survive on purpose: returning mid-flight fetches the thread
     *  as it currently stands, and the turn is still running, so its spinner
     *  must outlive that fetch. */
    clearForConversation(convId: any): void;
    /** Turns to draw in this conversation. Pure over the snapshot, so callers
     *  re-derive during render rather than holding a second copy. */
    pendingFor(convId: any, s?: {
        turns: any[];
        reloads: {};
    }): any[];
};
export declare const inflight: {
    subscribe(fn: any): () => boolean;
    getSnapshot: () => {
        turns: any[];
        reloads: {};
    };
    /** True while ANY stream is still open — what the unload guard keys on. A
     *  primitive, so useSyncExternalStore compares it cleanly. */
    hasLiveTurn: () => boolean;
    /** Register a turn at submit time. `conversationId` is null for the first
     *  turn of a brand-new chat, where the id only arrives with the server's
     *  `conversation` event (see attachConversation). Returns the turn key,
     *  which Chat.jsx also stamps onto the two messages it appends. */
    startTurn({ question, conversationId }: {
        conversationId?: any;
        question: any;
    }): number;
    /** Backfill the conversation id for a brand-new chat's first turn.
     *  A no-op on an unknown key, which is what stops a turn that was already
     *  settled or hidden from being resurrected by a late event. */
    attachConversation(key: any, convId: any): void;
    /** Stop-generating: drop the UI representation but KEEP the stream live.
     *
     *  The two flags exist for exactly this moment. The stopped note promises
     *  "the answer will be saved to this chat", so:
     *    - `show=false` — no placeholder, and settling must not yank the
     *      finished answer in under the user, who deliberately stopped watching;
     *    - `live` untouched — the unload guard stays armed, because refreshing
     *      now would break that promise by killing the turn. */
    hideTurn(key: any): void;
    /** The stream finished (or threw). `rendered` means the owning view already
     *  displayed the result itself, so nothing needs reloading.
     *
     *  Dropped outright when there is nothing left to do: the owner rendered it,
     *  the user stopped it, or no conversation id ever arrived (an early
     *  transport failure — keeping it would strand a placeholder that can never
     *  be cleared). Otherwise the entry survives as a settled placeholder and
     *  bumps the conversation's reload counter, so the viewer's loader refetches
     *  exactly once and replaces the placeholder with the real answer. */
    settleTurn(key: any, { rendered }?: {
        rendered?: boolean;
    }): void;
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
    reloadNow(convId: any): void;
    /** Is this turn's stream still open?
     *
     *  Chat.jsx asks before it offers "Check now" on a stopped bubble. The
     *  answer only reaches the database when the drained stream finishes, so
     *  checking earlier fetches the thread as it stood BEFORE the question and
     *  replaces the stopped note with it — the reader's question vanishing is a
     *  worse outcome than the wait. Pure over the snapshot, like pendingFor, so
     *  the caller re-derives during render instead of holding a second copy. */
    isTurnLive(key: any, s?: {
        turns: any[];
        reloads: {};
    }): boolean;
    /** A full server load of this conversation supersedes any settled
     *  placeholder for it. Called from the loader so the placeholder disappears
     *  in the SAME commit the real rows arrive — no flicker.
     *
     *  LIVE entries survive on purpose: returning mid-flight fetches the thread
     *  as it currently stands, and the turn is still running, so its spinner
     *  must outlive that fetch. */
    clearForConversation(convId: any): void;
    /** Turns to draw in this conversation. Pure over the snapshot, so callers
     *  re-derive during render rather than holding a second copy. */
    pendingFor(convId: any, s?: {
        turns: any[];
        reloads: {};
    }): any[];
};
