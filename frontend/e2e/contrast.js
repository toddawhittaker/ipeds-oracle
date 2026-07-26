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

// Non-text contrast (WCAG 1.4.11) for a control whose STATE is carried by a
// pseudo-element — the switch thumb. Returns { fill, ring }: the ::before's
// background against the host's background, and its box-shadow ring against the
// same. BOTH are reported because either one can supply the boundary, and which
// one does flips with state: the switch's thumb fill clears 3:1 on the --accent
// track (ON) but not on the --line-strong track (OFF), where the ring carries
// it. Asserting only the fill would demand something no single colour can
// satisfy in both states — see the note in styles.css.
//
// axe cannot cover any of this: a pseudo-element has no text, so it is filed as
// `incomplete` rather than a violation, and 1.4.11 has no axe rule at all.
export async function nonTextContrast(page, selector) {
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
    const ratio = (a, b) => {
      const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
      return (hi + 0.05) / (lo + 0.05);
    };
    const before = globalThis.getComputedStyle(el, "::before");
    const track = channels(globalThis.getComputedStyle(el).backgroundColor);
    const fill = channels(before.backgroundColor);
    // A RING is a hard outline: zero offset, zero blur, positive spread
    // (`0 0 0 1px c`). A soft DROP SHADOW (`0 1px 2px c`) has offset/blur and no
    // spread — it is not a boundary and must not be counted as one. Getting this
    // wrong is not academic: reading the first colour in the list scored the
    // drop shadow as a ring, which made these assertions pass against the very
    // CSS they were written to reject.
    const parts = (before.boxShadow || "").split(/,(?![^(]*\))/);
    let ring = null;
    for (const part of parts) {
      const colour = part.match(/rgba?\([^)]*\)/);
      if (!colour) continue;
      const px = (part.replace(colour[0], "").match(/-?[\d.]+px/g) || [])
        .map((v) => parseFloat(v));
      const [dx = 0, dy = 0, blur = 0, spread = 0] = px;
      if (dx === 0 && dy === 0 && blur === 0 && spread > 0) {
        ring = channels(colour[0]);
        break;
      }
    }
    return {
      fill: ratio(fill, track),
      ring: ring ? ratio(ring, track) : null,
    };
  }, selector);
}

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
