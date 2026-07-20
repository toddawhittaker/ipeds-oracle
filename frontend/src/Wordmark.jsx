// The "IPEDS Oracle" wordmark — an inline SVG + type lockup rendered straight
// from the app's theme tokens, so light and dark come from ONE source (no more
// wordmark.png / wordmark-dark.png pair). The Column mark (--accent shaft between
// --ochre capital & base) sits beside mono "IPEDS" (the data source / the
// machine), an ochre hairline, and serif "Oracle" (the answer) — the app's own
// figure device. Scale is driven by the container's font-size (see .brand
// .wordmark / .login .wordmark in styles.css).
//
// role="img" + aria-label gives the whole lockup one accessible name; the inner
// text and mark are aria-hidden so a screen reader hears "IPEDS Oracle" once,
// not "IPEDS" then "Oracle" as separate strings.
// `showIcon` (default true) draws the Column mark; pass false for the text-only
// type lockup (mono "IPEDS" · ochre rule · serif "Oracle") — e.g. inline in the
// About dialog title, where the head already carries its own icon.
export default function Wordmark({ showIcon = true }) {
  return (
    <span className="wordmark" role="img" aria-label="IPEDS Oracle">
      {showIcon && (
        <svg className="wm-icon" viewBox="0 0 64 64" aria-hidden="true" focusable="false">
          <rect className="wm-cap" x="14" y="11" width="36" height="5.4" rx="1.6" />
          <g className="wm-shaft">
            <rect x="19.5" y="19" width="4.6" height="26" rx="1.6" />
            <rect x="25.7" y="19" width="4.6" height="26" rx="1.6" />
            <rect x="31.9" y="19" width="4.6" height="26" rx="1.6" />
            <rect x="38.1" y="19" width="4.6" height="26" rx="1.6" />
          </g>
          <rect className="wm-cap" x="14" y="47.6" width="36" height="5.4" rx="1.6" />
        </svg>
      )}
      <span className="wm-ipeds" aria-hidden="true">IPEDS</span>
      <span className="wm-div" aria-hidden="true" />
      <span className="wm-oracle" aria-hidden="true">Oracle</span>
    </span>
  );
}
