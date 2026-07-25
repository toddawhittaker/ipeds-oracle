import React from "react";
import { UserMenu } from "ipeds-query-web";

// The top bar holds exactly two things: the Wordmark and this. The avatar's
// initials come from the email; the corner pill is the capped attention count
// (accent-toned — a queue is work waiting, never a red failure).
//
// The menu's OPEN state is interaction-driven and cannot render statically, so
// these stories show the trigger in its meaningful variants.

const base = {
  theme: "light" as const,
  onToggleTheme: () => {},
  onSignOut: () => {},
  onAbout: () => {},
};

export const SignedIn = () => <UserMenu {...base} email="todd.whittaker@franklin.edu" />;

export const AdminWithAttention = () => (
  <UserMenu {...base} email="todd.whittaker@franklin.edu" isAdmin attentionTotal={7} />
);

export const BadgeCapped = () => (
  <UserMenu {...base} email="dana.reyes+reports@franklin.edu" isAdmin attentionTotal={128} />
);

// NO dark-theme story: the tokens live on :root[data-theme="dark"], so a
// nested wrapper renders the LIGHT palette and the card would misrepresent the
// theme. `theme` here only picks which toggle icon the menu shows.
export const ThemeToggleShowsMoon = () => (
  <UserMenu {...base} theme="dark" email="alex.kim@franklin.edu" />
);
