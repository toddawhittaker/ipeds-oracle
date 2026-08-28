// Admin → Usage: aggregate-only usage dashboard (never verbatim question text).
// Split out of Admin.jsx unchanged.
import React, { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { loadErrorMessage } from "../authcopy.js";
import Chart from "../Chart.jsx";
import { shortZone } from "../datetime.js";
import { IconInfo } from "../icons.jsx";
import HelpPopover from "../HelpPopover.jsx";
import TableScroll from "../TableScroll.jsx";
import { STAT_INFO, directionHint } from "../usageinfo.js";
import {
  exhaustionLabel,
  groundedFigureLabel, groundedFigureRate, groundedTableLabel, groundedTableRate,
  leakLabel, leakRate,
  promptCacheRate, schemaCacheRate,
  spendEstimated, spendLabel,
} from "../usagestats.js";
import { money } from "./format.js";

const RANGES = [
  { key: "hour", label: "Hour", secs: 3600 },
  { key: "day", label: "Day", secs: 86400 },
  { key: "7d", label: "7 days", secs: 7 * 86400 },
  { key: "30d", label: "30 days", secs: 30 * 86400 },
];

const METRICS = ["queries", "tokens", "spend"];

// The two doors onto the agent (usage_log.source, migration 37). "Web chat" is
// every row the MCP endpoint did not write, which includes every row predating
// it -- see _source_sql in backend/app/routers/admin.py for why the split is
// written that way round.
const SOURCES = [
  { key: "all", label: "All" },
  { key: "web", label: "Web chat" },
  { key: "mcp", label: "MCP" },
];

export default function Usage() {
  const [range, setRange] = useState("7d");
  const [custom, setCustom] = useState({ since: "", until: "" });
  const [metric, setMetric] = useState("tokens");
  const [source, setSource] = useState("all");
  const [u, setU] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  // `<input type=date>` values are parsed as LOCAL midnight (matching the local
  // "now" used by the quick ranges), not UTC, so the window aligns with the
  // admin's day.
  useEffect(() => {
    const now = Date.now() / 1000;
    let since, until;
    if (range === "custom") {
      since = custom.since ? new Date(`${custom.since}T00:00:00`).getTime() / 1000 : now - 7 * 86400;
      until = custom.until ? new Date(`${custom.until}T23:59:59`).getTime() / 1000 : now;
    } else {
      since = now - RANGES.find((r) => r.key === range).secs;
      until = now;
    }
    api.usage(since, until, source)
      .then((d) => { setU(d); setErr(""); })
      // A swallowed failure left `u` null forever, and the render gate is `!u`
      // -- so a failed load rendered "Loading…" PERMANENTLY, with `loading`
      // already false so even the "updating…" hint was gone.
      .catch((e) => setErr(loadErrorMessage("usage", e?.detail)))
      .finally(() => setLoading(false));
  }, [range, custom, source]);

  const pick = (fn) => { setLoading(true); fn(); };
  const t = u?.totals || {};
  const spec = useMemo(() => {
    const s = u?.series || [];
    const zone = shortZone();
    return s.length ? {
      type: "line", x: "t", y: [metric], yLabel: metric === "spend" ? "USD" : metric,
      xLabel: zone ? `Time (${zone})` : "Time",
      data: s.map((r) => ({ t: r.t, queries: r.queries, tokens: r.tokens, spend: Number(r.spend) })),
    } : null;
  }, [u, metric]);

  return (
    <div className="panel">
      <h2 className="sr-only">Usage</h2>
      <div className="usage-filters">
        <div className="usage-range" role="group" aria-label="Time range">
          {RANGES.map((r) => (
            <button key={r.key} className={range === r.key ? "on" : ""} aria-pressed={range === r.key}
                    onClick={() => pick(() => setRange(r.key))}>{r.label}</button>
          ))}
          <button className={range === "custom" ? "on" : ""} aria-pressed={range === "custom"}
                  onClick={() => pick(() => setRange("custom"))}>Custom</button>
          {range === "custom" && (
            <span className="usage-custom">
              <input type="date" value={custom.since} aria-label="From date"
                     onChange={(e) => pick(() => setCustom((c) => ({ ...c, since: e.target.value })))} />
              <span className="muted">to</span>
              <input type="date" value={custom.until} aria-label="To date"
                     onChange={(e) => pick(() => setCustom((c) => ({ ...c, until: e.target.value })))} />
            </span>
          )}
          {loading && u && <span className="muted small">updating…</span>}
        </div>
        <div className="usage-range" role="group" aria-label="Source">
          {SOURCES.map((x) => (
            <button key={x.key} className={source === x.key ? "on" : ""}
                    aria-pressed={source === x.key}
                    onClick={() => pick(() => setSource(x.key))}>{x.label}</button>
          ))}
        </div>
      </div>

      {err ? <p className="denied-error" role="alert">{err}</p>
       : !u ? <div className="muted">Loading…</div> : (
        <div className={"usage-body" + (loading ? " updating" : "")}>
          {u.cost_warning && (
            <p className="notice warn small" role="status">
              <strong>Spend isn’t being recorded.</strong> Your LLM provider isn’t
              reporting per-request cost and no fallback prices are set, so “Spend”
              reads $0 despite real activity. Set <code>LLM_INPUT_COST_PER_MTOK</code>{" "}
              and <code>LLM_OUTPUT_COST_PER_MTOK</code> in the server’s environment
              to estimate it (see the admin guide). This clears once cost data
              appears or those prices are set.
            </p>
          )}
          <p className="usage-privacy muted small">
            Every number here is computed locally, on this server, from its own
            database — and is shown only to signed-in admins. None of it is ever
            sent to a central server, telemetry service, or any third party; your
            usage data never leaves this machine. Hover or tap the ⓘ on any stat
            for what it means and which way is good.
          </p>
          {/* Grouped into three bands so an admin can tell operational
              volume/cost from efficiency from answer-quality at a glance. */}
          <div className="stat-band">
            <div className="field-label">Activity</div>
            <div className="stats">
              <Stat label="Queries" value={(t.queries || 0).toLocaleString()} info={STAT_INFO.queries} />
              <Stat label="Tokens" value={(t.tokens || 0).toLocaleString()} info={STAT_INFO.tokens} />
              {/* "~" marks a figure we ESTIMATED from list prices rather than one
                  the provider billed — see spendEstimated(). The label carries the
                  split when a window holds both kinds. */}
              <Stat label={spendLabel(t)}
                    value={spendEstimated(t) ? `~${money(t.spend)}` : money(t.spend)}
                    info={STAT_INFO.spend} />
            </div>
          </div>
          <div className="stat-band">
            <div className="field-label">Efficiency</div>
            <div className="stats">
              <Stat label="Answer cache" value={t.cache_hits || 0} info={STAT_INFO.answerCache} />
              <Stat label="Schema cache" value={schemaCacheRate(t)} info={STAT_INFO.schemaCache} />
              <Stat label="Prompt cache" value={promptCacheRate(t)} info={STAT_INFO.promptCache} />
              <Stat label="Escalations" value={t.escalations || 0} info={STAT_INFO.escalations} />
            </div>
          </div>
          <div className="stat-band">
            <div className="field-label">Answer quality</div>
            <div className="stats">
              <Stat label={groundedFigureLabel(t)} value={groundedFigureRate(t)} info={STAT_INFO.groundedFigures} />
              <Stat label={groundedTableLabel(t)} value={groundedTableRate(t)} info={STAT_INFO.groundedCells} />
              <Stat label={leakLabel(t)} value={leakRate(t)} info={STAT_INFO.answerLeaks} />
              <Stat label="Failures" value={t.failures || 0} info={STAT_INFO.failures} />
              <Stat label={exhaustionLabel(t)} value={(t.exhausted_turns || 0).toLocaleString()} info={STAT_INFO.exhausted} />
            </div>
          </div>

          <div className="usage-chart-head">
            <h3>{metric[0].toUpperCase() + metric.slice(1)} over time ({u.bucket === "hour" ? "hourly" : "daily"})</h3>
            <div className="chart-types" role="group" aria-label="Metric">
              {METRICS.map((m) => (
                <button key={m} className={metric === m ? "on" : ""} aria-pressed={metric === m}
                        onClick={() => setMetric(m)}>{m[0].toUpperCase() + m.slice(1)}</button>
              ))}
            </div>
          </div>
          {spec ? <Chart spec={spec} /> : <div className="muted">No activity in this range.</div>}

          <h3>Top users</h3>
          {/* The same reflow scroll region DataTable.jsx uses (WCAG 1.4.10).
              This table sets no column widths, but an email address is one
              unbreakable token — measured at 526px for an ordinary long
              address, which made the whole `.admin` column scroll sideways at
              320px. No `min-width` here: the table is otherwise fluid, so it
              only scrolls when its own content is genuinely too wide.
              FOCUSABLE, unlike DataTable's: every cell here is plain text, so
              there is nothing inside for a keyboard to land on and nothing to
              scroll the region into view. Shipping the wrapper without this
              made the Tokens/Spend columns unreachable by keyboard the moment
              a long address pushed them out (WCAG 2.1.1) -- axe's
              scrollable-region-focusable, invisible to our gate only because
              the scans run at 1280x2600 where a fluid table never overflows. */}
          <TableScroll focusable label="Top users">
          <table className="grid" aria-label="Top users">
            <thead><tr>
              <th scope="col">User</th><th scope="col">Queries</th>
              <th scope="col">Tokens</th><th scope="col">Spend</th>
            </tr></thead>
            <tbody>{u.top_users.map((x) => (
              <tr key={x.email}>
                <td>{x.email}</td><td>{x.queries}</td>
                <td>{(x.tokens || 0).toLocaleString()}</td><td>{money(x.spend)}</td>
              </tr>
            ))}</tbody>
          </table>
          </TableScroll>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, info }) {
  // Label BEFORE value in the DOM so a screen reader hears the name then the
  // number ("Queries … 1,234"), not the reverse; `.stat` is column-reverse so the
  // big value still sits visually on top.
  return (
    <div className="stat">
      <div className="l">
        <span>{label}</span>
        {info && (
          <HelpPopover label={`What “${info.name}” measures`} icon={IconInfo}
                       className="help-compact">
            <div className="help-body statinfo">
              <p>{info.what}</p>
              {info.note && <p className="statinfo-note">{info.note}</p>}
              <p className={"statinfo-dir dir-" + info.direction}>
                {directionHint(info.direction)}
              </p>
            </div>
          </HelpPopover>
        )}
      </div>
      <div className="v">{value}</div>
    </div>
  );
}

