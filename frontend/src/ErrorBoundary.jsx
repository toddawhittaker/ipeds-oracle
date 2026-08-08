import React from "react";

import { boundaryFallback, CRASH_TITLE } from "./errorcopy.js";
import { inflight } from "./inflight.js";

// App-wide error boundary: if any descendant throws during render, catch it and
// show a recoverable fallback instead of React unmounting the whole tree to a
// blank white screen. Error boundaries must be class components.
/**
 * @typedef {object} ErrorBoundaryProps
 * @property {React.ReactNode} children Subtree to guard. Any render error below
 *   this swaps the whole subtree for the reload card.
 * @property {string} [resetKey] Changing this CLEARS a caught error, so
 *   navigating away from a broken route recovers. Deliberately a prop compared
 *   in componentDidUpdate rather than a React `key`: a key would REMOUNT the
 *   subtree on every change, and an admin sub-tab switch is a URL change — that
 *   would destroy the three DataTables' search/sort/page/selection state, which
 *   surviving a tab switch is an explicit contract of that screen. Resetting
 *   state re-renders; it does not remount.
 */

/** @extends {React.Component<ErrorBoundaryProps, { error: Error | null }>} */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidUpdate(prevProps) {
    // Clear on navigation only — see the resetKey prop docs for why this is not
    // a `key`. Guarded on there BEING an error so an ordinary route change never
    // triggers a needless setState.
    if (this.state.error && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  componentDidCatch(error, info) {
    // Surface the crash for debugging (and any future telemetry hook).
    console.error("Unhandled UI error:", error, info?.componentStack);
  }

  handleReload = () => {
    // A full reload is the safest reset from an unknown broken state — UNLESS a
    // turn is still in flight, which is why it is no longer offered
    // unconditionally (see errorcopy.js).
    window.location.reload();
  };

  handleDismiss = () => {
    // "Wait" clears the caught error and re-renders the subtree. If the cause
    // was transient the app comes back; if it throws again the card returns,
    // which is no worse than staying. It does NOT reload, so a live turn keeps
    // draining and can still persist its answer.
    this.setState({ error: null });
  };

  render() {
    if (this.state.error) {
      // `inflight` is a module-level store, readable outside React — which is
      // exactly what lets a class fallback consult it. Reading it at render time
      // (not subscribing) is deliberate: the card is a dead end, so it needs the
      // state as of the crash, not a live feed.
      const fb = boundaryFallback({ liveTurn: inflight.hasLiveTurn() });
      return (
        <div className="center">
          <div className="card errbound" role="alert">
            <h1>{CRASH_TITLE}</h1>
            <p className="muted">{fb.body}</p>
            <button type="button"
                    onClick={fb.primary.reload ? this.handleReload : this.handleDismiss}>
              {fb.primary.label}
            </button>
            {fb.secondary && (
              <button type="button" className="link"
                      onClick={fb.secondary.reload ? this.handleReload : this.handleDismiss}>
                {fb.secondary.label}
              </button>
            )}
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
