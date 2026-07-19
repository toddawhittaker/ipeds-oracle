import React, { useEffect, useRef, useState } from "react";
import {
  Bar, BarChart, CartesianGrid, Customized, LabelList, Legend, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { svgToPngDataUrl } from "./chartimg.js";
import { IconCopy } from "./icons.jsx";
import { pctChange, trendValues } from "./trendstats.js";

const PALETTE = ["#3b82c4", "#e0803a", "#38a169", "#a23bc9", "#d64f6b", "#0e9aa7"];
// Exports are always rendered light so a pasted chart looks right in a document
// regardless of the app's current theme.
const LIGHT = { text: "#1a1f26", muted: "#6b7280", line: "#e3e6ea", ochre: "#a66a12" };
const EXPORT_W = 620, EXPORT_H = 320;

const fmtNum = (v) => (typeof v === "number" ? v.toLocaleString() : v);

function useThemeColors() {
  const read = () => {
    const cs = getComputedStyle(document.documentElement);
    const g = (n, d) => cs.getPropertyValue(n).trim() || d;
    return {
      text: g("--text", "#1a1f26"), muted: g("--muted", "#6b7280"),
      line: g("--line", "#e3e6ea"), panel: g("--panel", "#ffffff"),
      ochre: g("--ochre", "#a66a12"),
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

// A Recharts <Customized> renderer draws the title INSIDE the SVG (so it travels
// with the rasterized image when a single chart is copied).
function titleRenderer(title, fill) {
  // x="50%" is relative to the chart SVG, so the title is reliably centered
  // (the <Customized> width prop was unreliable and pushed it to the left).
  return function ChartTitle() {
    return (
      <text x="50%" y={16} textAnchor="middle" fontSize={13} fontWeight="600" fill={fill}>
        {title}
      </text>
    );
  };
}

// Build the chart's children once so the on-screen and export charts stay in
// sync. `forExport` drops interactive-only bits (tooltip/legend).
function chartChildren({ colors, isBar, keys, spec, showLabels, forExport, trendKey }) {
  const xLabel = spec.xLabel || spec.x;
  const yLabel = spec.yLabel || (keys.length === 1 ? keys[0] : "");
  const tick = { fill: colors.muted, fontSize: 12 };
  const seriesEl = keys.map((key, i) => {
    const color = PALETTE[i % PALETTE.length];
    const labels = showLabels
      ? <LabelList dataKey={key} position="top" fontSize={11} fill={colors.text} formatter={fmtNum} />
      : null;
    return isBar
      ? <Bar key={key} dataKey={key} fill={color} radius={[3, 3, 0, 0]} isAnimationActive={false}>{labels}</Bar>
      : <Line key={key} type="monotone" dataKey={key} stroke={color} strokeWidth={2}
              dot={{ r: 3 }} isAnimationActive={false}>{labels}</Line>;
  });
  return [
    <CartesianGrid key="grid" stroke={colors.line} strokeDasharray="3 3" vertical={false} />,
    <XAxis key="x" dataKey={spec.x} stroke={colors.muted} tick={tick}
           label={{ value: xLabel, position: "insideBottom", offset: -6,
                    fill: colors.muted, fontSize: 12 }} />,
    <YAxis key="y" width={70} stroke={colors.muted} tick={tick} tickFormatter={fmtNum}
           label={{ value: yLabel, angle: -90, position: "insideLeft", offset: 6,
                    fill: colors.muted, fontSize: 12, style: { textAnchor: "middle" } }} />,
    !forExport && (
      <Tooltip key="tip" formatter={fmtNum}
               contentStyle={{ background: colors.panel, border: `1px solid ${colors.line}`,
                 borderRadius: 8, color: colors.text }} labelStyle={{ color: colors.text }} />
    ),
    !forExport && keys.length > 1 && <Legend key="leg" wrapperStyle={{ fontSize: 12 }} />,
    spec.title && <Customized key="title" component={titleRenderer(spec.title, colors.text)} />,
    ...seriesEl,
    // The fitted trend line — a computed `__trend` series (dashed ochre, no dots,
    // no labels), kept OUT of `keys` so it never gets a LabelList or triggers the
    // legend. Rendered here so it appears in both the on-screen and export charts.
    trendKey && <Line key="trend" type="linear" dataKey={trendKey} name="trend"
                      stroke={colors.ochre} strokeWidth={1.5} strokeDasharray="6 4"
                      dot={false} activeDot={false} isAnimationActive={false} legendType="none" />,
  ].filter(Boolean);
}

export default function Chart({ spec }) {
  const c = useThemeColors();
  const [type, setType] = useState(spec?.type === "bar" ? "bar" : "line");
  const [showLabels, setShowLabels] = useState(false);
  const [showTrend, setShowTrend] = useState(true);
  const [png, setPng] = useState(null);
  const [copied, setCopied] = useState(false);
  const exportRef = useRef(null);

  useEffect(() => {
    let cancelled = false, tries = 0;
    const gen = async () => {
      if (cancelled) return;
      const svg = exportRef.current?.querySelector(".recharts-surface, svg");
      if (!svg || svg.getBoundingClientRect().width < 1) {
        if (tries++ < 30) requestAnimationFrame(gen);
        return;
      }
      try {
        const out = await svgToPngDataUrl(svg, { background: "#ffffff" });
        if (!cancelled && out) setPng(out);
      } catch { /* leave png null */ }
    };
    const t = setTimeout(gen, 60);
    return () => { cancelled = true; clearTimeout(t); };
  }, [type, spec, showLabels, showTrend]);

  if (!spec || !Array.isArray(spec.data) || spec.data.length === 0 || !spec.x) return null;
  const keys = (Array.isArray(spec.y) ? spec.y : [spec.y]).filter(Boolean);
  if (keys.length === 0) return null;

  const isBar = type === "bar";
  const VisChart = isBar ? BarChart : LineChart;
  const margin = { top: spec.title ? 28 : 10, right: 22, bottom: 24, left: 10 };

  // Trend intelligence (single numeric series only): a fitted trend LINE for a
  // line time-series with enough points, and the %-change over the range as a
  // delta badge. Both computed client-side from the already-numeric data. Gated to
  // a TIME-LIKE x-axis — a "% change over the range" / fitted slope is only
  // meaningful along an ordered time axis, never across categorical entities (a
  // A-vs-B comparison, e.g. compare mode, must not read as a bogus trend).
  const timeLikeX = /year|date|month|quarter|day/i.test(String(spec.x || ""));
  const singleKey = keys.length === 1 ? keys[0] : null;
  const trend = (!isBar && singleKey && timeLikeX && spec.data.length >= 3)
    ? trendValues(spec.data, singleKey) : null;
  const delta = (singleKey && timeLikeX) ? pctChange(spec.data, singleKey) : null;
  const trendKey = showTrend && trend ? "__trend" : null;
  const chartData = trendKey
    ? spec.data.map((r, i) => ({ ...r, __trend: trend[i] })) : spec.data;
  const deltaArrow = delta && { up: "▲", down: "▼", flat: "→" }[delta.dir];

  async function copyChart() {
    if (!png) return;
    let ok = false;
    try {
      const blob = await (await fetch(png.url)).blob();
      if (navigator.clipboard?.write && window.ClipboardItem) {
        await navigator.clipboard.write([new window.ClipboardItem({ "image/png": blob })]);
        ok = true;
      }
    } catch { /* fall through */ }
    if (!ok) {
      try {
        const img = document.createElement("img"); img.src = png.url;
        const holder = document.createElement("div");
        holder.setAttribute("contenteditable", "true");
        holder.style.cssText = "position:fixed;left:-9999px;top:0";
        holder.appendChild(img); document.body.appendChild(holder);
        if (img.decode) await img.decode().catch(() => {});  // ensure it's ready to copy
        const range = document.createRange(); range.selectNodeContents(holder);
        const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
        ok = document.execCommand("copy"); sel.removeAllRanges();
        document.body.removeChild(holder);
      } catch { ok = false; }
    }
    if (ok) { setCopied(true); setTimeout(() => setCopied(false), 1400); }
  }

  const alt = `${spec.title || "Chart"}: ${type} chart of ${keys.join(", ")}`
    + (spec.x ? ` by ${spec.x}` : "");

  return (
    <figure className="chart" role="img" aria-label={alt}>
      <div className="chart-head">
        <div className="chart-types" role="group" aria-label="Chart type">
          {["line", "bar"].map((t) => (
            <button key={t} type="button" className={type === t ? "on" : ""}
                    aria-pressed={type === t} onClick={() => setType(t)}>
              {t[0].toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>
        {delta && (
          <span className={"chart-delta " + delta.dir}
                title={`${Math.abs(delta.pct).toFixed(1)}% change over the range shown`}>
            {deltaArrow} {Math.abs(delta.pct).toFixed(1)}%
          </span>
        )}
        <div className="chart-head-tools">
          {trend && (
            <button type="button" className={"pill-toggle" + (showTrend ? " on" : "")}
                    aria-pressed={showTrend} onClick={() => setShowTrend((v) => !v)}>
              Trend
            </button>
          )}
          <button type="button" className={"pill-toggle" + (showLabels ? " on" : "")}
                  aria-pressed={showLabels} onClick={() => setShowLabels((v) => !v)}>
            Data labels
          </button>
          <button type="button" className="link ico" onClick={copyChart}
                  disabled={!png} title="Copy this chart as an image">
            <IconCopy />{copied ? "Copied!" : "Copy image"}
          </button>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={320}>
        <VisChart data={chartData} margin={margin}>
          {chartChildren({ colors: c, isBar, keys, spec, showLabels, forExport: false, trendKey })}
        </VisChart>
      </ResponsiveContainer>

      {/* Hidden, fixed-size, LIGHT, no-animation chart — the source for the PNG. */}
      <div className="chart-export-src" aria-hidden="true" ref={exportRef}>
        <VisChart width={EXPORT_W} height={EXPORT_H} data={chartData} margin={margin}>
          {chartChildren({ colors: LIGHT, isBar, keys, spec, showLabels, forExport: true, trendKey })}
        </VisChart>
      </div>

      {png && <img className="chart-export-img" alt={spec.title || "chart"}
                   src={png.url} data-w={png.w} data-h={png.h} aria-hidden="true" />}
    </figure>
  );
}
