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

import asyncio
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

# How many MCP tool calls may occupy the shared worker-thread pool at once.
#
# `run_in_threadpool` is the PROCESS-WIDE anyio limiter (40 threads by default),
# the same one every synchronous FastAPI route runs in, and the rate limiter in
# app/ratelimit.py bounds requests STARTED per minute (60), not requests in
# flight. Measured before this existed: 55 concurrent `run_sql` calls from one
# valid key — no 429, since 55 < 60 — took `/api/health` from 0.01s to 22.28s,
# and `/api/health` is what the container's healthcheck polls. No crafted query
# is needed; `SELECT * FROM c_a ORDER BY ctotalt DESC` runs 7s and spills ~0.7GB
# of SQLite temp per call.
#
# 8 of 40 leaves the web app four fifths of the pool no matter what MCP is
# doing, and still lets a handful of clients query in parallel. A module
# constant rather than a setting: it is a property of the pool it shares, not
# something a deployment tunes (so `scripts/ci_env.sh` needs no entry).
#
# This bounds the DATA tools. `ask` is bounded elsewhere and differently — it
# holds no pool thread of its own (it awaits the agent loop, which hops per
# blocking step) and is capped per user by the chat rate limiter it charges.
# The semaphore itself is built per lifespan, in `start_mcp`, NOT here: an
# asyncio primitive caches the running loop the first time it is awaited and
# refuses a second one, and this repo's tests stand up dozens of TestClients,
# each with its own loop. Same reason the transport app is built there.
MCP_TOOL_CONCURRENCY = 8

# How long a tool call may WAIT for one of those slots before it is refused.
#
# The semaphore bounds how much work runs at once; on its own it does not bound
# how much is QUEUED. The rate limiter counts requests started per minute, not
# requests pending, so one key holder can put 60/min against a drain rate of 8
# per (up to) `sql_timeout_seconds` — every waiter holding a socket and a task,
# and uvicorn does not cancel an ASGI task when the client hangs up, so a script
# with a 1-second client timeout keeps adding work nobody is waiting for.
#
# Refusing is better than queueing: a client gets an answer it can act on, and
# the per-key rate limit goes back to meaning something. The wait is slightly
# longer than one full SQL timeout, so a call that queues behind a legitimately
# slow query still gets its turn rather than being refused for someone else's
# work.
MCP_TOOL_WAIT_SECONDS = 30.0

# The refusal itself. Shaped like every other tool error (see ERROR_PREFIXES) so
# a client renders it as a failed call with a reason, not as an answer.
BUSY_MESSAGE = ("ERROR server busy: too many tool calls are already running. "
                "Retry in a few seconds.")

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
    # Bounded, so MCP traffic cannot starve chat and the web UI of the shared
    # pool — see MCP_TOOL_CONCURRENCY. Acquired around the hop, not inside
    # `_call`, because it is the THREAD that is scarce.
    #
    # With a WAIT BOUND, so the queue in front of those slots is bounded too:
    # `async with` on its own waits forever, which turns a burst into an
    # unbounded backlog of held sockets and tasks. See MCP_TOOL_WAIT_SECONDS.
    slots = _current["slots"]
    try:
        await asyncio.wait_for(slots.acquire(), MCP_TOOL_WAIT_SECONDS)
    except TimeoutError:  # asyncio.TimeoutError is an alias of it on 3.11+
        log.warning("MCP tool %s refused: no slot within %.0fs",
                    params.name, MCP_TOOL_WAIT_SECONDS)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=BUSY_MESSAGE)],
            is_error=True)
    try:
        return await run_in_threadpool(_call, params.name, arguments)
    finally:
        slots.release()


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
_current: dict[str, Any] = {"app": None, "slots": None}


class _Endpoint:
    """The route registered at import time. Delegates to whatever the current
    lifespan built, and answers 503 when nothing has — which is what a bare
    `TestClient(app)` (no context manager, so no lifespan) gets, rather than an
    AttributeError three frames into the SDK.

    POST ONLY. The route has to be registered as an any-method Route (a Mount
    does not match a bare `/mcp` — see app/main.py), and the SDK's transport
    answers a GET by opening an SSE stream that stays open until the client goes
    away. This server never pushes anything: it is stateless, issues no session
    id, and sends no notifications, so that stream carries nothing and is only a
    held socket, a task in the manager's group, and its buffers. The rate
    limiter charges one unit when the stream OPENS and never sees the hold, so a
    key holder could park 60 of them a minute, indefinitely. Measured: 40
    concurrent GETs from one key, all 200, all still open after 20s.

    Refusing here also makes the code agree with what three documents already
    say — the transport comment below, `docs/MCP.md`, and
    `docs/AUTH_AND_SECURITY.md` all describe a POST-only endpoint.
    """

    async def __call__(self, scope, receive, send):
        app = _current["app"]
        if app is None:
            await JSONResponse(
                {"detail": "The MCP endpoint is not running."},
                status_code=503)(scope, receive, send)
            return
        if scope.get("method") != "POST":
            # Before the bearer gate on purpose: which methods exist is not a
            # secret, and an unauthenticated GET should not open a stream just
            # to have its key rejected afterwards.
            await JSONResponse(
                {"detail": "The MCP endpoint accepts POST only."},
                status_code=405, headers={"Allow": "POST"})(scope, receive, send)
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
    # Saved and RESTORED rather than blanked, because these lifespans nest:
    # `backend/tests/test_api_keys.py` opens a second `TestClient(app)` inside an
    # outer one, and an unconditional `= None` on the inner exit would leave the
    # outer client's `/mcp` answering 503 "not running" for the rest of the file
    # — pointing at exactly the wrong cause.
    prev_app, prev_slots = _current["app"], _current["slots"]
    _current["app"] = RequireApiKey(transport)
    # Built here, in the running loop, for the reason at MCP_TOOL_CONCURRENCY.
    _current["slots"] = asyncio.Semaphore(MCP_TOOL_CONCURRENCY)
    try:
        async with server.session_manager.run():
            log.info("MCP endpoint ready at %s", MCP_PATH)
            yield
    finally:
        _current["app"], _current["slots"] = prev_app, prev_slots
