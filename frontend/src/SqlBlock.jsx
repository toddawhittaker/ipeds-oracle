import React, { useMemo } from "react";
import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import { format } from "sql-formatter";

// Register only the SQL grammar (PrismLight ships none by default) so the
// bundle carries one language, not Prism's whole catalog.
SyntaxHighlighter.registerLanguage("sql", sql);

// Pretty-print the model's SQL so a one-line query becomes a readable, indented
// block instead of a horizontally-scrolling ribbon. Falls back to the raw text
// if the formatter can't parse it (e.g. a partial stream), so we never hide the
// query. `sqlite` matches the app's read-only dataset dialect.
function prettySql(code) {
  try {
    return format(code || "", { language: "sqlite", keywordCase: "upper" });
  } catch {
    return code || "";
  }
}

// A formatted, syntax-highlighted SQL block. `useInlineStyles={false}` makes
// react-syntax-highlighter emit Prism token *class names* (no inline style
// attributes) which styles.css colors per theme (.sqlblock .token.*) — so the
// highlighting tracks light/dark and needs no CSP style exception of its own.
export default function SqlBlock({ code, className = "" }) {
  const pretty = useMemo(() => prettySql(code), [code]);
  return (
    <SyntaxHighlighter
      language="sql"
      useInlineStyles={false}
      PreTag="pre"
      className={`sqlblock ${className}`.trim()}
    >
      {pretty}
    </SyntaxHighlighter>
  );
}
