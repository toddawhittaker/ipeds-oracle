import React, { useEffect, useRef, useState } from "react";
import {
  Bar, BarChart, CartesianGrid, Customized, LabelList, Legend, Line, LineChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { svgToPngDataUrl } from "./chartimg.js";
import { IconCopy, IconCheck, IconTag, IconMaximize } from "./icons.jsx";
import ChartModal from "./ChartModal.jsx";
import { pctChange, trendValues } from "./trendstats.js";

// Series colors. Muted/earthy to match the app's archival cream+teal+ochre
// aesthetic (the old palette read as neon on both themes), but still six
// clearly-separated hues. ORDER matters: teal · ochre · plum · clay · sage ·
// slate-blue, so the two greens (teal, sage) are never neighbors and a 2–3
// series chart draws from maximally-separated hues (green/orange/purple) —
// green-vs-green (which collapses under deuteranopia) only appears at 5+ series.
// Two variants: deeper tones for the light cream panel, lighter tints for the
// dark green-ink panel. The first color is the app teal, so a single-series bar
// chart is on-brand.
const PALETTE_LIGHT = ["#2f6f68", "#b07a2e", "#7d5a86", "#b05f50", "#5f7d4f", "#4a6b8a"];
const PALETTE_DARK = ["#6bb3aa", "#cf9a54", "#b592c2", "#cf8479", "#8aa878", "#7ea3c4"];

// Exports are always rendered light so a pasted chart looks right in a document
// regardless of the app's current theme — so they carry the light palette.
const LIGHT = { text: "#1a1f26", muted: "#6b7280", line: "#e3e6ea", ochre: "#a66a12",
                palette: PALETTE_LIGHT, cursorFill: "rgba(20,26,24,0.06)" };
const EXPORT_W = 620, EXPORT_H = 320;

const fmtNum = (v) => (typeof v === "number" ? v.toLocaleString() : v);

function useThemeColors() {
  const read = () => {
    const cs = getComputedStyle(document.documentElement);
    const g = (n, d) => cs.getPropertyValue(n).trim() || d;
    // Resolve the active theme the same way the app does: an explicit
    // data-theme wins, else the OS preference. Drives which series palette
    // (deeper for light, lighter for dark) reads well on the current panel.
    const attr = document.documentElement.getAttribute("data-theme");
    const dark = attr === "dark"
      || (attr == null && !!window.matchMedia?.("(prefers-color-scheme: dark)").matches);
    return {
      text: g("--text", "#1a1f26"), muted: g("--muted", "#6b7280"),
      line: g("--line", "#e3e6ea"), panel: g("--panel", "#ffffff"),
      ochre: g("--ochre", "#a66a12"),
      palette: dark ? PALETTE_DARK : PALETTE_LIGHT,
      // The bar-hover cursor highlight — a soft translucent overlay, not
      // Recharts' default opaque light-gray rect (which reads as a jarring
      // white block on the dark panel). Tinted per theme.
      cursorFill: dark ? "rgba(230,236,232,0.08)" : "rgba(20,26,24,0.06)",
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
  return function ChartTitle(props) {
    // Wrap a long title onto up to 2 lines so a narrow (side-by-side) chart doesn't
    // clip it. x="50%" keeps centering reliable; the width prop is used only as an
    // approximate character budget (a wide export chart keeps the title on one line).
    const w = Number(props?.width) || 320;
    const maxChars = Math.max(12, Math.floor((w - 30) / 6.6));
    const lines = wrapLabel(title, maxChars, 2);
    return (
      <text x="50%" y={15} textAnchor="middle" fontSize={13} fontWeight="600" fill={fill}>
        {lines.map((ln, i) => (
          <tspan key={i} x="50%" dy={i === 0 ? 0 : 14}>{ln}</tspan>
        ))}
      </text>
    );
  };
}

// Split a long category label into up to `maxLines` word-wrapped lines of ~`max`
// chars — so full institution names read on the x-axis without angling (which
// clips the first bar off the container edge) or truncation (which collides on
// shared prefixes like "The University of …").
function wrapLabel(text, max = 16, maxLines = 3) {
  const words = String(text ?? "").split(/\s+/).filter(Boolean);
  const lines = [];
  let cur = "";
  for (const w of words) {
    if (cur && (cur + " " + w).length > max) { lines.push(cur); cur = w; }
    else cur = cur ? `${cur} ${w}` : w;
  }
  if (cur) lines.push(cur);
  if (lines.length > maxLines) {
    lines[maxLines - 1] += "…";
    lines.length = maxLines;
  }
  return lines.length ? lines : [""];
}

// A centered, multi-line x-axis tick for long category labels.
function WrapTick({ x, y, payload, fill }) {
  const lines = wrapLabel(payload?.value);
  return (
    <text x={x} y={y} textAnchor="middle" fill={fill} fontSize={11}>
      {lines.map((ln, i) => (
        <tspan key={i} x={x} dy={i === 0 ? 12 : 12}>{ln}</tspan>
      ))}
    </text>
  );
}

// Build the chart's children once so the on-screen and export charts stay in
// sync. `forExport` drops interactive-only bits (tooltip/legend).
function chartChildren({ colors, isBar, keys, spec, showLabels, forExport, trendKey, longLabels }) {
  const xLabel = spec.xLabel || spec.x;
  const yLabel = spec.yLabel || (keys.length === 1 ? keys[0] : "");
  const tick = { fill: colors.muted, fontSize: 12 };
  // Categorical axes show ALL ticks (never drop a bar's label); long ones wrap onto
  // multiple centered lines so they fit. A wrapped axis omits the centered axis title
  // (it would collide with the taller ticks).
  const xAxisExtra = longLabels
    ? { interval: 0, height: 56, tick: <WrapTick fill={colors.muted} /> }
    : (isBar ? { interval: 0 } : {});
  const xAxisLabel = longLabels ? undefined
    : { value: xLabel, position: "insideBottom", offset: -6, fill: colors.muted, fontSize: 12 };
  const seriesEl = keys.map((key, i) => {
    const palette = colors.palette || PALETTE_LIGHT;
    const color = palette[i % palette.length];
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
           {...xAxisExtra} label={xAxisLabel} />,
    <YAxis key="y" width={70} stroke={colors.muted} tick={tick} tickFormatter={fmtNum}
           label={{ value: yLabel, angle: -90, position: "insideLeft", offset: 6,
                    fill: colors.muted, fontSize: 12, style: { textAnchor: "middle" } }} />,
    !forExport && (
      <Tooltip key="tip" formatter={fmtNum}
               cursor={isBar ? { fill: colors.cursorFill }
                             : { stroke: colors.muted, strokeWidth: 1 }}
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

// `inModal` renders a bigger chart (in the maximize dialog) and hides its own
// maximize control; initial* carry the opener chart's current type/trend/labels
// into the modal so it opens showing what you were looking at.
export default function Chart({ spec, inModal = false, initialType, initialTrend, initialLabels }) {
  const c = useThemeColors();
  const [type, setType] = useState(initialType ?? (spec?.type === "bar" ? "bar" : "line"));
  const [showLabels, setShowLabels] = useState(initialLabels ?? false);
  const [showTrend, setShowTrend] = useState(initialTrend ?? true);
  const [png, setPng] = useState(null);
  const [copied, setCopied] = useState(false);
  const [maxed, setMaxed] = useState(false);
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
  // Long CATEGORY labels (e.g. full university names in a comparison) collide on a
  // horizontal axis, and Recharts silently DROPS the ones that would overlap — a real
  // bug (a bar with no label is unreadable). Force every tick to render and angle the
  // long ones, giving the axis extra vertical room.
  const catLabels = spec.data.map((r) => String(r?.[spec.x] ?? ""));
  const longLabels = isBar && catLabels.some((l) => l.length > 12);
  // Reserve vertical room for a title that may wrap to two lines on a narrow chart.
  const margin = { top: spec.title ? 40 : 10, right: 22, bottom: longLabels ? 12 : 24, left: 10 };
  const chartH = inModal ? (longLabels ? 560 : 460) : (longLabels ? 372 : 320);

  // Trend intelligence (single numeric series only): a fitted trend LINE for a
  // line time-series with enough points, and the %-change over the range as a
  // delta badge. Both computed client-side from the already-numeric data. Gated to
  // a TIME-LIKE x-axis — a "% change over the range" / fitted slope is only
  // meaningful along an ordered time axis, never across categorical entities (a
  // A-vs-B comparison, e.g. compare mode, must not read as a bogus trend).
  const timeLikeX = /year|date|month|quarter|day/i.test(String(spec.x || ""));
  const singleKey = keys.length === 1 ? keys[0] : null;
  // Trend ELIGIBILITY depends on the data (a single numeric time-series with enough
  // points) — NOT on the current type — so "Line + trend" stays in the dropdown even
  // while "Bar" is selected (pick it to jump straight to a trended line). The fitted
  // trend LINE is only drawn on a line chart (a trend line over bars is meaningless).
  const trendEligible = !!singleKey && timeLikeX && spec.data.length >= 3;
  const trend = (trendEligible && !isBar) ? trendValues(spec.data, singleKey) : null;
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

  // One compact <select> collapses the old Line/Bar buttons AND the Trend toggle:
  // "Line + trend" is a line subtype, offered whenever the data is trend-eligible
  // (independent of the current type). Keeps the toolbar narrow enough to sit beside
  // a table without its controls overflowing the chart.
  const typeValue = isBar ? "bar" : (showTrend && trendEligible ? "line-trend" : "line");
  function onTypeChange(v) {
    if (v === "bar") { setType("bar"); return; }
    setType("line");
    setShowTrend(v === "line-trend");
  }

  return (
    <>
    <figure className="chart" role="img" aria-label={alt}>
      <div className="chart-head">
        <select className="chart-type" value={typeValue} aria-label="Chart type"
                onChange={(e) => onTypeChange(e.target.value)}>
          <option value="line">Line</option>
          {trendEligible && <option value="line-trend">Line + trend</option>}
          <option value="bar">Bar</option>
        </select>
        {delta && (
          <span className={"chart-delta " + delta.dir}
                title={`${Math.abs(delta.pct).toFixed(1)}% change over the range shown`}>
            {deltaArrow} {Math.abs(delta.pct).toFixed(1)}%
          </span>
        )}
        <div className="chart-head-tools">
          <button type="button" className={"chart-ico-toggle" + (showLabels ? " on" : "")}
                  aria-pressed={showLabels} onClick={() => setShowLabels((v) => !v)}
                  title="Show data labels" aria-label="Show data labels">
            <IconTag />
          </button>
          <button type="button" className="chart-ico-btn" onClick={copyChart}
                  disabled={!png}
                  title={copied ? "Copied!" : "Copy chart as an image"}
                  aria-label={copied ? "Chart image copied" : "Copy chart as an image"}>
            {copied ? <IconCheck /> : <IconCopy />}
          </button>
          {!inModal && (
            <button type="button" className="chart-ico-btn" onClick={() => setMaxed(true)}
                    title="Maximize chart" aria-label="Maximize chart">
              <IconMaximize />
            </button>
          )}
        </div>
      </div>

      <ResponsiveContainer width="100%" height={chartH}>
        <VisChart data={chartData} margin={margin}>
          {chartChildren({ colors: c, isBar, keys, spec, showLabels, forExport: false, trendKey, longLabels })}
        </VisChart>
      </ResponsiveContainer>

      {/* Hidden, fixed-size, LIGHT, no-animation chart — the source for the PNG. */}
      <div className="chart-export-src" aria-hidden="true" ref={exportRef}>
        <VisChart width={EXPORT_W} height={longLabels ? 380 : EXPORT_H} data={chartData} margin={margin}>
          {chartChildren({ colors: LIGHT, isBar, keys, spec, showLabels, forExport: true, trendKey, longLabels })}
        </VisChart>
      </div>

      {png && <img className="chart-export-img" alt={spec.title || "chart"}
                   src={png.url} data-w={png.w} data-h={png.h} aria-hidden="true" />}
    </figure>
    {maxed && (
      <ChartModal spec={spec} initialType={type} initialTrend={showTrend}
                  initialLabels={showLabels} onClose={() => setMaxed(false)} />
    )}
    </>
  );
}
