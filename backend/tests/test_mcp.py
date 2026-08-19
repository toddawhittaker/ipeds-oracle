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
# Low enough that the 429 case can reach the cap in a fraction of a second, high
# enough that the rest of the suite (about a dozen calls on one key) never
# brushes it. The 429 case mints its OWN key so its spending is its own.
MCP_CAP = 30
os.environ["MCP_RATE_MAX_PER_KEY"] = str(MCP_CAP)

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

from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.mcpsrv import resources  # noqa: E402
from app.tools import registry  # noqa: E402

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
            expected = sorted(s["function"]["name"] for s in registry.tool_specs())
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
