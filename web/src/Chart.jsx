import React, { useEffect, useState } from "react";
import {
  Bar, BarChart, CartesianGrid, Legend, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

// Series palette chosen to read on both light and dark backgrounds.
const PALETTE = ["#3b82c4", "#e0803a", "#38a169", "#a23bc9", "#d64f6b", "#0e9aa7"];

// Track theme colors from CSS vars so charts follow the light/dark toggle.
function useThemeColors() {
  const read = () => {
    const cs = getComputedStyle(document.documentElement);
    const g = (n, d) => cs.getPropertyValue(n).trim() || d;
    return {
      text: g("--text", "#1a1f26"), muted: g("--muted", "#6b7280"),
      line: g("--line", "#e3e6ea"), panel: g("--panel", "#ffffff"),
    };
  };
  const [colors, setColors] = useState(read);
  useEffect(() => {
    const update = () => setColors(read());
    const obs = new MutationObserver(update);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    mq?.addEventListener?.("change", update);
    return () => { obs.disconnect(); mq?.removeEventListener?.("change", update); };
  }, []);
  return colors;
}

// Renders a chart from a compact spec the model emits:
//   { type:"line"|"bar", x:"year", y:"awards" | ["a","b"], title?, data:[…] }
export default function Chart({ spec }) {
  const c = useThemeColors();
  if (!spec || !Array.isArray(spec.data) || spec.data.length === 0 || !spec.x) return null;

  const series = (Array.isArray(spec.y) ? spec.y : [spec.y]).filter(Boolean);
  if (series.length === 0) return null;
  const isBar = spec.type === "bar";
  const ChartEl = isBar ? BarChart : LineChart;
  const axis = { stroke: c.muted, tick: { fill: c.muted, fontSize: 12 } };

  return (
    <figure className="chart">
      {spec.title && <figcaption className="chart-title">{spec.title}</figcaption>}
      <ResponsiveContainer width="100%" height={300}>
        <ChartEl data={spec.data} margin={{ top: 8, right: 16, bottom: 4, left: 4 }}>
          <CartesianGrid stroke={c.line} strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey={spec.x} {...axis} />
          <YAxis width={60} {...axis} />
          <Tooltip
            contentStyle={{ background: c.panel, border: `1px solid ${c.line}`,
              borderRadius: 8, color: c.text }}
            labelStyle={{ color: c.text }} />
          {series.length > 1 && <Legend wrapperStyle={{ fontSize: 12 }} />}
          {series.map((key, i) => (isBar
            ? <Bar key={key} dataKey={key} fill={PALETTE[i % PALETTE.length]} radius={[3, 3, 0, 0]} />
            : <Line key={key} type="monotone" dataKey={key} stroke={PALETTE[i % PALETTE.length]}
                    strokeWidth={2} dot={{ r: 3 }} />
          ))}
        </ChartEl>
      </ResponsiveContainer>
    </figure>
  );
}
