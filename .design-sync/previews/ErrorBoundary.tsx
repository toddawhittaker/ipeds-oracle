import React from "react";
import { ErrorBoundary } from "ipeds-query-web";

// Two honest stories: the pass-through (no error → children render untouched)
// and the caught state. The caught one throws during render on purpose, which
// is the only way an error boundary's fallback can be shown.

function Boom(): React.ReactElement {
  throw new Error("Simulated render failure");
}

export const PassesChildrenThrough = () => (
  <ErrorBoundary>
    <p className="muted">Nothing has gone wrong, so children render untouched.</p>
  </ErrorBoundary>
);

export const CaughtError = () => (
  <ErrorBoundary>
    <Boom />
  </ErrorBoundary>
);
