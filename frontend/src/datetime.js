// Pure date/time helpers for user-facing timestamps + turn duration. All display
// uses the VIEWER's own browser timezone via Intl — never a server/deployment tz
// — so each user sees times in their own local zone (with the short zone name).

// A unix-SECONDS timestamp (messages.created_at) → a short local time with its
// zone, e.g. "2:47 PM EST". "" when the value isn't a usable timestamp.
export function formatStamp(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "";
  try {
    return new Intl.DateTimeFormat(undefined, {
      hour: "numeric", minute: "2-digit", timeZoneName: "short",
    }).format(new Date(n * 1000));
  } catch {
    return "";
  }
}

// The viewer's current short zone name, e.g. "EST"/"EDT" — for a chart axis label.
export function shortZone() {
  try {
    const parts = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" })
      .formatToParts(new Date());
    return parts.find((p) => p.type === "timeZoneName")?.value || "";
  } catch {
    return "";
  }
}

// Turn duration in MILLISECONDS → "Thought for N seconds". null when there is
// nothing meaningful to show (missing/negative), so the caller renders nothing.
export function thoughtLabel(ms) {
  if (ms == null) return null;  // no duration recorded (cache hit / refusal) → show nothing
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return null;
  if (n < 1000) return "Thought for less than a second";
  const secs = Math.round(n / 1000);
  return `Thought for ${secs} second${secs === 1 ? "" : "s"}`;
}
