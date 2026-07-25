import React from "react";
import { ToastProvider, useToast } from "ipeds-query-web";

// ToastProvider renders nothing on its own — it supplies the useToast() context
// and hosts the toast outlet. useToast() returns push(message, kind?) where
// kind is "" | "ok" | "error". A story therefore has to PUSH on mount for the
// visual this component owns to appear in a static card.

function PushOnMount({ message, kind = "" }: { message: string; kind?: string }) {
  const push = useToast();
  React.useEffect(() => {
    push(message, kind);
  }, []);
  return <p className="muted">Pushed from a child via useToast().</p>;
}

export const Confirmation = () => (
  <ToastProvider>
    <PushOnMount message="3 users approved" kind="ok" />
  </ToastProvider>
);

export const Failure = () => (
  <ToastProvider>
    <PushOnMount message="Could not reach the server — try again" kind="error" />
  </ToastProvider>
);

export const Neutral = () => (
  <ToastProvider>
    <PushOnMount message="Rebuilding the dataset…" />
  </ToastProvider>
);
