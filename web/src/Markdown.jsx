import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import Chart from "./Chart.jsx";
import { normalizeMarkdown } from "./mdnorm.js";

function codeText(children) {
  return Array.isArray(children) ? children.join("") : String(children ?? "");
}

// GFM gives us tables, which the analyst answers rely on. Tables are wrapped in
// a horizontally scrollable container so wide results never break the layout.
// The source is first run through normalizeMarkdown to repair malformed tables
// (e.g. a header/delimiter column-count mismatch) that GFM would otherwise drop.
// A ```chart fenced block (compact JSON spec) is rendered as a Recharts figure;
// bad JSON falls back to showing the raw code block.
export default function Markdown({ children }) {
  const src = typeof children === "string" ? normalizeMarkdown(children) : children;
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          table: (props) => (
            <div className="table-wrap" tabIndex={0} role="region" aria-label="Result table">
              <table {...props} />
            </div>
          ),
          th: (props) => <th {...props} scope="col" />,
          a: (props) => <a {...props} target="_blank" rel="noreferrer" />,
          pre: (props) => {
            const child = props.children;
            const cn = child?.props?.className || "";
            if (/\blanguage-chart\b/.test(cn)) {
              try {
                const spec = JSON.parse(codeText(child.props.children).trim());
                return <Chart spec={spec} />;
              } catch { /* malformed spec — fall through to raw code */ }
            }
            return <pre {...props} />;
          },
        }}
      >
        {src}
      </ReactMarkdown>
    </div>
  );
}
