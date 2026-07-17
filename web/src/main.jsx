import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App.jsx";
import ErrorBoundary from "./ErrorBoundary.jsx";
import "./styles.css";

// Apply the saved theme before first paint so there's no light/dark flash.
const savedTheme = localStorage.getItem("theme");
if (savedTheme === "light" || savedTheme === "dark") {
  document.documentElement.setAttribute("data-theme", savedTheme);
}

// ErrorBoundary stays OUTERMOST, wrapping BrowserRouter rather than sitting
// inside it: it falls back to a plain window.location.reload(), which must
// still work even if the thing that threw was the router itself.
//
// LOAD-BEARING PRECONDITION, WARNING FOR FUTURE MAINTAINERS: do NOT add
// `future={{ v7_startTransition: true }}` to this <BrowserRouter> without
// re-reading Chat.jsx's submit() `conversation` event handler first. That
// flag (the documented v6->v7 migration step, and the v7 DEFAULT) makes
// react-router defer its location updates as a React transition instead of a
// plain synchronous commit -- which breaks the auto-batching Chat.jsx's
// mid-stream URL flip relies on to keep a just-streamed answer on screen.
createRoot(document.getElementById("root")).render(
  <ErrorBoundary>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </ErrorBoundary>,
);
