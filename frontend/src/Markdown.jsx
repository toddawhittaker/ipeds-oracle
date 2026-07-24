import React, { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import Chart from "./Chart.jsx";
import SqlBlock from "./SqlBlock.jsx";
import { normalizeMarkdown } from "./mdnorm.js";
import { briefLayout } from "./briefdata.js";
import { chartSpecFromTable, columnIsNumeric, countMarkdownTables, downloadCsv, downloadServerCsv, extractTable, sortedIndices } from "./tabledata.js";
import { comparableTable, compareSpec } from "./compare.js";

function codeText(children) {
  return Array.isArray(children) ? children.join("") : String(children ?? "");
}

const MAX_COMPARE = 4; // palette has 6 colors; keep the chart legible

// Only follow http(s)/mailto links out of a table cell — react-markdown already
// sanitizes hrefs into the hast (no rehype-raw), but re-checking is cheap
// belt-and-suspenders on model-authored content.
function safeHref(href) {
  try {
    const u = new URL(String(href || ""), window.location.origin);
    return /^(https?|mailto):$/.test(u.protocol) ? href : null;
  } catch {
    return null;
  }
}

// Minimal inline-markdown renderer over the hast a react-markdown cell holds:
// text plus the inline subset that actually shows up in a data cell (bold,
// italic, code, strike, links, breaks). It re-renders the ALREADY-PARSED nodes
// — it does not re-parse markdown — so extraction to text for sort/CSV stays the
// source of truth while the display keeps a link clickable and a total bold.
// An unknown/other inline node degrades to just its text (via its children).
function renderInline(node, key) {
  if (!node) return null;
  if (node.type === "text") return node.value || "";
  const kids = (node.children || []).map((c, i) => renderInline(c, i));
  switch (node.tagName) {
    case "strong": case "b": return <strong key={key}>{kids}</strong>;
    case "em": case "i": return <em key={key}>{kids}</em>;
    case "code": return <code key={key}>{kids}</code>;
    case "del": case "s": return <del key={key}>{kids}</del>;
    case "br": return <br key={key} />;
    case "a": {
      const href = safeHref(node.properties?.href);
      return href
        ? <a key={key} href={href} target="_blank" rel="noreferrer">{kids}</a>
        : <React.Fragment key={key}>{kids}</React.Fragment>;
    }
    default: return <React.Fragment key={key}>{kids}</React.Fragment>;
  }
}

// A whole <td> hast node → its inline children rendered (or the plain text
// fallback when no node is available, e.g. a defensively-empty parse).
const renderCell = (tdNode, text) =>
  (tdNode?.children ? tdNode.children.map((c, i) => renderInline(c, i)) : text);

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
// indicators. Sorting is DISPLAY-ONLY — a row-index permutation from
// sortedIndices, keyed off the visible cell TEXT; it never refetches and never
// changes the CSV (which re-runs the full query server-side). The SAME
// permutation reorders the parallel `cellNodes`, so each cell RENDERS through
// the minimal inline renderer (a link stays clickable, a total stays bold)
// while sort/CSV/compare keep using the extracted text. Compare mode's leading
// checkbox is rendered inline (selection keyed by the entity LABEL, so it
// survives a re-sort); that's why the old CompareContext/tr-override is gone.
function SortableTable({ headers, rows, cellNodes, cmp, selected, toggle, label }) {
  const [sort, setSort] = useState({ col: null, dir: null });
  const numericByCol = useMemo(
    () => headers.map((_, c) => columnIsNumeric(rows, c)), [headers, rows]);
  const order = useMemo(
    () => sortedIndices(rows, sort.col, sort.dir, numericByCol[sort.col]),
    [rows, sort, numericByCol]);

  // A "more below" cue: the box is scroll-capped, so a table cut exactly at the
  // boundary can look complete — flag when it's actually scrolled up from the end.
  const wrapRef = useRef(null);
  const [moreBelow, setMoreBelow] = useState(false);
  const onScroll = () => {
    const el = wrapRef.current;
    if (el) setMoreBelow(el.scrollTop + el.clientHeight < el.scrollHeight - 1);
  };
  useEffect(() => { onScroll(); }, [order]); // recompute after a re-sort/mount

  // asc → desc → back to the original (query) order.
  const clickHeader = (c) => setSort((s) => (
    s.col !== c ? { col: c, dir: "asc" }
      : s.dir === "asc" ? { col: c, dir: "desc" }
        : { col: null, dir: null }));

  const comparable = !!cmp;
  const entityCol = cmp?.entityCol ?? -1;

  return (
    <>
    <div className={"table-wrap" + (moreBelow ? " more-below" : "")}
         tabIndex={0} role="region" aria-label={label} ref={wrapRef} onScroll={onScroll}>
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
          {order.map((ri) => {
            const r = rows[ri];
            const nodes = cellNodes?.[ri];
            const rowLabel = comparable ? String(r[entityCol] ?? "") : "";
            const checked = comparable && selected.has(rowLabel);
            const atCap = comparable && !checked && selected.size >= MAX_COMPARE;
            return (
              <tr key={ri} className={comparable && checked ? "cmp-row picked" : undefined}>
                {comparable && (
                  <td className="cmp-cell">
                    {/* aria-disabled (not native disabled) so a capped checkbox
                        stays focusable and its title explains WHY — the toggle
                        no-ops past the cap regardless. */}
                    <input type="checkbox" checked={checked}
                           aria-disabled={atCap ? "true" : undefined}
                           title={atCap ? "Clear a selection to compare a different row (max 4)" : undefined}
                           aria-label={`Compare ${rowLabel}`}
                           onChange={() => { if (!atCap) toggle(rowLabel); }} />
                  </td>
                )}
                {r.map((cell, c) => <td key={c}>{renderCell(nodes?.[c], cell)}</td>)}
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
        Sorted the {rows.length} rows shown here — download the CSV for the full result.
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
  const { headers, rows, cellNodes } = useMemo(() => extractTable(node), [node]);
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
      <SortableTable headers={headers} rows={rows} cellNodes={cellNodes} cmp={cmp}
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
