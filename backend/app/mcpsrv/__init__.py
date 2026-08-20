"""The MCP (Model Context Protocol) server, mounted at POST /mcp.

Named `mcpsrv` rather than `mcp` on purpose: a local package sharing a name with
the third-party dependency it imports is one accidental relative import away
from a confusing failure.

An MCP client (Claude Desktop, an editor, a script) presents a static bearer key
minted by app/apikeys.py and gets the same tools the chat agent uses — the same
`app/tools/registry.py` specs, dispatched through the same `registry.dispatch`,
so there is exactly one definition of what a tool is and what it does. It also
gets `ask`, which is the whole agent behind one tool call for a client that
wants an answer rather than the primitives to build one.

  * `server.py`  — the low-level MCP server, its handlers, and the lifespan
  * `auth.py`    — the bearer-key gate in front of it, and who it admitted
  * `ask.py`     — the agent loop as a single stateless tool
  * `results.py` — the structured half of a `run_sql` result
  * `resources.py` — docs/SCHEMA.md and docs/DATASET.md, as MCP resources
"""
from __future__ import annotations

from app.mcpsrv.server import MCP_PATH, endpoint, start_mcp

__all__ = ["MCP_PATH", "endpoint", "start_mcp"]
