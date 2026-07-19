import React, { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import Chart from "./Chart.jsx";
import SqlBlock from "./SqlBlock.jsx";
import { normalizeMarkdown } from "./mdnorm.js";
import { chartSpecFromTable, downloadCsv, extractTable } from "./tabledata.js";

function codeText(children) {
  return Array.isArray(children) ? children.join("") : String(children ?? "");
}

// A markdown result table plus a per-table toolbar: download THIS table as CSV
// (fixes the old single-per-message CSV that broke with multiple tables) and,
// when the data supports it, chart it inline with a switchable type.
function DataTable({ node, ...props }) {
  const { headers, rows } = useMemo(() => extractTable(node), [node]);
  const spec = useMemo(() => chartSpecFromTable(headers, rows), [headers, rows]);
  const [showChart, setShowChart] = useState(false);
  // Distinct region name (its column headers) so multiple tables in one answer
  // don't all read as an identical "Result table" landmark.
  const label = headers.length ? `Result table: ${headers.join(", ").slice(0, 60)}` : "Result table";
  return (
    <div className="table-block">
      <div className="table-wrap" tabIndex={0} role="region" aria-label={label}>
        <table {...props} />
      </div>
      <div className="table-tools">
        <button type="button" className="link"
                onClick={() => downloadCsv(headers, rows)}>Download CSV</button>
        {spec && (
          <button type="button" className="link" aria-pressed={showChart}
                  onClick={() => setShowChart((s) => !s)}>
            {showChart ? "Hide chart" : "Chart this"}
          </button>
        )}
      </div>
      {showChart && spec && <Chart spec={spec} />}
    </div>
  );
}

// A ```chart fenced block (compact JSON spec) renders as a Recharts figure; a
// memoized spec (keyed by the raw text) keeps the Chart from remounting/losing
// its type on re-render. Bad JSON falls back to the raw code block. A ```sql
// fence is syntax-highlighted (highlight-only — the author's own layout is kept,
// no reformat) so SQL reads the same everywhere it appears.
function Pre({ node, ...props }) {
  const child = props.children;
  const cn = child?.props?.className || "";
  const isChart = /\blanguage-chart\b/.test(cn);
  const isSql = /\blanguage-sql\b/.test(cn);
  const raw = (isChart || isSql) ? codeText(child.props.children) : "";
  const spec = useMemo(() => {
    if (!isChart) return null;
    try { return JSON.parse(raw.trim()); } catch { return null; }
  }, [isChart, raw]);
  if (spec) return <Chart spec={spec} />;
  if (isSql) return <SqlBlock code={raw.replace(/\n$/, "")} format={false} />;
  return <pre {...props} />;
}

const Th = ({ node, ...props }) => <th {...props} scope="col" />;
const Anchor = ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />;

// Stable references — defined once so react-markdown never remounts the subtree
// (which would reset a chart's selected type). See Pre/DataTable above.
const MD_COMPONENTS = { table: DataTable, th: Th, a: Anchor, pre: Pre };

// GFM gives us tables, which the analyst answers rely on. The source is first run
// through normalizeMarkdown to repair malformed tables (header/delimiter column
// mismatch) that GFM would otherwise drop.
export default function Markdown({ children }) {
  const src = typeof children === "string" ? normalizeMarkdown(children) : children;
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
        {src}
      </ReactMarkdown>
    </div>
  );
}
