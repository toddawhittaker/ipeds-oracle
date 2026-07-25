// The Admin section SHELL: route-param handling (AdminRoute) and the tab chrome
// (Admin). The five pages it renders live in ./admin/* — they were already
// independent, props-only components inside one 2,467-line file, which meant
// their pure helpers could never be unit-tested and every edit to one page
// touched the same file as the other four.
//
// This file owns ADMIN_TABS and the alias/redirect rules ONLY. Sub-tab session
// memory lives in usertabs.js (next to resolveSubTab) rather than here, because
// Allowlist writes it too and importing it back from this file would be a cycle.
import React from "react";
import { NavLink, Navigate, useParams } from "react-router";
import { rememberedSubTab, resolveSubTab } from "./usertabs.js";
import { formatBadge } from "./attention.js";
import { IconInfo } from "./icons.jsx";
import Allowlist from "./admin/Allowlist.jsx";
import Imports from "./admin/Imports.jsx";
import Usage from "./admin/Usage.jsx";
import Skills from "./admin/Skills.jsx";
import Logs from "./admin/Logs.jsx";

// One source of truth for both the subtab nav and the /admin/:tab route
// validator below -- "users" is the route/label; the underlying component
// stays named Allowlist (it mirrors the /api/admin/allowlist endpoints it
// drives, which are unchanged by this rename).
export const ADMIN_TABS = ["users", "imports", "usage", "skills", "logs"];

// Former standalone user-management pages redirect INTO the Users sub-tabs, so
// old bookmarks/links keep working. These segments are deliberately NOT in
// ADMIN_TABS, and the alias map is checked first, so an alias never collides
// with a real tab.
const USERS_TAB_ALIASES = { pending: "pending", blocked: "blocked", allowlist: "current" };

// Reads the :tab (and, for Users, :sub) route params and renders Admin, or
// redirects: a legacy alias -> its Users sub-tab; an unknown tab -> Users; a
// bare/invalid Users sub -> the remembered-or-default sub, canonicalized into
// the URL so every view has a distinct, bookmarkable address; a stray :sub on a
// non-Users tab -> the bare tab. Kept separate from Admin so Admin stays a
// plain props component.
export function AdminRoute({ me, onDataChanged, attention, onAttentionChanged, version }) {
  const { tab, sub } = useParams();
  if (Object.prototype.hasOwnProperty.call(USERS_TAB_ALIASES, tab)) {
    return <Navigate to={`/admin/users/${USERS_TAB_ALIASES[tab]}`} replace />;
  }
  if (!ADMIN_TABS.includes(tab)) return <Navigate to="/admin/users/current" replace />;
  if (tab === "users") {
    const resolved = resolveSubTab(sub);
    // Bare /admin/users (sub == null) restores the remembered sub-tab; an
    // invalid sub falls back to the default. Either way, redirect so the URL
    // always names the concrete active tab.
    if (sub !== resolved) {
      return <Navigate to={`/admin/users/${sub == null ? rememberedSubTab() : resolved}`} replace />;
    }
    return <Admin me={me} tab={tab} sub={resolved} onDataChanged={onDataChanged}
                  attention={attention} onAttentionChanged={onAttentionChanged} version={version} />;
  }
  if (sub != null) return <Navigate to={`/admin/${tab}`} replace />;
  return <Admin me={me} tab={tab} onDataChanged={onDataChanged}
                attention={attention} onAttentionChanged={onAttentionChanged} version={version} />;
}

export default function Admin({ me, tab, sub, onDataChanged, attention, onAttentionChanged, version }) {
  // Attention counts default to empty so the nav renders unbadged if the Shell
  // hasn't fetched yet (or a test mounts Admin directly). refresh is a no-op
  // fallback for the same reason.
  const counts = attention || {};
  const refreshAttention = onAttentionChanged || (() => {});
  // NON-dismissible on purpose: an available update is an attention item like a
  // pending user or a log problem — it stays until you ACT on it (update the
  // deployment → update_available goes false → the banner AND the +1 avatar
  // badge clear together). So the badge always maps to this visible banner.
  return (
    <main className="admin thin-scroll">
      <h1 className="sr-only">Admin</h1>
      {version?.update_available && (
        <div className="update-banner" role="status">
          <IconInfo size={16} aria-hidden="true" />
          <span>
            <strong>v{version.latest}</strong> is available — you&rsquo;re on {version.current}.{" "}
            <a href="https://github.com/toddawhittaker/ipeds-oracle/releases"
               target="_blank" rel="noreferrer">Release notes</a>
          </span>
        </div>
      )}
      <nav className="subtabs" aria-label="Admin sections">
        {ADMIN_TABS.map((t) => {
          // Only areas with an actionable backlog carry a count (users/skills/
          // logs); imports/usage are absent from `counts` → no badge.
          const badge = formatBadge(counts[t]);
          const n = counts[t] || 0;
          return (
            // Users drops `end` so it stays active across its sub-tab paths
            // (/admin/users/current|pending|blocked, all prefixes of /admin/users);
            // the other tabs match exactly.
            <NavLink key={t} to={`/admin/${t}`} end={t !== "users"}
                     aria-label={n > 0 ? `${t[0].toUpperCase() + t.slice(1)}, ${n} awaiting attention` : undefined}
                     className={({ isActive }) => (isActive ? "on" : "")}>
              {t[0].toUpperCase() + t.slice(1)}
              {badge && <span className="tab-badge attention" aria-hidden="true">{badge}</span>}
            </NavLink>
          );
        })}
      </nav>
      {tab === "users" && <Allowlist me={me} sub={sub} onAttentionChanged={refreshAttention} />}
      {tab === "imports" && <Imports onDataChanged={onDataChanged} />}
      {tab === "usage" && <Usage />}
      {tab === "skills" && <Skills onAttentionChanged={refreshAttention} />}
      {tab === "logs" && <Logs onAttentionChanged={refreshAttention} />}
    </main>
  );
}
