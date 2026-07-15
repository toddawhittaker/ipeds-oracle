import React from "react";

// App-wide error boundary: if any descendant throws during render, catch it and
// show a recoverable fallback instead of React unmounting the whole tree to a
// blank white screen. Error boundaries must be class components.
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Surface the crash for debugging (and any future telemetry hook).
    console.error("Unhandled UI error:", error, info?.componentStack);
  }

  handleReload = () => {
    // A full reload is the safest reset from an unknown broken state.
    window.location.reload();
  };

  render() {
    if (this.state.error) {
      return (
        <div className="center">
          <div className="card errbound" role="alert">
            <h1>Something went wrong</h1>
            <p className="muted">
              The page hit an unexpected error. Reloading usually fixes it. If it
              keeps happening, let an administrator know.
            </p>
            <button type="button" onClick={this.handleReload}>
              Reload the page
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
