// Pure display formatters shared by the Admin pages.
//
// These were trapped inside Admin.jsx — a browser-tested component file with no
// co-located unit test — so real input→output logic (magnitude thresholds, the
// +tag canonicalization that decides which rows a Blocked-users group collapses)
// had no fast-tier coverage at all and was only ever exercised incidentally
// through Playwright. Splitting Admin.jsx into src/admin/* is what made them
// reachable; that is the point of extracting them, not tidiness.
//
// Dates deliberately render in the VIEWER's locale (toLocale*), never a
// hardcoded format — the app-wide rule, same as usage series bucketing and the
// chat turn stamps.

// Byte counts for the import/disk estimates. Whole bytes stay whole ("512 B");
// anything scaled carries one decimal so a 1.4 GB dataset doesn't read as "1 GB".
export function humanBytes(n) {
  if (n == null || !isFinite(n)) return "?";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Math.abs(n);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

// Elapsed/estimated durations on an import job. Under a minute reads in seconds;
// past that, minutes with the seconds remainder dropped when it is zero.
export function humanSeconds(s) {
  if (s == null || !isFinite(s)) return "?";
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const rs = Math.round(s % 60);
  return rs ? `${m}m ${rs}s` : `${m}m`;
}

// The canonical form a denial is matched on: lowercased, +tag stripped, DOTS
// LEFT ALONE. Dots are load-bearing — they can distinguish two real people at
// many providers — so stripping them would over-block. Mirrors the backend's
// access_requests.canon_email so the Blocked-users table groups exactly the rows
// a single block actually covers.
export function canonEmailForDisplay(email) {
  const trimmed = email.trim().toLowerCase();
  const at = trimmed.indexOf("@");
  if (at === -1) return trimmed;
  return trimmed.slice(0, at).split("+")[0] + trimmed.slice(at);
}

// App-standard date+time; null/absent → an em dash. Unix SECONDS in (not ms —
// the backend stores seconds, and passing ms lands you in the year 55000).
export const fmtDateTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "—");

// Local-date string for the audit note stored on a user added/allowlisted
// ("approved|added on <date> by <admin>").
export const fmtApprovalDate = (d = new Date()) => d.toLocaleDateString();

// Date only, no time — for a column that cannot afford fmtDateTime's 210px and
// whose question is "which day", not "which minute" (Admin -> Keys carries two
// such columns side by side, and the full stamp stays in each cell's title).
// Same unix-SECONDS input and same em dash on null as fmtDateTime.
export const fmtDay = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString() : "—");

// Spend. Sub-dollar amounts need 4 places or a per-query cost rounds to $0.00
// and the whole column reads as free.
export const money = (v) => "$" + Number(v || 0).toFixed(Number(v) >= 1 ? 2 : 4);

// A lesson's display name in the Skills table. Ordered fallback: the generalized
// headline, then the longer lesson body, then the admin note, then the
// originating question — a seeded/older row may have only the later fields.
export function ruleName(s) {
  return s.headline || s.lesson || s.notes || s.question || "untitled lesson";
}
