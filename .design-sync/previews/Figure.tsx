import React from "react";
import { Figure } from "ipeds-query-web";

// The signature hero statistic: mono caption · big serif number · ochre rule ·
// mono source. Content is real IPEDS Completions data so the cards read as the
// product, not as lorem.

export const Verified = () => (
  <Figure
    spec={{
      value: "324,575",
      label: "Peak national nursing degrees",
      source: "IPEDS Completions · 2022",
    }}
    grounding="exact"
  />
);

export const NotVerified = () => (
  <Figure
    spec={{
      value: "1,012,486",
      label: "Associate's degrees conferred",
      source: "IPEDS Completions · 2024-25",
    }}
    grounding="ungrounded"
  />
);

export const WithUnit = () => (
  <Figure
    spec={{
      value: "+25.0",
      unit: "%",
      label: "Change in computer science bachelor's, 2019-25",
      source: "IPEDS Completions",
    }}
    grounding="derived"
  />
);

export const NoSource = () => (
  <Figure spec={{ value: "3,982", label: "Degree-granting institutions" }} />
);
