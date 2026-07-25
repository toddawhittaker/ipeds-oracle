import React from "react";
import { BulkBar, IconShieldPlus, IconShieldMinus, IconTrash, IconCheck, IconClose } from "ipeds-query-web";

// Contextual by design: it renders NOTHING at count 0, so there is no
// "empty" story to show. Labels use stable verbs — the count lives in the
// confirm dialog, not the button — and any danger action is split off behind a
// divider.

const nouns = { one: "user", many: "users" };
const noop = () => {};

export const PageSelection = () => (
  <BulkBar
    nouns={nouns}
    mode="page"
    count={3}
    totalEligible={128}
    pageSelectedCount={3}
    pageEligibleCount={25}
    onSelectAllMatching={noop}
    onClear={noop}
    actions={[
      { key: "promote", label: "Make admin", icon: IconShieldPlus, onClick: noop },
      { key: "demote", label: "Remove admin", icon: IconShieldMinus, onClick: noop },
      { key: "delete", label: "Remove", icon: IconTrash, onClick: noop, variant: "danger" as const },
    ]}
  />
);

export const WholePageSelected = () => (
  <BulkBar
    nouns={nouns}
    mode="page"
    count={25}
    totalEligible={128}
    pageSelectedCount={25}
    pageEligibleCount={25}
    onSelectAllMatching={noop}
    onClear={noop}
    actions={[
      { key: "approve", label: "Approve", icon: IconCheck, onClick: noop },
      { key: "reject", label: "Reject", icon: IconClose, onClick: noop, variant: "danger" as const },
    ]}
  />
);

export const AllMatching = () => (
  <BulkBar
    nouns={nouns}
    mode="all"
    count={128}
    totalEligible={128}
    pageSelectedCount={25}
    pageEligibleCount={25}
    onSelectAllMatching={noop}
    onClear={noop}
    actions={[
      { key: "approve", label: "Approve", icon: IconCheck, onClick: noop },
      { key: "reject", label: "Reject", icon: IconClose, onClick: noop, variant: "danger" as const },
    ]}
  />
);
