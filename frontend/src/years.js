// Collection-year labels for the loaded dataset.
//
// IPEDS `year` is the ENDING year of a collection cycle: the 2019-20 collection
// is stored as 2020. So every user-facing label has to reach one year BACK, and
// getting that off by one silently mislabels the whole dataset by a year — which
// is exactly what a hardcoded range in the chat empty state risked, since each
// deployment loads its own years via Admin → Imports and `_years` is the only
// authority. The server (GET /api/auth/me) reports the bounds; the wording lives
// here.

// Number(null) is 0 and Number("") is 0 — both FINITE — so a plain
// Number.isFinite guard lets a missing bound through and renders it as year
// zero ("-1-00 through 2024-25"). Reject the empties before converting.
function asYear(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// 2020 → "2019-20". The second half is the ending year's last two digits, so a
// century rollover still reads correctly (2100 → "2099-00").
export function collectionYearLabel(year) {
  const end = asYear(year);
  if (end === null) return "";
  return `${end - 1}-${String(end % 100).padStart(2, "0")}`;
}

// The phrase naming the loaded range, or "" when nothing is loaded (callers
// render the no-data state instead, so there is no sentence to write).
//
// A single loaded year gets "collection year 2024-25", never "2024-25 through
// 2024-25" — the degenerate range reads like a bug to anyone who sees it, and a
// fresh deployment with one imported year is the common case, not an edge one.
export function collectionYearRange(years) {
  if (!years) return "";
  const min = asYear(years.min);
  const max = asYear(years.max);
  if (min === null || max === null) return "";
  if (min === max) return `collection year ${collectionYearLabel(max)}`;
  return `collection years ${collectionYearLabel(min)} through ${collectionYearLabel(max)}`;
}
