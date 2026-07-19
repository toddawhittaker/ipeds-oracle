import React from "react";
import { normalizeFigure } from "./figure.js";

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
export default function Figure({ spec }) {
  const fig = normalizeFigure(spec);
  if (!fig) return null;
  const { value, unit, label, source } = fig;
  // One readable sentence for assistive tech.
  const alt = [label, value + (unit ? ` ${unit}` : ""), source].filter(Boolean).join(" — ");
  return (
    <figure className="answer-figure" role="img" aria-label={alt}>
      <span className="field-label">{label}</span>
      <div className="figure num">
        {value}
        {unit ? <span className="unit"> {unit}</span> : null}
      </div>
      <div className="fig-rule" aria-hidden="true" />
      {source ? <figcaption className="answer-figure-src">{source}</figcaption> : null}
    </figure>
  );
}
