// Copy plain text to the clipboard.
//
// Lifted out of Chat.jsx so the API-keys reveal dialog (Keys.jsx,
// admin/Keys.jsx) can use the same one rather than importing from a chat
// component or growing a fourth hand-rolled fallback. Behaviour is unchanged
// from the Chat original.
//
// The execCommand fallback is not legacy cruft: `navigator.clipboard` is
// undefined in a non-secure context, which is the documented self-host case
// (plain http on a LAN IP), and the copy button has to keep working there.
// Returns true only when the text really reached the clipboard, so callers can
// tell the user when it didn't.
export async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* fall through */ }
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  // Stays false if execCommand throws (the catch must not re-assign it —
  // ESLint 10's no-useless-assignment flags a write nothing ever reads).
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { /* keep false */ }
  document.body.removeChild(ta);
  return ok;
}
