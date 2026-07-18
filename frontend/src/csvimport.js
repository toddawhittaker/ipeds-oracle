// Pure CSV parse -> normalize -> validate -> plan pipeline for the admin Users
// tab's bulk import.
//
// Only the DATA LOGIC lives here; the browser behaviour around it (the drag-and-
// drop zone, the file <input>, reading file.text(), the summary/confirm flow, the
// error-report table, aria-live) stays in Admin.jsx and is covered by
// frontend/e2e/csv-import.spec.js. The exact input->output behaviour below is
// pinned by frontend/src/csvimport.test.js (vitest) — no browser needed.
//
// The end product is buildImportPlan(text, existingEmails, { today }) -> a summary
// object the UI renders as counts + an error report, and whose `ready` list the UI
// POSTs to /api/admin/allowlist/bulk. The backend re-validates authoritatively
// (EmailStr, an existing-row check) — this layer is the fast, offline preview.

// RFC-4180-ish parser -> array of records, each an array of string cells. Handles
// double-quoted fields (embedded commas, newlines, and "" escapes), CRLF and LF
// line endings, and a final record with no trailing newline. Blank PHYSICAL lines
// survive as a one-empty-cell record so buildImportPlan can keep row numbers
// aligned with the file (it decides what counts as blank).
export function parseCsv(text) {
  const s = String(text ?? "");
  const records = [];
  let record = [];
  let field = "";
  let inQuotes = false;
  const pushField = () => { record.push(field); field = ""; };
  const pushRecord = () => { pushField(); records.push(record); record = []; };
  let i = 0;
  while (i < s.length) {
    const c = s[i];
    if (inQuotes) {
      if (c === '"') {
        if (s[i + 1] === '"') { field += '"'; i += 2; continue; } // "" -> literal "
        inQuotes = false; i += 1; continue;
      }
      field += c; i += 1; continue;
    }
    if (c === '"') { inQuotes = true; i += 1; continue; }
    if (c === ",") { pushField(); i += 1; continue; }
    if (c === "\r") { if (s[i + 1] === "\n") i += 1; pushRecord(); i += 1; continue; }
    if (c === "\n") { pushRecord(); i += 1; continue; }
    field += c; i += 1;
  }
  // Flush a dangling final record (file didn't end with a newline). A file that
  // DID end with a newline leaves field="" and record=[] here -> no phantom row.
  if (field !== "" || record.length > 0) pushRecord();
  return records;
}

// Canonicalize a header cell: lowercase and drop every non-alphanumeric character
// so common punctuation/separator/spacing variants collapse to one key. Returns
// "email" | "note" | "admin", or null for any unknown column (which is ignored).
// e.g. "Email", "E-mail", "e_mail", "E Mail" all -> "email".
export function normalizeHeader(raw) {
  const key = String(raw ?? "").toLowerCase().replace(/[^a-z0-9]/g, "");
  return key === "email" || key === "note" || key === "admin" ? key : null;
}

// Map a header row to the column index of each supported field. First match wins;
// unknown columns are ignored. Absent fields are null.
export function mapColumns(headerRow) {
  const cols = { email: null, note: null, admin: null };
  (headerRow || []).forEach((cell, idx) => {
    const key = normalizeHeader(cell);
    if (key && cols[key] === null) cols[key] = idx;
  });
  return cols;
}

// The admin truthy set. Trim + lowercase, then ONLY these are true:
// yes, y, t, true, 1, x. Everything else (no/n/f/false/0/blank/unknown) is false.
// NOTE: deliberately NOT the backend's config.is_truthy set ({true,t,yes,y,1}) —
// this one also accepts "x" (a common spreadsheet checkbox marker).
const ADMIN_TRUE = new Set(["yes", "y", "t", "true", "1", "x"]);
export function parseAdminFlag(value) {
  return ADMIN_TRUE.has(String(value ?? "").trim().toLowerCase());
}

// Client-side email pre-check (the backend EmailStr is the authoritative gate on
// create). Trimmed, non-empty, one @, a dot in the domain, no whitespace.
export function isValidEmail(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(value ?? "").trim());
}

// Resolve a row's note: the trimmed cell if non-blank, else the "Imported on
// {date}" default. `today` is the caller's already-formatted local date string
// (new Date().toLocaleDateString()), passed in so this stays pure.
export function resolveNote(rawNote, today) {
  return String(rawNote ?? "").trim() || `Imported on ${today}`;
}

const cell = (record, idx) => (idx == null ? "" : (record[idx] ?? ""));
const isBlankRecord = (record) => record.every((c) => String(c ?? "").trim() === "");

// Build the import plan the UI renders and (on confirm) submits.
//   text          — raw CSV file contents
//   existingEmails — iterable of emails already on the allowlist (any case)
//   today          — pre-formatted local date for the default-note ("7/17/2026")
// Returns:
//   headerError        — string when the file is unusable (empty / no email column),
//                        else null. When set, the other lists are empty.
//   totalRows          — non-blank DATA rows detected (excludes the header + blanks)
//   ready[]            — { row, email, note, is_admin } to create
//   existingOrDuplicate[] — { row, email, reason: "already a user" | "duplicate in file" }
//   invalid[]          — { row, email, reason: "missing email" | "invalid email" }
//   adminCount         — how many of `ready` get admin
// `row` is the 1-based position of the record in the file (the header is row 1),
// so the error report points the admin at the real line.
export function buildImportPlan(text, existingEmails, { today } = {}) {
  const empty = { headerError: null, totalRows: 0, ready: [], existingOrDuplicate: [], invalid: [], adminCount: 0 };
  const records = parseCsv(text);
  if (records.length === 0) {
    return { ...empty, headerError: "The file is empty." };
  }
  const cols = mapColumns(records[0]);
  if (cols.email === null) {
    return { ...empty, headerError: 'No "email" column found. The first row must be a header with an email column.' };
  }

  const existing = new Set(Array.from(existingEmails || [], (e) => String(e).trim().toLowerCase()));
  const seen = new Set();
  const ready = [];
  const existingOrDuplicate = [];
  const invalid = [];
  let totalRows = 0;

  for (let r = 1; r < records.length; r += 1) {
    const record = records[r];
    if (isBlankRecord(record)) continue; // blank rows are ignored, not counted
    totalRows += 1;
    const row = r + 1; // header is row 1

    const rawEmail = String(cell(record, cols.email)).trim();
    if (!rawEmail) { invalid.push({ row, email: "", reason: "missing email" }); continue; }
    if (!isValidEmail(rawEmail)) { invalid.push({ row, email: rawEmail, reason: "invalid email" }); continue; }

    const email = rawEmail.toLowerCase();
    if (seen.has(email)) { existingOrDuplicate.push({ row, email, reason: "duplicate in file" }); continue; }
    seen.add(email);
    if (existing.has(email)) { existingOrDuplicate.push({ row, email, reason: "already a user" }); continue; }

    ready.push({
      row,
      email,
      note: resolveNote(cell(record, cols.note), today),
      is_admin: cols.admin === null ? false : parseAdminFlag(cell(record, cols.admin)),
    });
  }

  return { headerError: null, totalRows, ready, existingOrDuplicate, invalid, adminCount: ready.filter((u) => u.is_admin).length };
}
