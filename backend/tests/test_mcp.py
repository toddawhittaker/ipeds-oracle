"""The MCP endpoint's contract (backend/app/mcpsrv/ + the /mcp route).

Every check below names a regression with a plausible way of happening, and
most of them fail SILENTLY — which is why they are here rather than left to a
manual poke at the endpoint:

  * the route registered as a Mount instead of a Route. Starlette compiles a
    Mount's pattern as `path + "/{path:path}"`, so `/mcp` stops matching and
    falls through to the SPA catch-all: POST answers 405 and GET serves the
    React shell. Nothing errors; the endpoint simply is not there any more.
  * OAuth resource metadata coming back. Passing `auth=`/`token_verifier=` to
    the SDK adds a `/.well-known/oauth-protected-resource` route and a
    `WWW-Authenticate` header, and clients that see either have been reported
    to abandon a configured static key and go hunting for a login flow this
    deployment does not have.
  * DNS-rebinding protection switching itself on. `streamable_http_app()`
    auto-enables it for a loopback host — every request behind the reverse
    proxy would 421, and it works perfectly on localhost, so the failure is
    invisible until the first real deployment.
  * a rejected query arriving as a broken connection instead of a readable tool
    error. The low-level tier turns an exception into a protocol error; the
    contract is that `registry.dispatch` returns its refusal as a string.
  * `structured_content` going missing, leaving a caller to parse numbers back
    out of a Markdown table — which is exactly where a digit gets lost.
  * an unauthenticated, revoked, or de-allowlisted key getting through.

Both protocol legs are covered: omitting `MCP-Protocol-Version` routes to the
legacy path, and a real client picks its own era.

Deliberately NOT written against the SDK's in-memory `Client(server)` helper —
that connects straight to the server object and skips the HTTP layer, which is
where the bearer gate and the route registration live.

Uses the standalone-script style the rest of backend/tests/ uses: env before
import, a check() helper, non-zero exit on any failure.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

tmp = tempfile.mkdtemp()
os.environ["APP_DB_PATH"] = str(Path(tmp) / "app.db")
os.environ["ADMIN_EMAILS"] = "admin@example.edu"
os.environ["COOKIE_SECURE"] = "false"
os.environ["LLM_API_KEY"] = ""
os.environ["RESEND_API_KEY"] = ""
os.environ["AUTH_RATE_MAX_PER_EMAIL"] = "1000"
os.environ["AUTH_RATE_MAX_PER_IP"] = "1000"
# A SUITE-SIZE BUDGET, not a product number: low enough that the 429 case can
# reach the cap in a fraction of a second, high enough that the rest of the suite
# never brushes it on its way past. It therefore has to move when the suite
# grows — adding the four `ask` calls behind the chart checks put the tail of the
# file over 30, and the symptom is a pile of unrelated cases failing with
# "Too many requests" rather than on their own contract. The 429 case mints its
# OWN key so its spending is its own.
MCP_CAP = 50
os.environ["MCP_RATE_MAX_PER_KEY"] = str(MCP_CAP)
# `ask` charges the SAME per-user limiter the web chat charges, so this file has
# to pin it: config.py's default is 30, and a suite that quietly drifts past it
# would start failing on a call count rather than on a contract. The same
# suite-size budget as MCP_CAP above, and it moves for the same reason — low
# enough that the shared-budget case can exhaust it quickly, high enough that
# every other `ask` in the file never brushes it. That case spends a DIFFERENT
# user's budget anyway, because this limiter is keyed on the user.
CHAT_CAP = 20
os.environ["CHAT_RATE_MAX_PER_USER"] = str(CHAT_CAP)

from fastapi.testclient import TestClient  # noqa: E402

from app import mailer  # noqa: E402

captured = {}
mailer.send_magic_link = lambda to, link: captured.__setitem__("link", link) or True
mailer.send_access_request = lambda *a, **k: True
mailer.send_access_approved = lambda to: True

# The modern protocol revision this SDK serves. Read from the SDK rather than
# hardcoded, so a version bump surfaces as a real failure instead of this file
# quietly testing only the legacy leg forever.
from mcp.server.lowlevel.server import MODERN_PROTOCOL_VERSIONS  # noqa: E402

from app import guard, skills  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import connect  # noqa: E402
from app.llm import AgentResult  # noqa: E402
from app.main import app  # noqa: E402
from app.mcpsrv import ask, resources, server  # noqa: E402
from app.tools import registry  # noqa: E402
from app.tools.sql import QueryResult  # noqa: E402

PROTOCOL_VERSION = MODERN_PROTOCOL_VERSIONS[-1]
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
# tools/call, prompts/get and resources/read must repeat their named parameter
# in an Mcp-Name header, and the transport refuses a mismatch.
NAME_PARAM = {"tools/call": "name", "resources/read": "uri"}

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  ✓ {name}")
    except AssertionError as e:
        FAILURES.append(name)
        print(f"  ✗ {name}: {e}")


def mcp_post(c, method, params=None, key=None, modern=True, headers=None):
    """One JSON-RPC POST to /mcp, with the headers the transport requires.

    On the modern leg those headers are cross-checked against the body — the
    protocol version must equal `params._meta`'s, `Mcp-Method` the body's
    method, and `Mcp-Name` the named parameter — so they are derived here from
    the same values rather than passed in, which is the only way a caller can't
    accidentally test a mismatch it meant to be a match.
    """
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": dict(params or {})}
    h = {"Accept": "application/json, text/event-stream",
         "Content-Type": "application/json"}
    if key is not None:
        h["Authorization"] = f"Bearer {key}"
    if modern:
        body["params"].setdefault("_meta", {})
        body["params"]["_meta"][META_VERSION] = PROTOCOL_VERSION
        body["params"]["_meta"].setdefault(META_CAPS, {})
        h["MCP-Protocol-Version"] = PROTOCOL_VERSION
        h["Mcp-Method"] = method
        named = NAME_PARAM.get(method)
        if named and named in body["params"]:
            h["Mcp-Name"] = str(body["params"][named])
    h.update(headers or {})
    return c.post("/mcp", json=body, headers=h)


def result_of(response):
    """The JSON-RPC `result`, asserting the call was not an error envelope.

    Asserting on the body and not only the HTTP status is the point: a JSON-RPC
    error still travels, and reading `response.status_code == 200` alone would
    call a rejected request a success.
    """
    assert response.status_code == 200, f"HTTP {response.status_code}: {response.text}"
    payload = response.json()
    assert "error" not in payload, f"JSON-RPC error: {payload['error']}"
    return payload["result"]


def call_ask(c, question, key):
    """One `ask` tool call, returning the JSON-RPC result."""
    return result_of(mcp_post(c, "tools/call",
                              {"name": "ask", "arguments": {"question": question}},
                              key=key))


def fake_agent(answer="Ohio awarded **1,234** nursing degrees in 2023.",
               figure=None, sql="SELECT 1", grounding="exact", boom=False):
    """A stand-in for llm.stream_agent that yields the events a real turn does.

    The agent loop itself is pinned by test_agent_loop.py; what these checks are
    about is the PLUMBING around it — that a turn bills exactly one usage row,
    writes no conversation, and hands the figure and its grounding status back
    through MCP's structured channel. Driving a real turn would need a provider
    key CI does not have, and would test the model instead of the wiring.
    """
    async def gen(question, **kwargs):
        if boom:
            raise AssertionError("the agent ran when it should not have")
        # A real QueryResult, not an empty list: the cache check below asserts
        # that ask stores the rows behind an answer, and an agent that returned
        # none would satisfy that assertion by having nothing to lose.
        rows = QueryResult(columns=["stabbr", "n"], rows=[("OH", 1234)],
                           row_count=1, sql=sql)
        res = AgentResult(answer=answer, model_used="fake-model",
                          sql_log=[sql], figure=figure, results=[rows],
                          last_result=rows,
                          figure_grounding=grounding if figure else "no_figure",
                          prompt_tokens=100, completion_tokens=20, cost=0.001)
        yield {"type": "answer", "text": answer}
        yield {"type": "done", "result": res}
    return gen


def usage_rows(question):
    """Every usage_log row for `question`, newest last."""
    con = connect()
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM usage_log WHERE question=? ORDER BY id", (question,))]
    finally:
        con.close()


def table_count(table):
    con = connect()
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def sign_in(c, email):
    r = c.post("/api/auth/request", json={"email": email})
    assert r.status_code == 200, r.text
    token = captured["link"].split("token=")[1]
    v = c.post("/api/auth/verify", json={"token": token})
    assert v.status_code == 200, v.text


def run():
    with TestClient(app) as c:
        sign_in(c, "admin@example.edu")
        key = c.post("/api/keys", json={"label": "mcp"}).json()["key"]

        # --- the bearer gate -------------------------------------------------
        def rejects_without_a_key():
            r = mcp_post(c, "tools/list")
            assert r.status_code == 401, f"{r.status_code}: {r.text}"
            r = mcp_post(c, "tools/list", key="ipeds_mcp_not-a-real-key")
            assert r.status_code == 401, f"a bogus key got {r.status_code}"
            assert "ipeds" not in r.text.lower() or "detail" in r.text, r.text
        check("no key and a wrong key are both refused", rejects_without_a_key)

        def a_wrong_auth_scheme_is_a_clean_401():
            # A client configured for Basic auth (or any other scheme) must get
            # the same flat refusal as one with no header at all, rather than
            # having its credential parsed as a bearer token or blowing up
            # inside the gate.
            r = mcp_post(c, "tools/list", headers={"Authorization": "Basic Zm9vOmJhcg=="})
            assert r.status_code == 401, f"{r.status_code}: {r.text}"
        check("an Authorization header in another scheme is refused cleanly",
              a_wrong_auth_scheme_is_a_clean_401)

        def a_revoked_key_stops_working():
            minted = c.post("/api/keys", json={"label": "doomed"}).json()
            assert result_of(mcp_post(c, "tools/list", key=minted["key"]))
            assert c.delete(f"/api/keys/{minted['id']}").status_code == 200
            r = mcp_post(c, "tools/list", key=minted["key"])
            assert r.status_code == 401, f"a revoked key still worked ({r.status_code})"
        check("a revoked key stops working immediately", a_revoked_key_stops_working)

        def the_401_advertises_no_auth_scheme():
            # Clients that see OAuth resource metadata have been reported to
            # abandon a perfectly good configured static key and go hunting for
            # a login flow this deployment does not have. The way that comes
            # back is somebody replacing app/mcpsrv/auth.py's gate with the
            # SDK's own `token_verifier`, whose RequireAuthMiddleware stamps
            # WWW-Authenticate on its 401 — confirmed by making exactly that
            # swap and watching this check go red.
            #
            # There is deliberately no companion check that
            # /.well-known/oauth-protected-resource is unserved: those routes
            # live on the SDK's inner Starlette app, and the parent app routes
            # nothing to it but /mcp, so they are unreachable by construction
            # and a test for them could not fail.
            r = mcp_post(c, "tools/list")
            assert r.status_code == 401, r.status_code
            assert "www-authenticate" not in {k.lower() for k in r.headers}, \
                f"the 401 advertises an auth scheme: {dict(r.headers)}"
        check("the 401 advertises no auth scheme", the_401_advertises_no_auth_scheme)

        # --- routing: a Route, not a Mount -----------------------------------
        def get_mcp_is_the_endpoint_not_the_spa():
            r = c.get("/mcp")
            assert r.status_code != 404, "GET /mcp did not match the MCP route"
            assert "<div id=\"root\"" not in r.text and "<!doctype" not in r.text.lower(), \
                ("GET /mcp served the SPA shell — the route fell through to the "
                 "catch-all, which is what a Mount instead of a Route does")
            spa = c.get("/")
            assert spa.status_code == 200, spa.status_code
        check("GET /mcp reaches the MCP endpoint and GET / still serves the app",
              get_mcp_is_the_endpoint_not_the_spa)

        def only_post_is_served():
            """A GET used to reach the SDK's transport, which answers it by
            opening an SSE stream that lives until the client goes away. This
            server pushes nothing — stateless, no session id, no notifications —
            so the stream carries nothing, and the rate limiter charges it once
            when it OPENS and never sees the hold. Measured before this refusal
            existed: 40 concurrent GETs from one key, all 200, all still open
            after 20s. A key holder could park 60 sockets a minute for free.

            The route still has to accept every method to be registered at all
            (a Mount does not match a bare /mcp), so the refusal is ours to make.
            """
            for method in ("GET", "DELETE", "PUT"):
                r = c.request(method, "/mcp", headers={"Authorization": f"Bearer {key}"})
                assert r.status_code == 405, \
                    f"{method} /mcp answered {r.status_code}, not 405"
                assert r.headers.get("allow") == "POST", dict(r.headers)
            # And the refusal does not depend on the key: an anonymous GET must
            # not open a stream just to have its credential rejected afterwards.
            assert c.get("/mcp").status_code == 405, "an anonymous GET was not refused"
        check("every method but POST is refused with 405", only_post_is_served)

        def tool_calls_cannot_flood_the_shared_worker_pool():
            """The regression, measured on a running instance before the bound
            existed: 55 concurrent `run_sql` calls from ONE key — no 429, since
            55 is inside the 60/minute ceiling — took an unauthenticated
            `/api/health` from 0.01s to 22.28s. `run_in_threadpool` is the
            process-wide 40-thread pool every synchronous route shares, the rate
            limiter counts requests STARTED per minute rather than requests in
            flight, and no crafted query is needed: `SELECT * FROM c_a ORDER BY
            ctotalt DESC` runs 7s and spills ~0.7GB of temp per call.

            Driven at the HANDLER, not over HTTP, and that is deliberate. The
            first version of this check fired 20 concurrent POSTs through
            TestClient and passed with the semaphore DELETED: something in that
            stack — portal, transport, or client — never let more than 8 calls
            run at once anyway, so the check could not see a flood and was
            measuring the harness. `on_call_tool` is where the bound lives, so
            that is where it is observed, with a semaphore built in this loop
            (an asyncio primitive refuses a second event loop once used, which
            is why the real one is per-lifespan too).

            Two-sided on purpose: `high <= LIMIT` alone is satisfied by breaking
            parallelism altogether, which would be a different bug that still
            passed. So it also asserts the bound is REACHED.
            """
            import asyncio as aio
            import threading

            limit = 4                     # smaller than the shipped 8, so the
            calls = limit * 3             # ambient pool can never be the cap
            lock = threading.Lock()
            state = {"cur": 0, "high": 0}
            at_limit = threading.Event()
            released = threading.Event()
            real_dispatch = registry.dispatch

            class _Params:                # what on_call_tool actually reads
                name, arguments = "list_families", {}

            def blocking_dispatch(*a, **k):
                with lock:
                    state["cur"] += 1
                    state["high"] = max(state["high"], state["cur"])
                    if state["cur"] >= limit:
                        at_limit.set()
                released.wait(timeout=15.0)
                with lock:
                    state["cur"] -= 1
                return "OK — 1 row(s)"

            async def drive():
                prev = server._current["slots"]
                server._current["slots"] = aio.Semaphore(limit)
                try:
                    async def releaser():
                        # Frees the blocked workers once the bound is hit (or
                        # once it is clear it never will be).
                        await aio.to_thread(at_limit.wait, 10.0)
                        released.set()
                    rel = aio.ensure_future(releaser())
                    await aio.gather(*(server.on_call_tool(None, _Params())
                                       for _ in range(calls)))
                    await rel
                finally:
                    server._current["slots"] = prev

            registry.dispatch = blocking_dispatch
            try:
                aio.run(drive())
            finally:
                released.set()
                registry.dispatch = real_dispatch

            assert at_limit.is_set(), \
                (f"only {state['high']} of {limit} slots were ever busy — the "
                 "calls are serialized somewhere, so this check can no longer "
                 "see a flood")
            assert state["high"] <= limit, \
                (f"{state['high']} tool calls held pool threads at once, over the "
                 f"{limit} bound — MCP can starve chat and the web UI again")
        check("concurrent tool calls are bounded to MCP_TOOL_CONCURRENCY",
              tool_calls_cannot_flood_the_shared_worker_pool)

        def a_nested_lifespan_does_not_blank_the_endpoint():
            """`start_mcp` used to set `_current["app"] = None` on the way out
            unconditionally, so an inner `TestClient(app)` — which
            `backend/tests/test_api_keys.py` opens three times inside its outer
            one — left the OUTER client's /mcp answering 503 "The MCP endpoint is
            not running" for the rest of the file. Latent only because no test
            called /mcp after a nested block, and the 503 names exactly the wrong
            cause.
            """
            with TestClient(app):
                pass
            r = mcp_post(c, "tools/list", key=key)
            assert r.status_code != 503, \
                "a nested lifespan tore down the outer client's MCP endpoint"
            assert result_of(r)["tools"], r.text
        check("a nested TestClient lifespan leaves the outer endpoint running",
              a_nested_lifespan_does_not_blank_the_endpoint)

        def a_cross_origin_header_still_reaches_the_gate():
            """CSRFMiddleware wraps every route, and it refuses any
            state-changing request whose Origin matches neither Host nor
            APP_PUBLIC_URL. That caught /mcp: a browser-hosted client — the MCP
            Inspector on localhost:6274 is the first thing anyone points at a new
            server — got a 403 about cross-origin requests before the bearer gate
            ran, on an endpoint that carries no cookie and therefore has no
            ambient credential for a cross-origin page to borrow.

            Asserts a real 200 result, not merely 'not 403': the exemption has to
            let the request through to the tool, not just past the middleware.
            """
            # A FOREIGN origin, not the Inspector's own localhost:6274: this
            # file runs the dev posture (COOKIE_SECURE=false), where csrf.py
            # accepts loopback origins anyway, so a localhost origin here would
            # pass with the exemption deleted.
            r = mcp_post(c, "tools/list", key=key,
                         headers={"Origin": "https://inspector.example"})
            assert r.status_code != 403, \
                f"a browser-hosted client was refused as cross-origin: {r.text}"
            assert result_of(r)["tools"], r.text
            # The exemption is one exact path, not a prefix: everything else
            # still gets the CSRF layer it had before.
            other = c.post("/api/keys", json={"label": "x"},
                           headers={"Origin": "https://inspector.example"})
            assert other.status_code == 403, \
                f"the exemption leaked to /api/keys ({other.status_code})"
        check("a cross-origin Origin reaches the bearer gate, and only on /mcp",
              a_cross_origin_header_still_reaches_the_gate)

        def the_public_hostname_is_accepted():
            host = "ipeds.example.edu"
            r = mcp_post(c, "tools/list", key=key, headers={"Host": host})
            assert r.status_code != 421, \
                ("a forwarded public Host was refused with 421 — DNS-rebinding "
                 "protection is on, which passes on localhost and fails behind "
                 "every reverse proxy")
            assert result_of(r)["tools"], r.text
        check("a request carrying the deployment's public Host is accepted",
              the_public_hostname_is_accepted)

        # --- tools -----------------------------------------------------------
        def lists_exactly_the_registry_tools():
            listed = result_of(mcp_post(c, "tools/list", key=key))["tools"]
            names = sorted(t["name"] for t in listed)
            # Every registry tool, plus `ask` — which is appended by the server
            # and deliberately absent from the registry (see the ask check
            # below for why that separation is load-bearing).
            expected = sorted([s["function"]["name"] for s in registry.tool_specs()]
                              + ["ask"])
            assert names == expected, f"{names} != {expected}"
            by_name = {t["name"]: t for t in listed}
            assert by_name["run_sql"]["inputSchema"] == \
                next(s["function"]["parameters"] for s in registry.tool_specs()
                     if s["function"]["name"] == "run_sql"), \
                "the advertised schema is not the registry's"
            assert "outputSchema" in by_name["run_sql"], \
                "run_sql advertises no output schema, so structured rows are undeclared"
        check("tools/list is the registry's own specs, schemas and all",
              lists_exactly_the_registry_tools)

        def both_protocol_legs_serve_tools():
            legacy = result_of(mcp_post(c, "tools/list", key=key, modern=False))
            modern = result_of(mcp_post(c, "tools/list", key=key))
            assert [t["name"] for t in legacy["tools"]] == \
                [t["name"] for t in modern["tools"]], \
                "the two protocol legs disagree about which tools exist"
        check("both the modern and the legacy protocol leg serve tools/list",
              both_protocol_legs_serve_tools)

        def run_sql_returns_structured_rows():
            r = result_of(mcp_post(c, "tools/call", {
                "name": "run_sql",
                "arguments": {"sql": "SELECT unitid, instnm FROM hd ORDER BY unitid LIMIT 3"},
            }, key=key))
            assert r.get("isError") in (False, None), r
            data = r["structuredContent"]
            assert data["columns"] == ["unitid", "instnm"], data["columns"]
            assert len(data["rows"]) == 3, data["rows"]
            assert data["row_count"] == 3, data
            assert data["truncated"] is False, data
            assert data["sql"].startswith("SELECT unitid"), data["sql"]
            assert isinstance(data["rows"][0][0], int), \
                ("a cell came back as text — the caller is being handed the "
                 "Markdown table's stringified values, not real rows")
            assert "| unitid |" in r["content"][0]["text"], \
                "the Markdown table a model reads is missing"
        check("run_sql carries real columns and rows, not just a Markdown blob",
              run_sql_returns_structured_rows)

        def a_tool_that_raises_is_a_tool_error_not_a_protocol_error():
            # The six lookup tools go straight to app/tools/schema.py, which does
            # NOT catch what the database throws the way run_sql does — a missing
            # table arrives as a bare sqlite3.OperationalError. At this tier an
            # escaping exception becomes a JSON-RPC internal error: a failed CALL
            # rather than a failed TOOL, which a client can neither read nor
            # retry. Forced here rather than left to whichever tool the current
            # database happens to break, so the check means the same thing
            # against the CI fixture and against a real ipeds.db.
            original = registry.dispatch

            def boom(*a, **kw):
                raise RuntimeError("the database fell over")

            registry.dispatch = boom
            try:
                resp = mcp_post(c, "tools/call",
                                {"name": "list_families", "arguments": {}}, key=key)
            finally:
                registry.dispatch = original
            assert resp.status_code == 200, \
                f"a raising tool answered HTTP {resp.status_code}: {resp.text}"
            r = result_of(resp)
            assert r["isError"] is True, r
            assert "fell over" in r["content"][0]["text"], r["content"][0]["text"]
        check("a tool that raises comes back as a tool error, not a protocol error",
              a_tool_that_raises_is_a_tool_error_not_a_protocol_error)

        def a_rejected_query_is_a_tool_error_not_a_500():
            for sql, why in (("DROP TABLE hd", "DDL"),
                             ("SELECT 1; SELECT 2", "two statements")):
                resp = mcp_post(c, "tools/call",
                                {"name": "run_sql", "arguments": {"sql": sql}}, key=key)
                assert resp.status_code == 200, \
                    f"{why} came back as HTTP {resp.status_code}, not a tool result"
                r = result_of(resp)
                assert r["isError"] is True, f"{why} was not flagged as an error: {r}"
                assert "REJECTED" in r["content"][0]["text"], r["content"][0]["text"]
                assert r.get("structuredContent") is None, \
                    f"{why} returned structured rows alongside its refusal"
        check("a rejected query is a readable tool error, not a 500",
              a_rejected_query_is_a_tool_error_not_a_500)

        def an_unknown_tool_is_a_tool_error():
            r = result_of(mcp_post(c, "tools/call",
                                   {"name": "no_such_tool", "arguments": {}}, key=key))
            assert r["isError"] is True, r
        check("an unknown tool name comes back as a tool error",
              an_unknown_tool_is_a_tool_error)

        def sqllint_findings_reach_the_caller():
            r = result_of(mcp_post(c, "tools/call", {
                "name": "run_sql",
                "arguments": {"sql": "SELECT SUM(ctotalt) AS n FROM c_a "
                                     "WHERE awlevel IN (1,20,21)"},
            }, key=key))
            notes = " ".join(r["structuredContent"]["notes"])
            assert "awlevel-cert-double-count" in notes, \
                ("the double-counting warning did not reach the caller — an MCP "
                 f"client would publish the wrong total unwarned. notes={notes!r}")
            assert "awlevel-cert-double-count" in r["content"][0]["text"], \
                "the model-facing text dropped the warning"
        check("a query that trips the SQL linter carries its warning to the caller",
              sqllint_findings_reach_the_caller)

        # --- the ask tool ----------------------------------------------------
        def ask_is_advertised_but_is_not_an_agent_tool():
            listed = result_of(mcp_post(c, "tools/list", key=key))["tools"]
            by_name = {t["name"]: t for t in listed}
            assert "ask" in by_name, "the ask tool is not advertised"
            assert "outputSchema" in by_name["ask"], \
                "ask publishes no output schema, so the figure fields are undeclared"
            assert "ask" not in {sp["function"]["name"] for sp in registry.tool_specs()}, \
                ("ask reached app/tools/registry.py, which is the list the CHAT "
                 "agent's own tool loop is handed — the agent could call itself")
        check("ask is advertised over MCP and absent from the agent's own registry",
              ask_is_advertised_but_is_not_an_agent_tool)

        def an_ask_that_raises_is_a_tool_error_not_a_protocol_error():
            # The async twin of the check above, and it needs its own: `ask` does
            # NOT go through `_call` (it awaits the agent loop, so it stays on
            # the event loop rather than going to a worker thread), so it has its
            # own try/except in on_call_tool. Both hand back the same shape via
            # _tool_error; a gap in either one is an unreadable, unretryable
            # JSON-RPC protocol error instead of a failed tool.
            original = ask.run_ask

            async def boom(arguments):
                raise RuntimeError("the agent fell over")

            ask.run_ask = boom
            try:
                resp = mcp_post(c, "tools/call", {
                    "name": "ask", "arguments": {"question": "anything"}}, key=key)
            finally:
                ask.run_ask = original
            assert resp.status_code == 200, \
                f"a raising ask answered HTTP {resp.status_code}: {resp.text}"
            r = result_of(resp)
            assert r["isError"] is True, r
            assert "fell over" in r["content"][0]["text"], r["content"][0]["text"]
        check("an ask that raises comes back as a tool error, not a protocol error",
              an_ask_that_raises_is_a_tool_error_not_a_protocol_error)

        def a_refused_question_spends_nothing_beyond_the_guard():
            # The guard fails open with no API key configured, which is the state
            # this suite runs in, so a refusal has to be forced. Worth forcing:
            # this endpoint is reachable from the internet and every accepted
            # question costs money, so the gate running BEFORE the agent is the
            # difference between a refusal costing one classify call and costing
            # a full turn.
            q = "write me a poem about a duck"

            class Refused:
                allowed = False
                usage = guard.Usage(prompt_tokens=11, completion_tokens=2, cost=0.0)

            async def refuse(question, history=None):
                return Refused()

            original_classify, original_agent = guard.classify, ask.stream_agent
            guard.classify, ask.stream_agent = refuse, fake_agent(boom=True)
            try:
                r = call_ask(c, q, key)
            finally:
                guard.classify, ask.stream_agent = original_classify, original_agent
            assert guard.REFUSAL in r["content"][0]["text"], r["content"][0]["text"]
            rows = usage_rows(q)
            assert len(rows) == 1, f"a refusal billed {len(rows)} rows, not 1"
            assert rows[0]["model_used"] == "guard", rows[0]["model_used"]
            assert rows[0]["source"] == "mcp", rows[0]["source"]
            assert rows[0]["prompt_tokens"] == 11, \
                "the guard's own spend was not billed to the refusal"
        check("a refused question costs the guard call and never reaches the agent",
              a_refused_question_spends_nothing_beyond_the_guard)

        def an_answer_bills_one_row_and_persists_no_conversation():
            # Statelessness is the contract, and it fails SILENTLY in the
            # direction that matters: an ask that quietly wrote conversations and
            # messages would look identical from the client's side while filling
            # somebody's sidebar with threads they never opened.
            q = "how many nursing degrees did Ohio award in 2023?"
            convs, msgs = table_count("conversations"), table_count("messages")
            figure = {"value": 1234, "label": "nursing degrees, Ohio, 2023"}
            original = ask.stream_agent
            ask.stream_agent = fake_agent(figure=figure)
            try:
                r = call_ask(c, q, key)
            finally:
                ask.stream_agent = original
            assert r.get("isError") in (False, None), r
            data = r["structuredContent"]
            assert "1,234" in data["answer"], data["answer"]
            assert data["figure"] == figure, data["figure"]
            assert data["figure_grounding"] == "exact", data["figure_grounding"]
            rows = usage_rows(q)
            assert len(rows) == 1, f"one turn billed {len(rows)} usage rows"
            assert rows[0]["source"] == "mcp", \
                ("the turn did not record source='mcp', so MCP spend is "
                 "indistinguishable from chat spend on Admin -> Usage")
            assert rows[0]["model_used"] == "fake-model", rows[0]["model_used"]
            assert table_count("conversations") == convs, \
                "ask created a conversation — it is supposed to be stateless"
            assert table_count("messages") == msgs, \
                "ask persisted messages — it is supposed to be stateless"
        check("an answered ask bills one mcp usage row and persists no thread",
              an_answer_bills_one_row_and_persists_no_conversation)

        def a_chart_is_a_declared_field_not_prose():
            # A ```chart fence is a RENDERING DIRECTIVE for the web UI's
            # Chart.jsx, not prose. Forwarded as-is it reaches an MCP client as
            # undeclared JSON inside the answer text: a model reads it as part of
            # the sentence, and a chat client renders it as an opaque code block.
            # It has to come back as a field the output schema names.
            spec = {"type": "line", "x": "year", "y": ["awards"],
                    "data": [{"year": 2023, "awards": 1234}]}
            body = ("Ohio awarded **1,234** nursing degrees in 2023.\n\n"
                    "```chart\n" + json.dumps(spec) + "\n```")
            q = "chart nursing degrees in ohio"
            original = ask.stream_agent
            ask.stream_agent = fake_agent(answer=body, sql="SELECT 2")
            try:
                r = call_ask(c, q, key)
            finally:
                ask.stream_agent = original
            data = r["structuredContent"]
            assert data["chart"] == spec, data.get("chart")
            assert "```chart" not in data["answer"], \
                f"the chart fence stayed in the MCP answer text: {data['answer']!r}"
            # Not just the fence markers — the spec's own JSON must be gone too,
            # or stripping the backticks would "pass" while leaving the payload.
            assert '"awards"' not in data["answer"], data["answer"]
            assert "1,234" in data["answer"], "stripping the chart ate the prose"
            # The text content and the structured answer are the same string, so
            # a client reading either channel sees the same thing.
            assert r["content"][0]["text"] == data["answer"], r["content"][0]["text"]
        check("a chart comes back as a declared field, not as JSON in the prose",
              a_chart_is_a_declared_field_not_prose)

        def a_cached_answer_splits_its_chart_too():
            # The cache stores the answer WITH its fence (the web app needs it
            # there), so the replay path has to split it exactly like a fresh
            # turn. Two code paths return an answer; only one of them was
            # exercised above.
            spec = {"type": "bar", "x": "state", "y": ["n"],
                    "data": [{"state": "OH", "n": 7}]}
            body = ("Seven, in Ohio.\n\n```chart\n" + json.dumps(spec) + "\n```")
            q = "cached question that carries a chart"
            original = ask.stream_agent
            ask.stream_agent = fake_agent(answer=body, sql="SELECT 3")
            try:
                first = call_ask(c, q, key)
            finally:
                ask.stream_agent = original
            assert first["structuredContent"]["chart"] == spec, first

            ask.stream_agent = fake_agent(boom=True)
            try:
                replay = call_ask(c, q, key)
            finally:
                ask.stream_agent = original
            data = replay["structuredContent"]
            assert "Seven, in Ohio." in data["answer"], data["answer"]
            assert "```chart" not in data["answer"], \
                f"the cache replay shipped the raw fence: {data['answer']!r}"
            assert data["chart"] == spec, data.get("chart")
        check("a cached answer's chart is split out on replay too",
              a_cached_answer_splits_its_chart_too)

        def a_mangled_chart_fence_never_ships_its_json():
            # The fence is server-written and clean under structured emission,
            # but the fence FALLBACK path lets the MODEL write it, and that is
            # where a mangle comes from. A fence we cannot parse is still not
            # prose: strip it, and report no chart rather than a half-read one.
            body = ("Twelve.\n\n```chart\n{\"type\": \"line\", \"data\": [{,,,\n```")
            q = "a question whose chart fence is mangled"
            original = ask.stream_agent
            ask.stream_agent = fake_agent(answer=body, sql="SELECT 4")
            try:
                r = call_ask(c, q, key)
            finally:
                ask.stream_agent = original
            data = r["structuredContent"]
            assert data["chart"] is None, data["chart"]
            assert "```chart" not in data["answer"], data["answer"]
            assert "{" not in data["answer"], \
                f"unparseable chart JSON leaked into the answer: {data['answer']!r}"
            assert "Twelve." in data["answer"], data["answer"]
        check("an unparseable chart fence is stripped, not forwarded",
              a_mangled_chart_fence_never_ships_its_json)

        def an_answer_without_a_chart_is_untouched():
            # The field is REQUIRED by the output schema, so it has to be present
            # and null rather than absent — an SDK client validates against that
            # schema, and a missing required key is a validation failure on the
            # ordinary case.
            q = "a question that needs no chart"
            original = ask.stream_agent
            ask.stream_agent = fake_agent(sql="SELECT 5")
            try:
                r = call_ask(c, q, key)
            finally:
                ask.stream_agent = original
            data = r["structuredContent"]
            assert "chart" in data, list(data)
            assert data["chart"] is None, data["chart"]
            assert data["answer"] == "Ohio awarded **1,234** nursing degrees in 2023.", \
                data["answer"]
        check("an answer with no chart still carries the declared null field",
              an_answer_without_a_chart_is_untouched)

        def the_output_schema_declares_the_chart():
            # The schema is the published contract; a field returned but not
            # declared is one an SDK client may reject or ignore.
            props = ask.OUTPUT_SCHEMA["properties"]
            assert "chart" in props, list(props)
            assert "chart" in ask.OUTPUT_SCHEMA["required"], ask.OUTPUT_SCHEMA["required"]
            assert ask.TOOL.output_schema is ask.OUTPUT_SCHEMA or \
                "chart" in (ask.TOOL.output_schema or {}).get("properties", {}), \
                "the advertised tool's output_schema does not mention the chart"
        check("the ask output schema declares the chart field",
              the_output_schema_declares_the_chart)

        def a_cache_hit_does_not_run_the_agent():
            # The cache is SHARED with the web chat, so this also pins the thing
            # that goes wrong quietly: an ask that stored an answer without its
            # result rows would hand a later chat hit an answer with nothing to
            # ground a recited number against (the gap migration 31 closed).
            q = "which state granted the most masters degrees in education?"
            original = ask.stream_agent
            ask.stream_agent = fake_agent(answer="Cached answer body.",
                                          sql="SELECT stabbr FROM hd LIMIT 1")
            try:
                first = call_ask(c, q, key)
            finally:
                ask.stream_agent = original
            assert "Cached answer body." in first["content"][0]["text"], first

            ask.stream_agent = fake_agent(boom=True)
            try:
                second = call_ask(c, q, key)
            finally:
                ask.stream_agent = original
            assert "Cached answer body." in second["content"][0]["text"], \
                f"the second ask did not replay the cached answer: {second}"
            rows = usage_rows(q)
            assert len(rows) == 2, f"two asks billed {len(rows)} rows"
            assert rows[1]["cached"] == 1, "the replay was not billed as a cache hit"
            assert rows[1]["source"] == "mcp", rows[1]["source"]
            assert rows[1]["model_used"] == "cache", rows[1]["model_used"]
            con = connect()
            try:
                stored = con.execute(
                    "SELECT results FROM query_cache WHERE question=?", (q,)).fetchone()
            finally:
                con.close()
            assert stored is not None and stored["results"], \
                ("ask cached an answer with no result rows — a later CHAT hit on "
                 "this row would lose the conversation's grounding chain")
        check("a cached question replays its answer without running the agent",
              a_cache_hit_does_not_run_the_agent)

        def a_caching_failure_does_not_lose_the_answer():
            # Chat caches AFTER it has streamed the answer, so a throw there
            # costs the tail of something the user already read. Here the answer
            # has not been returned yet, so an unguarded throw would come back as
            # a tool error and lose an answer that was finished and already
            # billed for.
            q = "what happens when the cache write fails?"
            original_agent, original_store = ask.stream_agent, skills.cache_store

            def explode(*a, **kw):
                raise RuntimeError("the cache is on fire")

            ask.stream_agent = fake_agent(answer="The answer survived.")
            skills.cache_store = explode
            try:
                r = call_ask(c, q, key)
            finally:
                ask.stream_agent, skills.cache_store = original_agent, original_store
            assert r.get("isError") in (False, None), \
                f"a failed cache write turned a good answer into an error: {r}"
            assert "The answer survived." in r["content"][0]["text"], r
            assert len(usage_rows(q)) == 1, "the turn was billed more than once"
        check("a failed cache write costs the cache entry, not the answer",
              a_caching_failure_does_not_lose_the_answer)

        def no_llm_key_is_a_clean_tool_error():
            # The real, unpatched path: this suite runs with LLM_API_KEY="", so
            # stream_agent yields an error and no `done` event. The contract is
            # that ask says so readably and the DATA tools keep working — an MCP
            # client on a deployment with no provider configured can still query.
            resp = mcp_post(c, "tools/call",
                            {"name": "ask", "arguments": {"question": "how many students?"}},
                            key=key)
            assert resp.status_code == 200, \
                f"an unconfigured provider answered HTTP {resp.status_code}: {resp.text}"
            r = result_of(resp)
            assert r["isError"] is True, f"the failure was returned as an answer: {r}"
            assert "not configured" in r["content"][0]["text"].lower(), \
                r["content"][0]["text"]
            assert usage_rows("how many students?") == [], \
                "a turn that spent nothing still wrote a usage row"
            still_works = result_of(mcp_post(c, "tools/call", {
                "name": "run_sql",
                "arguments": {"sql": "SELECT unitid FROM hd LIMIT 1"}}, key=key))
            assert still_works.get("isError") in (False, None), \
                "ask being unavailable took the data tools down with it"
        check("with no LLM provider configured ask fails cleanly and run_sql still works",
              no_llm_key_is_a_clean_tool_error)

        def a_malformed_question_is_refused_before_any_spend():
            # The length cap is the chat path's, for the chat path's reason: the
            # body limiter allows 10 MB, and 10 MB of "question" would be billed
            # to the provider as tokens and written to usage_log. Refused before
            # the rate limiter, so a script hammering junk cannot also burn the
            # asker's turn budget doing it.
            for arguments, why in (({"question": "   "}, "an empty question"),
                                   ({}, "a missing question"),
                                   ({"question": "x" * 5000}, "an over-long question")):
                r = result_of(mcp_post(c, "tools/call",
                                       {"name": "ask", "arguments": arguments}, key=key))
                assert r["isError"] is True, f"{why} was accepted: {r}"
            assert usage_rows("x" * 5000) == [], "a refused question was billed"
        check("an empty or over-long question is refused as a tool error",
              a_malformed_question_is_refused_before_any_spend)

        def an_unidentified_caller_is_refused_not_defaulted():
            # The gate knows who is calling; `ask` reads that through a context
            # variable, and the SDK's task plumbing is what carries it. If that
            # ever stops working the value is None, and None must never be read
            # as a default — every step after this point spends money, reads a
            # cache, and bills a row on somebody's behalf, and there is no safe
            # guess for whose. Forced here because the only way to observe it
            # for real is the plumbing already being broken.
            #
            # Asserting `isError` alone would be VACUOUS here and was, until a
            # mutation showed it: this suite has no provider key, so every ask
            # that runs to completion already ends as a tool error. The check has
            # to name THIS refusal, and the boom-agent proves nothing downstream
            # ran on a caller the gate never named.
            original_caller, original_agent = ask.current_caller, ask.stream_agent
            ask.current_caller, ask.stream_agent = (lambda: None), fake_agent(boom=True)
            try:
                r = result_of(mcp_post(c, "tools/call", {
                    "name": "ask", "arguments": {"question": "who is asking?"}}, key=key))
            finally:
                ask.current_caller, ask.stream_agent = original_caller, original_agent
            assert r["isError"] is True, \
                f"an unidentifiable caller got an answer anyway: {r}"
            assert "identify the caller" in r["content"][0]["text"], \
                (f"the refusal did not come from the caller check: "
                 f"{r['content'][0]['text']!r}")
            assert usage_rows("who is asking?") == [], \
                "a turn with no known caller still billed somebody"
        check("a caller the gate did not identify is refused, never defaulted",
              an_unidentified_caller_is_refused_not_defaulted)

        def no_dataset_is_a_tool_error_not_an_agent_turn():
            # A deployment with no year imported would otherwise pay for a guard
            # call and a full agent turn to discover the tables are not there,
            # and hand back whatever the model made of that.
            original_years, original_agent = ask.ipeds_years, ask.stream_agent
            ask.ipeds_years, ask.stream_agent = (lambda: []), fake_agent(boom=True)
            try:
                r = result_of(mcp_post(c, "tools/call", {
                    "name": "ask", "arguments": {"question": "any data?"}}, key=key))
            finally:
                ask.ipeds_years, ask.stream_agent = original_years, original_agent
            assert r["isError"] is True, r
            assert "dataset" in r["content"][0]["text"].lower(), r["content"][0]["text"]
        check("with no dataset loaded ask says so without running the agent",
              no_dataset_is_a_tool_error_not_an_agent_turn)

        def a_failed_turn_is_an_error_and_is_still_billed():
            # An agent turn that ends in an error still consumed provider calls.
            # Returning it as an ANSWER would hand the caller an error string as
            # though it were data; dropping its usage row would under-report the
            # spend it really cost.
            q = "a question whose turn fails"

            async def failing(question, **kwargs):
                yield {"type": "done",
                       "result": AgentResult(model_used="fake-model",
                                             error="The provider timed out.",
                                             prompt_tokens=40, completion_tokens=0)}

            original = ask.stream_agent
            ask.stream_agent = failing
            try:
                r = result_of(mcp_post(c, "tools/call",
                                       {"name": "ask", "arguments": {"question": q}}, key=key))
            finally:
                ask.stream_agent = original
            assert r["isError"] is True, f"a failed turn came back as an answer: {r}"
            assert "timed out" in r["content"][0]["text"], r["content"][0]["text"]
            rows = usage_rows(q)
            assert len(rows) == 1 and rows[0]["ok"] == 0 and rows[0]["source"] == "mcp", \
                f"a failed turn was not billed as a failed mcp turn: {rows}"
        check("a turn that ends in an error is a tool error and still bills its spend",
              a_failed_turn_is_an_error_and_is_still_billed)

        def ask_spends_the_same_per_user_budget_as_the_web_chat():
            # Its OWN user, because this limiter is keyed on the user and the rest
            # of the file shares one. The regression: swapping this call for the
            # per-KEY MCP limiter, or dropping it, would leave MCP as an
            # unmetered second door onto the same provider spend — and every
            # other test in this file would still pass.
            budget_email = "budget@example.edu"
            assert c.post("/api/admin/allowlist",
                          json={"email": budget_email}).status_code in (200, 201)
            sign_in(c, budget_email)
            bkey = c.post("/api/keys", json={"label": "budget"}).json()["key"]
            for i in range(CHAT_CAP):
                r = mcp_post(c, "tools/call",
                             {"name": "ask", "arguments": {"question": f"q{i}"}},
                             key=bkey)
                assert r.status_code == 200, f"ask {i} got HTTP {r.status_code}"
            r = result_of(mcp_post(c, "tools/call",
                                   {"name": "ask", "arguments": {"question": "one more"}},
                                   key=bkey))
            assert r["isError"] is True and "many requests" in r["content"][0]["text"].lower(), \
                f"the {CHAT_CAP + 1}th ask was not throttled: {r}"
            chat = c.post("/api/chat/stream", json={"question": "and through the web door?"})
            assert chat.status_code == 429, \
                (f"the web chat still answered ({chat.status_code}) after ask "
                 "exhausted this user's budget — the two doors are not sharing a "
                 "limiter, so one person can spend twice over")
            sign_in(c, "admin@example.edu")
        check("ask charges the same per-user budget the web chat charges",
              ask_spends_the_same_per_user_budget_as_the_web_chat)

        # --- resources -------------------------------------------------------
        def resources_are_listed_and_readable():
            listed = result_of(mcp_post(c, "resources/list", key=key))["resources"]
            uris = {r["uri"] for r in listed}
            assert uris == set(resources.CATALOG), uris
            docs = get_settings().schema_md_path
            # Compared against the files themselves rather than against a phrase
            # picked out of them: the regression worth catching is the URI-to-file
            # mapping getting crossed or a read coming back short, and a prose
            # marker would also go red the day somebody edits a heading.
            for uri, path in ((resources.SCHEMA_URI, docs),
                              (resources.DATASET_URI, docs.parent / "DATASET.md")):
                r = result_of(mcp_post(c, "resources/read", {"uri": uri}, key=key))
                text = r["contents"][0]["text"]
                assert text == path.read_text(encoding="utf-8"), \
                    f"{uri} did not read back as {path.name}"
        check("both guides are listed and read back as themselves",
              resources_are_listed_and_readable)

        def an_unknown_resource_is_refused_cleanly():
            resp = mcp_post(c, "resources/read", {"uri": "ipeds://docs/NOPE.md"}, key=key)
            assert resp.status_code in (200, 400, 404), resp.status_code
            payload = resp.json()
            assert "error" in payload or payload["result"].get("isError"), \
                f"an unknown resource URI came back as a successful read: {payload}"
        check("an unknown resource URI is refused rather than answered",
              an_unknown_resource_is_refused_cleanly)

        def the_shipped_documents_exist_where_the_app_looks():
            s = get_settings()
            for path in (s.schema_md_path, s.schema_md_path.parent / "DATASET.md"):
                assert path.is_file(), \
                    (f"{path} is missing — the Dockerfile COPY for it is what "
                     "makes this true inside the image, and a resource that 404s "
                     "only in a container is invisible from here")
        check("both documents exist at the paths the resource handler reads",
              the_shipped_documents_exist_where_the_app_looks)

        # --- the per-key rate limit -----------------------------------------
        def the_cap_returns_429_and_is_per_key():
            spender = c.post("/api/keys", json={"label": "spender"}).json()["key"]
            for i in range(MCP_CAP):
                r = mcp_post(c, "tools/list", key=spender)
                assert r.status_code == 200, f"call {i} failed early: {r.status_code}"
            r = mcp_post(c, "tools/list", key=spender)
            assert r.status_code == 429, \
                f"the {MCP_CAP + 1}th call on one key got {r.status_code}, not 429"
            other = mcp_post(c, "tools/list", key=key)
            assert other.status_code == 200, \
                ("one key's exhausted budget blocked another key — the limiter "
                 "is counting something other than the key")
        check("the per-key cap returns 429 and does not spill onto another key",
              the_cap_returns_429_and_is_per_key)

    # --- outside any lifespan -----------------------------------------------
    def no_lifespan_is_a_503_not_a_crash():
        bare = TestClient(app)
        r = bare.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                      headers={"Accept": "application/json, text/event-stream"})
        assert r.status_code == 503, \
            (f"a request with no lifespan started got {r.status_code} — it should "
             "be a plain 503, not a crash inside the SDK's session manager")
    check("a request before startup answers 503, not an internal error",
          no_lifespan_is_a_503_not_a_crash)

    def a_second_lifespan_in_one_process_still_works():
        with TestClient(app) as c2:
            sign_in(c2, "admin@example.edu")
            k2 = c2.post("/api/keys", json={"label": "second"}).json()["key"]
            assert result_of(mcp_post(c2, "tools/list", key=k2))["tools"], \
                ("the endpoint died on a second startup in one process — the "
                 "transport's session manager refuses a second run(), so the app "
                 "has to be rebuilt per lifespan and not at import time")
    check("a second startup in the same process serves requests too",
          a_second_lifespan_in_one_process_still_works)


if __name__ == "__main__":
    print("=== MCP endpoint ===")
    run()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): " + ", ".join(FAILURES))
        sys.exit(1)
    print("\nAll MCP endpoint checks passed.")
