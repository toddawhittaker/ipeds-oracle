"""The MCP server itself: its handlers, its transport, and its lifespan.

WHY THE LOW-LEVEL `Server` AND NOT THE HIGH-LEVEL `MCPServer`. The high-level
tier takes a Python callable per tool and derives the JSON Schema from its type
annotations, which would mean re-declaring all seven signatures here and letting
them drift from `app/tools/registry.py`. The low-level tier takes handlers and
lets us supply the schema, so `registry.tool_specs()` stays the single source of
truth for both the chat agent loop and MCP. The adaptation is a field rename:
OpenAI's `{"function": {"name", "description", "parameters"}}` becomes MCP's
`Tool(name=…, description=…, input_schema=…)`.

TWO THINGS THE LOW-LEVEL TIER DOES NOT DO FOR YOU. It does not validate
arguments against the advertised schema, and an exception out of a handler
becomes a JSON-RPC protocol error rather than a tool result — a failed CALL
instead of a failed TOOL, which a client can neither read nor retry.

`registry.dispatch` covers the first and MOST of the second: it returns
`"ERROR: …"` / `"SQL REJECTED: …"` strings instead of raising. But only for
`run_sql`, which catches what the database throws. The six lookup tools call
straight through to `app/tools/schema.py`, where a missing table arrives as a
bare `sqlite3.OperationalError` — so `_call` catches everything as well. This
was found by running the suite against the CI fixture database, not by reading;
the plan for this step assumed dispatch covered it.

NO BLOCKING WORK RUNS ON THE EVENT LOOP. This is not optional at this tier. The
high-level `MCPServer` hops sync tool functions onto a worker thread for you;
low-level handlers are awaited directly on the event loop, and the tool
functions use blocking `sqlite3`. An unwrapped handler stalls every live chat
stream in the process for the length of the query. `run_in_threadpool` is the
same call the agent loop already makes (app/llm.py), so MCP traffic and chat
traffic share one anyio thread limiter — which also means llm.py's note about a
burst of heavy queries saturating that pool now covers this path too.

The `ask` tool is the one handler that stays on the loop, and for the same
reason rather than in spite of it: it awaits the agent loop, which is an async
generator that already hands each of its own blocking steps to the pool. Putting
it in a worker thread would mean running an event loop inside one. See
`app/mcpsrv/ask.py`.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import mcp.types as types
from mcp.server.lowlevel.server import DEFAULT_MAX_REQUEST_BODY_SIZE, Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.config import PRODUCT_NAME, get_settings
from app.mcpsrv import ask, resources
from app.mcpsrv.auth import RequireApiKey
from app.mcpsrv.results import RUN_SQL_OUTPUT_SCHEMA, structured_result
from app.tools import registry

log = logging.getLogger("ipeds.mcp")

MCP_PATH = "/mcp"

# Prefixes `registry.dispatch` returns instead of raising. They are the tool's
# way of saying "your call was wrong, here is why" — surfaced with is_error so a
# client renders them as a failed call rather than as an answer.
ERROR_PREFIXES = ("ERROR", "SQL REJECTED", "SQL TIMEOUT", "SQL TOO LARGE", "SQL ERROR")

INSTRUCTIONS = (
    "Query the IPEDS higher-education dataset. For a written answer, call `ask` "
    "with a plain-language question and the deployment's own agent will do the "
    "work. To drive the queries yourself, read the `schema-guide` resource first "
    "— it carries the aggregation rules (award-level nesting, CIP rollups) that "
    "silently produce wrong totals — then use `run_sql` for data and the lookup "
    "tools to find the right table, column, or code."
)


def _tools() -> list[types.Tool]:
    """`registry.tool_specs()`, renamed into MCP's shape, plus `ask`. No second
    list of the data tools.

    `ask` is appended HERE rather than added to the registry, and that is the
    whole reason it is safe: the registry is what the chat agent's own tool loop
    is handed, so a tool declared there would let the agent call the agent.
    """
    out = []
    for spec in registry.tool_specs():
        fn = spec["function"]
        # run_sql is the only tool with a machine-readable result, so it is the
        # only one that publishes an output schema.
        out.append(types.Tool(
            name=fn["name"], description=fn["description"],
            input_schema=fn["parameters"],
            output_schema=RUN_SQL_OUTPUT_SCHEMA if fn["name"] == "run_sql" else None))
    out.append(ask.TOOL)
    return out


def _tool_error(name: str, e: Exception) -> types.CallToolResult:
    """An escaped exception, as a failed TOOL rather than a failed CALL.

    One definition for both handlers below. At this tier an exception out of a
    handler becomes a JSON-RPC protocol error, which a client can neither read,
    retry, nor show its user — so nothing may escape, and both paths have to say
    so the same way.
    """
    log.warning("MCP tool %s failed: %s", name, e)
    return types.CallToolResult(
        content=[types.TextContent(type="text",
                                   text=f"ERROR calling {name}: {type(e).__name__}: {e}")],
        is_error=True)


def _call(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    """One tool call, start to finish. Runs on a worker thread (see the module
    docstring), so everything in here may block.

    `registry.dispatch` returns its own refusals as text, but only `run_sql`
    catches what the database throws — the six lookup tools call straight into
    `app/tools/schema.py` and a missing table comes out as a bare
    `sqlite3.OperationalError`. At this tier an escaping exception becomes a
    JSON-RPC internal error, i.e. a failed CALL rather than a failed TOOL, which
    a client cannot read, retry, or show its user. So everything is caught here
    and answered as a tool error, on the same terms as dispatch's own.
    """
    sink: dict[str, Any] = {}
    try:
        text = registry.dispatch(name, arguments, result_sink=sink)
    except Exception as e:  # noqa: BLE001 -- a tool failure is a RESULT, not a crash
        return _tool_error(name, e)
    is_error = text.startswith(ERROR_PREFIXES)
    structured = None
    result = sink.get("result")
    if result is not None and not is_error:
        structured = structured_result(result)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                structured_content=structured, is_error=is_error)


async def on_list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=_tools())


async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    arguments = params.arguments or {}
    if params.name == ask.TOOL_NAME:
        # `ask` runs on the event loop, not in the pool the data tools use: it
        # awaits the agent loop, an async generator that does its own thread
        # hops for every blocking step. (The plan for this step said `ask` would
        # go through `_call` and inherit its try/except; it cannot — `_call` is
        # synchronous. It shares the error SHAPE instead, via `_tool_error`,
        # which is what that instruction was actually protecting.)
        try:
            return await ask.run_ask(arguments)
        except Exception as e:  # noqa: BLE001 -- a tool failure is a RESULT, not a crash
            return _tool_error(params.name, e)
    return await run_in_threadpool(_call, params.name, arguments)


async def on_list_resources(ctx, params) -> types.ListResourcesResult:
    return types.ListResourcesResult(resources=[
        types.Resource(uri=uri, name=name, title=title, description=description,
                       mime_type="text/markdown")
        for uri, (name, title, description) in resources.CATALOG.items()])


async def on_read_resource(ctx, params: types.ReadResourceRequestParams,
                           ) -> types.ReadResourceResult:
    uri = str(params.uri)
    text = await run_in_threadpool(resources.read_resource, uri)
    return types.ReadResourceResult(contents=[
        types.TextResourceContents(uri=params.uri, mime_type="text/markdown", text=text)])


def build_server() -> Server:
    return Server(
        name=PRODUCT_NAME,
        version=get_settings().app_version,
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
    )


def _transport_security() -> TransportSecuritySettings:
    """DNS-rebinding protection, explicitly OFF.

    Passing this at all is the load-bearing part. `streamable_http_app()`
    defaults `host="127.0.0.1"`, and for a loopback host it AUTO-ENABLES
    rebinding protection with `allowed_hosts=["127.0.0.1:*", "localhost:*",
    "[::1]:*"]`. Behind the reverse proxy, which forwards the deployment's real
    `Host:`, that answers 421 to every request — and it works perfectly on
    localhost, so no local run and no test on the default host would ever catch
    it. Leaving this argument off is the silent production outage.

    Off rather than an allowlist built from APP_PUBLIC_URL, which is what the
    plan for this step proposed, because the protection buys nothing here and
    the allowlist has a real failure mode. A DNS-rebinding attack works by
    getting a victim's browser to send a request WITH ITS AMBIENT CREDENTIALS to
    a server that trusts them; this endpoint has no ambient credentials to
    borrow — every call needs an `Authorization: Bearer` header the attacker's
    page cannot produce, so the rebound request gets a 401 like any other
    anonymous one. Meanwhile a host allowlist 421s any deployment reached on a
    hostname that does not exactly match APP_PUBLIC_URL, port and all, and 421
    is not a diagnosable error from a client's side.
    """
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def _body_cap() -> int:
    """Keep the transport's body cap in step with the app-wide one, so the two
    do not disagree about how big a request may be. A non-positive
    `max_request_body_mb` disables app/bodylimit.py's cap; the transport has no
    "off", so it keeps the SDK's own default there."""
    mb = get_settings().max_request_body_mb
    return mb * 1024 * 1024 if mb > 0 else DEFAULT_MAX_REQUEST_BODY_SIZE


