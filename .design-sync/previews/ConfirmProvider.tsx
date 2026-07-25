import React from "react";
import { ConfirmProvider, useConfirm } from "ipeds-query-web";

// ConfirmProvider supplies useConfirm(), which returns confirm(options) —
// { variant, title, body, confirmLabel, onConfirm }. The modal is what this
// component owns visually, so each story opens one on mount.
//
// NOTE for anyone composing this: confirm() is NOT awaitable by the caller, and
// onConfirm must not return a long-running promise — the modal would sit
// spinning for its whole duration.

function OpenOnMount({ options }: { options: any }) {
  const confirm = useConfirm();
  React.useEffect(() => {
    confirm(options);
  }, []);
  return <p className="muted">Opened from a child via useConfirm().</p>;
}

export const Warning = () => (
  <ConfirmProvider>
    <OpenOnMount
      options={{
        variant: "warning",
        title: "Remove the later questions?",
        body: "Re-asking this question also drops the 3 questions that came after it. This cannot be undone.",
        confirmLabel: "Re-ask and drop 3",
        onConfirm: () => {},
      }}
    />
  </ConfirmProvider>
);

export const Danger = () => (
  <ConfirmProvider>
    <OpenOnMount
      options={{
        variant: "danger",
        title: 'Delete "Nursing completions by state"?',
        body: "This will permanently delete the chat and all of its messages. This action cannot be undone.",
        confirmLabel: "Delete",
        onConfirm: () => {},
      }}
    />
  </ConfirmProvider>
);
