import React from "react";
import { ChartModal } from "ipeds-query-web";

// The maximized view of a Chart, in a dialog. It carries the opener's current
// type/trend/labels through initial* props so maximizing never resets the view,
// and the inner <Chart inModal> hides its own maximize control.

const SPEC = {
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

export const Maximized = () => (
  <ChartModal spec={SPEC} initialType="line" initialTrend onClose={() => {}} />
);
