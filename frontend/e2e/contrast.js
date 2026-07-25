// Resolved-pixel contrast measurement, shared by the specs that need it.
//
// Why this exists at all, given the axe gate: axe reports an element whose text
// is very short as `incomplete` ("content is too short to determine if it is
// actual text content") rather than a violation, and `incomplete` is not
// gatable in general — it also holds the composer's deliberate transparent
// textarea overlay. So a badge or a small trust mark can sit below AA on every
// screen and the scan stays green. The other hole is coverage: axe only sees
// what the scanned page actually renders, so an element that needs specific
// mocked state to appear is invisible to it no matter how long its text is.
//
// Measuring RESOLVED pixels (not asserting a colour literal) pins the thing that
// matters — readability — and keeps passing when a token is retuned.
export async function contrastRatio(page, selector) {
  return page.evaluate((sel) => {
    const el = globalThis.document.querySelector(sel);
    if (!el) return null;
    const channels = (s) => (s.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
    const luminance = (rgb) => {
      const [r, g, b] = rgb.map((v) => {
        const c = v / 255;
        return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const opaque = (s) => s && s !== "transparent" && !/rgba\([^)]*,\s*0\s*\)$/.test(s);
    // Walk up for the nearest opaque background — the element's own is usually
    // transparent, and the effective backdrop is what the eye actually sees.
    let bg = null;
    for (let n = el; n; n = n.parentElement) {
      const c = globalThis.getComputedStyle(n).backgroundColor;
      if (opaque(c)) { bg = channels(c); break; }
    }
    if (!bg) return null;
    const fg = globalThis.getComputedStyle(el).color;
    const [hi, lo] = [luminance(channels(fg)), luminance(bg)]
      .sort((a, b) => b - a);
    return (hi + 0.05) / (lo + 0.05);
  }, selector);
}
