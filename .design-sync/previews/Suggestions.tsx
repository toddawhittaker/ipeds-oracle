import React from "react";
import { Suggestions } from "ipeds-query-web";

// The optional "you might also ask" drill-down chips, rendered under an answer.
// Outline-styled on purpose — compare with Clarify, which is a REQUIRED
// decision and therefore accent-filled.

const ITEMS = [
  "Break this out by award level",
  "How does Ohio compare to the national average?",
  "Show the last ten years instead",
];

export const Chips = () => <Suggestions items={ITEMS} onAsk={() => {}} />;

export const Disabled = () => <Suggestions items={ITEMS} onAsk={() => {}} disabled />;

export const Single = () => (
  <Suggestions items={["Show the same figures for private nonprofit institutions"]} onAsk={() => {}} />
);
