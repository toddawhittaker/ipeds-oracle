import React from "react";
import { Chart } from "ipeds-query-web";

// A time-like x-axis ("year") is what makes the trend line and the ▲/▼ delta
// badge eligible — on a categorical axis both are deliberately suppressed,
// which is why the Bar story below shows neither.

const TREND = {
  x: "year",
  y: "awards",
  title: "Nursing degrees conferred, nationally",
  data: [
    { year: 2019, awards: 258_310 },
    { year: 2020, awards: 271_902 },
    { year: 2021, awards: 299_444 },
    { year: 2022, awards: 324_575 },
    { year: 2023, awards: 318_206 },
    { year: 2024, awards: 311_880 },
  ],
};

export const LineWithTrend = () => <Chart spec={TREND} initialTrend />;

export const LineNoTrend = () => <Chart spec={TREND} initialTrend={false} initialLabels />;

export const BarByState = () => (
  <Chart
    spec={{
      type: "bar",
      x: "state",
      y: "institutions",
      title: "Degree-granting institutions by state",
      data: [
        { state: "California", institutions: 379 },
        { state: "New York", institutions: 291 },
        { state: "Texas", institutions: 217 },
        { state: "Pennsylvania", institutions: 205 },
        { state: "Florida", institutions: 194 },
      ],
    }}
  />
);
