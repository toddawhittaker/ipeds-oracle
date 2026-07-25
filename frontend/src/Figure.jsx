import React from "react";
import { isFigureVerified, normalizeFigure } from "./figure.js";

// The signature "figure": a typeset hero statistic rendered above an answer when
// the model emitted one — a single headline number that directly answers the
// question. Pure presentation over the Reading-Room `.figure` device (styles.css),
// the same typographic primitive the Login "door" uses: a mono small-caps caption,
// a big serif number (with an optional small unit), an ochre baseline rule, and a
// mono source line.
//
// Returns null when there's no usable figure (normalizeFigure guards value+label),
// so the caller can render it unconditionally. It's a sibling BEFORE <Markdown> in
// the assistant bubble, so it sits above the prose and stays outside the copy
// surface (the number is already in the prose — copies lose nothing).
// `grounding` is the server's verdict on whether this number reproduces from the
// query results (messages.figure_grounding / the `done` event). It earns a quiet
// "verified" mark when it does, and NOTHING when it doesn't — see
// isFigureVerified for why the negative case is deliberately silent.
/**
 * @typedef {object} FigureProps
 * @property {{ value: string | number, unit?: string, label: string, source?: string }} spec
 *   The hero statistic. `value` and `label` are both required — a spec missing
 *   either renders nothing at all.
 * @property {"exact" | "rounded" | "derived" | "ungrounded" | "no_figure" | "malformed" | "unchecked"} [grounding]
 *   Server-side grounding verdict. ONLY "exact" | "rounded" | "derived" render the
 *   quiet "✓ verified" mark; every other value (and undefined) renders NO mark and
 *   NO warning. Positive-only by design — never add an "unverified" state.
 */

/** @param {FigureProps} props */
export default function Figure({ spec, grounding }) {
  const fig = normalizeFigure(spec);
  if (!fig) return null;
  const { value, unit, label, source } = fig;
  const verified = isFigureVerified(grounding);
  // One readable sentence for assistive tech. The mark is part of it — a
  // sighted-only trust signal would be the wrong kind of quiet.
  const alt = [label, value + (unit ? ` ${unit}` : ""), source,
               verified ? "verified against the query results" : null]
    .filter(Boolean).join(" — ");
  return (
    <figure className="answer-figure" role="img" aria-label={alt}>
      <span className="field-label">{label}</span>
      <div className="figure num">
        {value}
        {unit ? <span className="unit"> {unit}</span> : null}
      </div>
      <div className="fig-rule" aria-hidden="true" />
      {(source || verified) && (
        <figcaption className="answer-figure-src">
          {source}
          {verified && (
            // aria-hidden: the alt sentence above already says this, and the
            // figure is a role="img" whose children are presentational anyway.
            <span className="fig-verified" aria-hidden="true"
                  title="This number reproduces from the query results">
              {source ? " · " : ""}✓ verified
            </span>
          )}
        </figcaption>
      )}
    </figure>
  );
}