# The app the route delegates to, rebuilt by each `start_mcp()` and cleared on
# the way out. It is NOT built at import time, and it cannot be: the transport's
# session manager raises if its `run()` is entered twice, while this repo's tests
# construct `TestClient(app)` dozens of times in one process. Building inside the
# lifespan gives each startup its own manager, which is both correct and the only
# thing the suite tolerates.
_current: dict[str, Any] = {"app": None}


class _Endpoint:
    """The route registered at import time. Delegates to whatever the current
    lifespan built, and answers 503 when nothing has — which is what a bare
    `TestClient(app)` (no context manager, so no lifespan) gets, rather than an
    AttributeError three frames into the SDK."""

    async def __call__(self, scope, receive, send):
        app = _current["app"]
        if app is None:
            await JSONResponse(
                {"detail": "The MCP endpoint is not running."},
                status_code=503)(scope, receive, send)
            return
        await app(scope, receive, send)


endpoint = _Endpoint()


@asynccontextmanager
async def start_mcp():
    """Build the MCP app and run its session manager for one process lifetime.

    Entered from the parent app's lifespan (app/main.py). Starlette does not run
    an adopted sub-app's lifespan, so without this the session manager never
    starts and the first request dies inside the SDK with "Task group is not
    initialized".
    """
    server = build_server()
    transport = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        # A POST-only server with no server-side session state and plain JSON
        # responses: best behaved behind a proxy, and what makes the endpoint
        # reachable from the synchronous TestClient the rest of the suite uses.
        # `stateless_http` only affects the legacy protocol leg — the modern one
        # is sessionless by construction — but we do not control which era a
        # client picks, so both are set.
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security(),
        max_request_body_size=_body_cap(),
    )
    _current["app"] = RequireApiKey(transport)
    try:
        async with server.session_manager.run():
            log.info("MCP endpoint ready at %s", MCP_PATH)
            yield
    finally:
        _current["app"] = None
