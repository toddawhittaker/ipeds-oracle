import React, { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import Chart from "./Chart.jsx";
import SqlBlock from "./SqlBlock.jsx";
import { normalizeMarkdown } from "./mdnorm.js";
import { briefLayout } from "./briefdata.js";
import { chartSpecFromTable, columnIsNumeric, countMarkdownTables, downloadCsv, downloadServerCsv, extractTable, sortRows } from "./tabledata.js";
import { comparableTable, compareSpec } from "./compare.js";

function codeText(children) {
  return Array.isArray(children) ? children.join("") : String(children ?? "");
}

const MAX_COMPARE = 4; // palette has 6 colors; keep the chart legible

// The up/down sort glyph in a header: both triangles dim when the column is
// unsorted (a "sortable" hint), the active direction lit in the accent.
function SortCaret({ dir }) {
  return (
    <span className="sort-caret" aria-hidden="true">
      <svg width="8" height="13" viewBox="0 0 8 13">
        <path d="M4 0 L7.5 4 L0.5 4 Z" className={dir === "asc" ? "act" : ""} />
        <path d="M4 13 L7.5 9 L0.5 9 Z" className={dir === "desc" ? "act" : ""} />
      </svg>
    </span>
  );
}

// The displayed result table, rendered from the EXTRACTED headers/rows (not
// react-markdown's pass-through) so the columns are click-to-sort with up/down
// indicators. Sorting is DISPLAY-ONLY — a new row order from sortRows, keyed off
// the visible cell text; it never refetches and never changes the CSV (which
// re-runs the full query server-side). Compare mode's leading checkbox is
// rendered inline here (selection keyed by the entity LABEL, so it survives a
// re-sort); that's why the old CompareContext/tr-override indirection is gone.
// Cells are the extracted text (data tables carry numbers/names, not inline
// markdown), which is what CSV/chart/compare already used.
function SortableTable({ headers, rows, cmp, selected, toggle, label }) {
  const [sort, setSort] = useState({ col: null, dir: null });
  const numericByCol = useMemo(
    () => headers.map((_, c) => columnIsNumeric(rows, c)), [headers, rows]);
  const shown = useMemo(
    () => sortRows(rows, sort.col, sort.dir, numericByCol[sort.col]),
    [rows, sort, numericByCol]);

  // asc → desc → back to the original (query) order.
  const clickHeader = (c) => setSort((s) => (
    s.col !== c ? { col: c, dir: "asc" }
      : s.dir === "asc" ? { col: c, dir: "desc" }
        : { col: null, dir: null }));

  const comparable = !!cmp;
  const entityCol = cmp?.entityCol ?? -1;

  return (
    <>
    <div className="table-wrap" tabIndex={0} role="region" aria-label={label}>
      <table>
        <thead>
          <tr>
            {comparable && <th className="cmp-cell" aria-hidden="true" />}
            {headers.map((h, c) => (
              <th key={c} scope="col"
                  aria-sort={sort.col === c ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}>
                <button type="button" className="th-sort" title={`Sort by ${h}`}
                        onClick={() => clickHeader(c)}>
                  <span>{h}</span>
                  <SortCaret dir={sort.col === c ? sort.dir : null} />
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((r, i) => {
            const rowLabel = comparable ? String(r[entityCol] ?? "") : "";
            const checked = comparable && selected.has(rowLabel);
            const atCap = comparable && !checked && selected.size >= MAX_COMPARE;
            return (
              <tr key={i} className={comparable && checked ? "cmp-row picked" : undefined}>
                {comparable && (
                  <td className="cmp-cell">
                    <input type="checkbox" checked={checked} disabled={atCap}
                           aria-label={`Compare ${rowLabel}`}
                           onChange={() => toggle(rowLabel)} />
                  </td>
                )}
                {r.map((cell, c) => <td key={c}>{cell}</td>)}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
    {/* Sorting is client-side over the rows on screen — which, for a large
        listing, is only the first page the model transcribed. Say so, so
        "sort ascending to find the smallest" isn't read as a global answer.
        The full set is in the CSV (whole-query order). */}
    {sort.col != null && (
      <p className="sort-note" role="note">
        Sorted the {shown.length} rows shown here — download the CSV for the full result.
      </p>
    )}
    </>
  );
}

// A markdown result table plus a per-table toolbar: download THIS table as CSV
// (fixes the old single-per-message CSV that broke with multiple tables) and,
// when the data supports it, chart it inline with a switchable type.
//
// Brief pairing (briefdata.js): when the answer is a single-number "brief" (one
// table + one chart), `sideChart` is the answer's own chart, rendered SIDE BY SIDE
// with this table, and `pairChart` drops the "Chart this" toggle (a chart is
// already shown — the toggle would be redundant).
function DataTable({ node, sideChart, pairChart, serverCsvId }) {
  const { headers, rows } = useMemo(() => extractTable(node), [node]);
  const inferred = useMemo(() => chartSpecFromTable(headers, rows), [headers, rows]);
  const [showChart, setShowChart] = useState(false);

  // Compare mode: only offered on a comparable (entity-row) table, and never when
  // the table is already paired with a brief's chart. Selection is a Set of entity
  // labels; picking 2+ shows an instant snapshot bar chart of just those rows.
  const cmp = useMemo(
    () => (pairChart ? null : comparableTable(headers, rows)), [pairChart, headers, rows]);
  const [selected, setSelected] = useState(() => new Set());
  const [showCompare, setShowCompare] = useState(false);
  const toggle = (labelText) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(labelText)) next.delete(labelText);
    else if (next.size < MAX_COMPARE) next.add(labelText);
    return next;
  });
  const clearCompare = () => { setSelected(new Set()); setShowCompare(false); };
  const cmpSpec = useMemo(
    () => (cmp && showCompare ? compareSpec(cmp.spec, [...selected]) : null),
    [cmp, showCompare, selected]);

  // Distinct region name (its column headers) so multiple tables in one answer
  // don't all read as an identical "Result table" landmark.
  const label = headers.length ? `Result table: ${headers.join(", ").slice(0, 60)}` : "Result table";
  const table = (
    <div className="table-block">
      <SortableTable headers={headers} rows={rows} cmp={cmp}
                     selected={selected} toggle={toggle} label={label} />
      <div className="table-tools">
        {/* When the answer is a single table with a persisted message id, the
            CSV comes from the server (the FULL dataset, re-running the query at
            the large download cap) instead of the ≤200 visible rows. Multi-table
            answers and live (not-yet-saved) turns fall back to the client-side
            CSV of exactly what's shown. */}
        <button type="button" className="link"
                onClick={() => (serverCsvId
                  ? downloadServerCsv(serverCsvId, headers.length)
                  : downloadCsv(headers, rows))}>Download CSV</button>
        {/* Offer "Chart this" only when the answer isn't ALREADY providing a chart
            (in a brief it is — beside the table — so the toggle is redundant). */}
        {!pairChart && inferred && (
          <button type="button" className="link" aria-pressed={showChart}
                  onClick={() => setShowChart((s) => !s)}>
            {showChart ? "Hide chart" : "Chart this"}
          </button>
        )}
      </div>
      {/* Compare bar — appears once at least one row is ticked; the action enables
          at 2 (nothing to compare with fewer). */}
      {cmp && selected.size >= 1 && (
        <div className="compare-bar" role="group" aria-label="Compare selected rows">
          <span className="compare-count">{selected.size} selected</span>
          <button type="button" className="link" disabled={selected.size < 2}
                  onClick={() => setShowCompare(true)}>
            Compare {selected.size} &rarr;
          </button>
          <button type="button" className="link" onClick={clearCompare}>Clear</button>
        </div>
      )}
      {cmpSpec && (
        <div className="compare-panel">
          <div className="compare-panel-head">
            <span className="field-label">Comparing {cmpSpec.data.length}</span>
            <button type="button" className="link"
                    onClick={() => setShowCompare(false)}>Close</button>
          </div>
          <Chart spec={cmpSpec} />
        </div>
      )}
      {!pairChart && showChart && inferred && <Chart spec={inferred} />}
    </div>
  );
  // Compact table + the answer's trend chart. Side by side ONLY when the table is
  // small enough to share the row without its cells (long program / institution
  // names are nowrap) overflowing UNDER the chart — i.e. the brief's intended
  // recent-years strip: a couple of columns, a handful of rows. A wider (>3 cols)
  // OR taller (>8 rows) table STACKS instead — chart below the full-width table —
  // per "if we can't shrink the table, put the graph below it". (The earlier
  // headers.length > 4 threshold let a 4-column ranking table sit beside the chart,
  // where its nowrap cells slid under it.) The side-by-side row also wraps to
  // stacked on a narrow viewport (see .brief-figrow).
  if (sideChart) {
    const wide = headers.length > 3 || rows.length > 8;
    return (
      <div className={"brief-figrow" + (wide ? " stacked" : "")}>
        {table}<Chart spec={sideChart} />
      </div>
    );
  }
  return table;
}

// A ```chart fenced block (compact JSON spec) renders as a Recharts figure; a
// memoized spec (keyed by the raw text) keeps the Chart from remounting/losing
// its type on re-render. Bad JSON falls back to the raw code block. A ```sql
// fence is syntax-highlighted (highlight-only — the author's own layout is kept,
// no reformat) so SQL reads the same everywhere it appears. `suppressChart` hides
// the standalone chart when it's been paired beside the brief's table.
function Pre({ node, suppressChart, ...props }) {
  const child = props.children;
  const cn = child?.props?.className || "";
  const isChart = /\blanguage-chart\b/.test(cn);
  const isSql = /\blanguage-sql\b/.test(cn);
  const raw = (isChart || isSql) ? codeText(child.props.children) : "";
  const spec = useMemo(() => {
    if (!isChart) return null;
    try { return JSON.parse(raw.trim()); } catch { return null; }
  }, [isChart, raw]);
  if (isChart && suppressChart) return null; // rendered beside the table (brief)
  if (spec) return <Chart spec={spec} />;
  if (isSql) return <SqlBlock code={raw.replace(/\n$/, "")} format={false} />;
  return <pre {...props} />;
}

const Anchor = ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />;

// GFM gives us tables, which the analyst answers rely on. The source is first run
// through normalizeMarkdown to repair malformed tables (header/delimiter column
// mismatch) that GFM would otherwise drop.
export default function Markdown({ children, messageId }) {
  const src = typeof children === "string" ? normalizeMarkdown(children) : children;
  const brief = useMemo(() => briefLayout(src), [src]);
  // Server-side full-dataset CSV is only unambiguous when the answer has exactly
  // ONE table (the download endpoint re-runs the answer's FINAL SQL, so a
  // second table's button would otherwise get the wrong query's rows). Count the
  // GFM delimiter rows (all of |, :, -, space with a run of dashes — never a
  // data row or a `---` horizontal rule, which has no pipe).
  const serverCsvId = useMemo(
    () => (messageId != null && countMarkdownTables(src) === 1 ? messageId : null),
    [src, messageId]);
  // Components are memoized on the brief pairing (stable per message source), so
  // react-markdown never remounts the subtree and resets a chart's selected type.
  const components = useMemo(() => ({
    table: (p) => <DataTable {...p} pairChart={brief.pair}
                             sideChart={brief.pair ? brief.chart : null}
                             serverCsvId={serverCsvId} />,
    a: Anchor,
    pre: (p) => <Pre {...p} suppressChart={brief.pair} />,
  }), [brief, serverCsvId]);
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {src}
      </ReactMarkdown>
    </div>
  );
}
