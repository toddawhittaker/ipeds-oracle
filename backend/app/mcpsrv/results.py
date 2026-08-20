"""The structured half of a `run_sql` tool result.

MCP has a first-class channel for this that the chat agent's OpenAI-style
function calling does not: a tool result carries `content` (the Markdown table,
for a model to read) *and* `structured_content` (real columns and rows, for the
caller's own code), with `output_schema` on the tool as the published contract
between them.

The point is that an MCP caller should never have to parse the Markdown table
back into numbers. That parse is exactly where a digit gets lost.

The payload is built from `QueryResult.to_storage()` rather than from a second
serializer, so the rows an MCP client sees are byte-for-byte the rows the chat
path persists and grounds figures against.
"""
from __future__ import annotations

from typing import Any

from app.tools.sql import QueryResult

# Published as the `run_sql` tool's output_schema. The server does not validate
# structured_content against this, but SDK clients do — so declaring it is a
# promise that has to be kept by _structured() below, not decoration.
RUN_SQL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "columns": {"type": "array", "items": {"type": "string"},
                    "description": "Column names, in result order."},
        "rows": {"type": "array", "items": {"type": "array"},
                 "description": "Result rows, each a list of cell values "
                                "positionally matching `columns`."},
        "row_count": {"type": "integer",
                      "description": "How many rows are in `rows`. When "
                                     "`truncated` is true the query matched MORE "
                                     "than this and the true total is NOT known "
                                     "— the reader stops one row past the cap, "
                                     "so counting the rest would mean running "
                                     "the query again. Re-query with COUNT(*) "
                                     "if the total is what you need."},
        "truncated": {"type": "boolean",
                      "description": "True when the row cap cut the result. An "
                                     "aggregate computed over a truncated "
                                     "result is wrong — re-query with the "
                                     "aggregation in SQL instead."},
        "sql": {"type": "string", "description": "The SQL that ran."},
        "notes": {"type": "array", "items": {"type": "string"},
                  "description": "Query-shape warnings from the SQL linter "
                                 "(app/tools/sqllint.py), e.g. an award-level "
                                 "rollup that double-counts."},
    },
    "required": ["columns", "rows", "row_count", "truncated", "sql", "notes"],
}


def structured_result(result: QueryResult) -> dict[str, Any]:
    """`result` as the JSON object RUN_SQL_OUTPUT_SCHEMA describes.

    `to_storage` carries columns, rows and truncation; the three fields it drops
    (they are model-facing prose the chat path never reloads) are re-added here,
    because an MCP caller reading rows programmatically needs all three:
    `row_count` to size what it is holding, `sql` to know what it is, and `notes`
    because a sqllint warning is the difference between a right number and a
    confidently wrong one.

    `row_count` IS `len(rows)`, always, and the schema now says so. It used to
    promise the opposite — "larger than len(rows) when truncated is true" — which
    `QueryResult` cannot deliver: `tools/sql.py` slices to the cap BEFORE
    counting, and the cursor stops one row past it, so the true total was never
    read. A caller reading 200 and reporting "200 institutions" was being told by
    the published schema that 200 was the total. `truncated` is the field that
    says there were more; nothing here can say how many.
    """
    payload = result.to_storage()
    payload["row_count"] = result.row_count
    payload["truncated"] = bool(payload.get("truncated"))
    payload["sql"] = result.sql
    payload["notes"] = list(result.notes)
    return payload
