// Derive a 1–2 letter avatar monogram from an email address (UserMenu.jsx).
//
// PURE: initials(email) -> uppercase string. The rule mirrors how people read a
// work address: a local part that looks like "first.last" (or first_last /
// first-last) yields two initials; anything else falls back to the single first
// letter. A "+tag" suffix is a routing tag, never a surname, so it's stripped
// before splitting (todd+ipeds@… -> "T", not "TI"). Unit-tested in initials.test.js.

const SEP = /[._-]+/;

export function initials(email) {
  const raw = String(email ?? "").trim();
  if (!raw) return "?";
  // Local part only; drop a +tag suffix before it can masquerade as a name token.
  const local = raw.split("@")[0].split("+")[0];
  // Name-like tokens: begin with a letter (skips a leading number chunk like
  // "2024.cohort", so the monogram reads "C", not "2").
  const tokens = local.split(SEP).filter((t) => /^[a-z]/i.test(t));
  if (tokens.length >= 2) {
    return (tokens[0][0] + tokens[1][0]).toUpperCase();
  }
  if (tokens.length === 1) {
    return tokens[0][0].toUpperCase();
  }
  // No name-like token at all: first alphanumeric of the local part.
  const first = local.match(/[a-z0-9]/i);
  return first ? first[0].toUpperCase() : "?";
}
