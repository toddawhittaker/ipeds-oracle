"""Tool definitions exposed to the LLM (OpenAI-compatible function-calling
format) and a dispatcher that runs them. These are embedded tools — same
functions can later be re-exported over MCP without change.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.tools import schema as sch
from app.tools.sql import SQLTimeoutError, SQLValidationError, run_sql


def _tool_run_sql(sql: str, sink: dict[str, Any] | None = None) -> str:
    try:
        r = run_sql(sql)
    except SQLValidationError as e:
        return f"SQL REJECTED: {e}"
    except SQLTimeoutError as e:
        return f"SQL TIMEOUT: {e}"
    except Exception as e:  # surface the sqlite error text to the model
        return f"SQL ERROR: {type(e).__name__}: {e}"
    # Hand the exact result back to the caller (per-request), so the API layer
    # can offer a CSV of the data behind the answer. No shared module state — a
    # global here would let concurrent chat turns clobber each other's result.
    #
    # "result" is the LAST result (the long-standing contract, pinned by
    # test_result_isolation.py); "results" ACCUMULATES every result of the turn,
    # in call order. The list is what app/grounding.py checks a figure against —
    # a brief runs several queries (the recent-years table plus the rank/share
    # query prompt step 6(a) invites), and overwriting left the server unable to
    # verify a headline number against the query that actually produced it.
    if sink is not None:
        sink["result"] = r
        sink.setdefault("results", []).append(r)
    header = f"OK — {r.row_count} row(s)" + (" (truncated)" if r.truncated else "")
    notes = ("\n" + " ".join(r.notes)) if r.notes else ""
    return f"{header}{notes}\n\n{r.to_markdown(max_rows=200)}"


# name -> (python callable, JSON schema for the model)
_TOOLS: dict[str, tuple[Callable[..., str], dict[str, Any]]] = {
    "run_sql": (_tool_run_sql, {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": "Execute ONE read-only SQLite SELECT/WITH query against "
                           "ipeds.db and return the result rows as a Markdown table. "
                           "Runs read-only with a hard timeout and row cap.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string",
                            "description": "A single SELECT or WITH statement. No "
                                           "semicolons, no DDL/DML/PRAGMA."}},
                "required": ["sql"],
            },
        },
    }),
    "list_families": (lambda: sch.list_families(), {
        "type": "function",
        "function": {
            "name": "list_families",
            "description": "List all data families (unified tables) with row counts "
                           "and the years each covers.",
            "parameters": {"type": "object", "properties": {}},
        },
    }),
    "get_columns": (lambda family: sch.get_columns(family), {
        "type": "function",
        "function": {
            "name": "get_columns",
            "description": "List the column names of a family (e.g. 'c_a', 'hd').",
            "parameters": {
                "type": "object",
                "properties": {"family": {"type": "string"}},
                "required": ["family"],
            },
        },
    }),
    "describe_variables": (lambda family, keyword=None: sch.describe_variables(family, keyword), {
        "type": "function",
        "function": {
            "name": "describe_variables",
            "description": "Human-readable titles for a family's variables, optionally "
                           "filtered by a keyword (from the IPEDS data dictionary).",
            "parameters": {
                "type": "object",
                "properties": {
                    "family": {"type": "string"},
                    "keyword": {"type": "string"}},
                "required": ["family"],
            },
        },
    }),
    "lookup_code": (lambda varname, value=None: sch.lookup_code(varname, value), {
        "type": "function",
        "function": {
            "name": "lookup_code",
            "description": "Code→label for a categorical variable (e.g. AWLEVEL, "
                           "CONTROL, SECTOR). Omit value to list all codes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "varname": {"type": "string"},
                    "value": {"type": "string"}},
                "required": ["varname"],
            },
        },
    }),
    "find_variable": (lambda keyword: sch.find_variable(keyword), {
        "type": "function",
        "function": {
            "name": "find_variable",
            "description": "Search all IPEDS variables by keyword to find the right "
                           "table/column (e.g. 'tuition', 'retention').",
            "parameters": {
                "type": "object",
                "properties": {"keyword": {"type": "string"}},
                "required": ["keyword"],
            },
        },
    }),
    "find_cip": (lambda keyword: sch.find_cip(keyword), {
        "type": "function",
        "function": {
            "name": "find_cip",
            "description": "Look up CIP program codes by program name (e.g. 'nursing', "
                           "'computer science'). Returns codevalue → label.",
            "parameters": {
                "type": "object",
                "properties": {"keyword": {"type": "string"}},
                "required": ["keyword"],
            },
        },
    }),
}


def tool_specs() -> list[dict[str, Any]]:
    """The tools array to send to the model."""
    return [spec for _, spec in _TOOLS.values()]


def dispatch(name: str, arguments: str | dict,
             result_sink: dict[str, Any] | None = None) -> str:
    """Run a tool call and return its string result. For run_sql, the caller may
    pass a per-request `result_sink` dict to receive the QueryResult — under
    "result" (the last one; used for the CSV download) and appended to "results"
    (every result of the turn; used for figure grounding). Other tools ignore
    it."""
    entry = _TOOLS.get(name)
    if entry is None:
        return f"ERROR: unknown tool '{name}'."
    fn, _ = entry
    try:
        args = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
        if name == "run_sql":
            return fn(sink=result_sink, **args)
        return fn(**args)
    except json.JSONDecodeError as e:
        return f"ERROR: could not parse tool arguments: {e}"
    except TypeError as e:
        return f"ERROR calling {name}: {e}"
