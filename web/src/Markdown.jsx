import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// GFM gives us tables, which the analyst answers rely on. Tables are wrapped in
// a horizontally scrollable container so wide results never break the layout.
export default function Markdown({ children }) {
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
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
