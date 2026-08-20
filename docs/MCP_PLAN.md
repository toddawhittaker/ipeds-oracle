# v0.5.0 — MCP server with per-user API keys

> **This file is temporary.** Everything else in `docs/` describes how the
> system works today; this one describes work that has not happened yet. It
> lives here so a build step can be picked up from cold, and it is **deleted in
> Step 5**, replaced by `docs/MCP.md`. If you are reading it after v0.5.0 is
> tagged, it should not exist — say so.

## Context

IPEDS Oracle answers questions about IPEDS data through its own web chat. Today
that is the only way in. This release adds a second front door: a Model Context
Protocol (MCP) endpoint so other tools — Claude Code, the Anthropic Messages API
MCP connector, any MCP client that can set a header — can reach the same data
and the same agent, authenticated with a per-user API key.

Why it is cheap to build: `backend/app/tools/registry.py:1` already says these
tools are "embedded tools — same functions can later be re-exported over MCP
without change." The read-only SQL sandbox in `backend/app/tools/sql.py`
(read-only immutable handle, single SELECT/WITH, watchdog timeout, per-value and
whole-result byte budgets) carries over untouched. The work is authentication,
transport, rate limiting, and a UI for issuing keys.

Two decisions that shape everything below:

- **Mounted in the existing app, not a second process.** The app already runs
  the identical query engine off the event loop via `run_in_threadpool`
  (`backend/app/llm.py:71`), so a separate process buys only crash isolation —
  not worth a second service, a second port, and a schema-ownership race between
  two processes running migrations on the same `app.db`.
- **Static bearer keys, not OAuth.** The MCP spec says HTTP servers SHOULD
  implement OAuth 2.1 with RFC 9728 discovery. We are deliberately not doing
  that. Static keys work with Claude Code (`--header`) and the Messages API MCP
  connector; they do not work with claude.ai custom connectors, which only offer
  OAuth. Accepted, and recorded in `docs/MCP.md` at the end.

**Decisions already made (do not re-litigate):**

| Question | Answer |
|---|---|
| Who can mint a key | Both: users self-serve, admins can also mint for a user and revoke anyone's |
| Tool surface | The seven data tools **and** an `ask` tool that runs the full agent loop |
| Key powers | Every key can call every tool. No per-key scopes. |
| `ask` and chat history | Stateless. No conversation or message rows. Spend is still recorded. |
| Release scope | 0.5.0 is this feature only |
| Public access | Behind the existing TLS reverse proxy on a real hostname |
| Protocol code | The official `mcp` Python SDK |
| This plan file | `docs/MCP_PLAN.md` during the epic, replaced by `docs/MCP.md` at the tag |

## Branch and CI

Epic branch **`feat/mcp-server`**, cut from `main`. Every step below is its own
PR **targeting `feat/mcp-server`**, not `main`. When the last step is green, one
PR merges the epic into `main`, then the tag goes on that commit.

**CI does not run on this branch as things stand.** `.github/workflows/ci.yml:17-22`
triggers only on `push: branches: [main]` and `pull_request: branches: [main]`.
Widening that trigger is Step 0 and must land before anything else, or every
sub-PR merges unchecked.

Per-PR rules from `CLAUDE.md` still apply on the epic branch: `scripts/run_ci_local.sh`
before pushing, never merge on a red or stale check, never lower a floor.

Step 0 widened `ci.yml` only. `codeql.yml` was left on `main`, so the epic ran
without static security analysis until #348 widened it the same way; both
`feat/mcp-server` lines come out together when the branch is deleted.

---

## Step 0 — CI on the epic branch, and this plan checked in

**PR title:** `chore: run CI on the feat/mcp-server epic branch`

1. `.github/workflows/ci.yml` — add `feat/mcp-server` to both `push.branches`
   and `pull_request.branches`. Leave the `tags: ["v*"]` trigger alone.
2. Add `docs/MCP_PLAN.md` — this document.

**Done when:** a throwaway PR into `feat/mcp-server` shows lint · unit · backend ·
e2e · image running.

---

## Step 1 — API keys in the database and over the API

**PR title:** `feat(keys): per-user API keys for the MCP endpoint`

No MCP code yet. This step is complete and useful on its own: a user can mint a
key, see it once, and revoke it.

### Migration

One migration, number **37**, following the multi-statement style of migration 35
(`backend/app/db.py:486-501`) with a prose comment above it explaining why.

```sql
CREATE TABLE api_keys (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    key_hash     TEXT NOT NULL UNIQUE,
    last4        TEXT NOT NULL,
    label        TEXT,
    created_at   REAL NOT NULL,
    created_by   TEXT,            -- email of the minting admin, NULL if self-serve
    last_used_at REAL,
    revoked_at   REAL
);
CREATE INDEX ix_api_keys_user ON api_keys(user_id);

CREATE TABLE mcp_request_attempts (
    key_id     INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX ix_mcp_attempts_created ON mcp_request_attempts(created_at);

ALTER TABLE usage_log ADD COLUMN source TEXT;   -- NULL/'chat' | 'mcp'
```

`mcp_request_attempts` mirrors `chat_request_attempts` from migration 28
(`backend/app/db.py:366-373`). `usage_log.source` is what lets Admin → Usage tell
MCP spend apart from chat spend later.

**This trips the golden schema fingerprint.** Regenerate with
`python backend/tests/test_migrations.py --print-schema` and update
`EXPECTED_SCHEMA_FINGERPRINT` (`backend/tests/test_migrations.py:787`) in the
same PR.

### New module: `backend/app/apikeys.py`

Small and boring. Reuses `new_token()` and `hash_token()` from
`backend/app/security.py:13-17` — the same primitives that mint session tokens.

- `mint(user_id, label, created_by=None) -> (raw_key, row)` — raw key is
  `"ipeds_mcp_" + new_token()`. Store `hash_token(raw)` and the last four
  characters. Return the raw string exactly once; it is never recoverable.
- `verify(raw) -> sqlite3.Row | None` — look up by `hash_token(raw)`, reject if
  `revoked_at` is set, and **re-check `is_allowlisted(con, email)`**
  (`backend/app/auth.py:17-26`), exactly as `_user_from_request` does at
  `backend/app/auth.py:324`. Without this, removing someone from the allowlist
  kills their browser session and leaves their API key working.
- `touch(key_id)` — update `last_used_at`, but only when the stored value is
  more than 60 seconds old. A write on every request is avoidable amplification
  on a shared SQLite file.
- `list_for_user(user_id)`, `list_all()`, `revoke(key_id)` — revoke sets
  `revoked_at`, never deletes, so the audit trail survives.

sha256 is the right hash here and bcrypt is not: the input is 32 bytes of
`secrets.token_urlsafe` entropy, not a guessable password, so a slow KDF buys
nothing and costs a hash on every MCP request.

### Endpoints

User-facing, new router `backend/app/routers/keys.py`, `prefix="/api/keys"`,
`Depends(current_user)`:

- `GET /api/keys` — the caller's own keys. Never returns a hash or a raw key.
- `POST /api/keys` — body `{label}`. Returns `{key: "ipeds_mcp_…", …}` — the
  only time the raw value is ever sent.
- `DELETE /api/keys/{id}` — 404 if the key is not the caller's.

Admin, added to `backend/app/routers/admin.py` (router-level `require_admin`
already applies, `admin.py:25-26`):

- `GET /api/admin/keys` — all keys with owner email.
- `POST /api/admin/keys` — body `{email, label}`, mints on someone's behalf and
  records `created_by`.
- `DELETE /api/admin/keys/{id}`.

Follow house conventions: plain dicts, no `response_model=`, `HTTPException(400,
"Human sentence.")`, static paths registered before `/{id}` paths
(`admin.py:1264-1272`).

Register `keys.router` alongside the others at `backend/app/main.py:190-192` —
before the SPA catch-all block.

### Tests — `backend/tests/test_api_keys.py`

Standalone script in the house style (env vars set before importing `app`, a
`check()` helper, `sys.exit(1)` on failure — see `backend/tests/test_admin_router.py:19-58`).
Each test names the regression it catches:

- A raw key is returned exactly once; a second `GET` never exposes it.
- The stored row holds a hash, never the raw key.
- A revoked key fails `verify`.
- **De-allowlisting a user makes their key stop working** — the one that
  silently breaks if the allowlist re-check is dropped.
- A user cannot list, mint against, or delete another user's key.
- A non-admin gets 403 from every `/api/admin/keys` route.

New modules land under `backend/app/` and are immediately subject to the
per-module 80% floor in `scripts/coverage_check.sh`.

---

## Step 2 — the MCP endpoint, data tools only

**Two PRs, in this order:**

- **2a** — `chore(deps): add the mcp SDK` (dependency only, nothing else)
- **2b** — `feat(mcp): serve a Streamable HTTP MCP endpoint at /mcp`

### Step 2a — the dependency, alone — **DONE** (#347)

Landed as predicted: 16 new distributions, 47 lock pins to 63, nothing removed
and no existing pin moved. Three corrections to what this section guessed are
recorded at the end of it.

Add `mcp>=2.0.0` to `backend/requirements.txt`, and regenerate
`backend/requirements.lock` **in the same PR** with
`pip-compile --generate-hashes --output-file requirements.lock requirements.txt`.
`backend/tests/test_requirements_lock.py` enforces the pairing. The `npm audit`
rule does not apply — no frontend lockfile changes here.

**Know what this drags in before you run it.** `mcp` 2.0.0 requires Python ≥3.10
(the image is 3.12, fine) and declares these runtime dependencies, several of
which are new to this project:

- already present: `anyio`, `pydantic`, `python-multipart`, `starlette`,
  `uvicorn`, `typing-extensions`
- **new**: `httpx2>=2.5.0` (a *second* HTTP client alongside the `httpx` the app
  already uses), `jsonschema`, `mcp-types`, `opentelemetry-api`,
  `pyjwt[crypto]` (pulls `cryptography`), `sse-starlette`, `typing-inspection`

That is roughly sixteen new distributions in the resolved closure, a second HTTP
stack in the image, and a larger surface for the gitleaks/semgrep/CodeQL gates
and for dependabot noise. Worth a sentence in the PR body.

**Two things to deal with in this PR, both found before we started:**

1. **`mcp==1.23.3` is already installed in `/home/todd/projects/ipeds/.venv` and
   is not in `requirements.lock`.** ~~Somebody pip-installed it by hand.~~
   **Wrong — it was a transitive of `semgrep`**, which bundles an MCP feature and
   pins that exact version. Taking the venv to `mcp` 2.0.0 therefore broke
   semgrep outright (`ModuleNotFoundError: No module named 'mcp.server.fastmcp'`,
   a module the 2.0 SDK no longer has), which would have failed the pre-push gate
   with a misleading "SAST finding" for anyone whose semgrep lived in the app
   venv. Fixed by isolating semgrep as its own tool
   (`uv tool install semgrep==1.171.0`, or `pipx`) and removing it from `.venv`,
   which is what `scripts/run_ci_local.sh:59` already recommended. CI was never
   affected — it installs semgrep on its own runner.
2. **Adding `mcp` changes the HTTP client under the whole backend test suite.**
   `mcp` 2.0 depends on `httpx2`, and Starlette's `TestClient` prefers `httpx2`
   when it is importable (it currently emits a deprecation warning because it is
   not). So the moment this lands, all ~30 `TestClient(app)` scripts switch from
   `httpx` 0.28 to `httpx2` for request construction, header normalization,
   cookies, and redirects. Nothing about that is wrong, but **run the full suite
   on this PR alone and read the diff in behavior** rather than discovering it as
   a flake three PRs later.

**Land the dependency as its own PR**, ahead of any MCP code. If the footprint
turns out to be unacceptable once you see the lockfile diff, the fallback is a
few hundred lines of plain JSON-RPC handling with no new dependency — but that is
a decision to revisit deliberately, not to drift into.

**What #347 corrected in the above, so nobody re-derives it:**

- **`typing-inspection` was already in the lock** via `pydantic-settings`, so it
  is not new. The list of seven "new" packages above also omits the second-order
  transitives that make up the rest of the sixteen: `httpcore2`, `truststore`,
  `jsonschema-specifications`, `referencing`, `rpds-py`, `attrs`, `cffi`,
  `pycparser`.
- **"A second HTTP stack" undersells what was already there.** The image has
  shipped `requests` and `urllib3` all along, via `fastembed` and `resend`, so
  `httpx2` makes four rather than two. Consolidating to one stack is **not
  available**: `huggingface_hub`, a hard dependency of `fastembed`, requires
  `httpx<1,>=0.23.0` unconditionally, while `mcp` requires `httpx2>=2.5.0`.
  Migrating the app's 22 `httpx` call sites would delete zero packages and would
  re-derive `nces.py`'s manual redirect validation (SEC-6) and `llmhttp.py`'s
  error taxonomy against a young library. Revisit only if `huggingface_hub` ever
  drops `httpx`.
- **The Dockerfile needed no change.** Every compiled distribution
  (`cryptography`, `cffi`, `rpds-py`) has a prebuilt cp312 manylinux wheel and
  `python:3.12-slim` is glibc 2.41, so no build dependencies were required. Added
  wheel weight is 6.2 MB.
- **Two gaps found by the review passes, both fixed on the epic branch:** CodeQL
  did not run here at all (`codeql.yml` was `main`-only — #348), and nothing
  audited `requirements.lock` for advisories, because GitHub's dependency graph
  cannot read a `.lock` file (#349 adds a `pip-audit` job).

### Step 2b — what the SDK actually looks like in 2.0

Verified by reading the `mcp` 2.0.0 wheel, not from memory — the API changed in
2.0 and the older `FastMCP` recollection is wrong. Four facts drive the design:

- The class is **`MCPServer`** in `mcp.server.mcpserver`. It has
  `.streamable_http_app(...)`, which returns a plain **Starlette** app, and
  `.add_tool(fn, ...)` / `.add_resource(...)`.
- **Use the low-level `Server`, not the high-level `MCPServer`.** The high-level
  `add_tool` takes a Python callable and derives the schema from type
  annotations, which would mean re-declaring all seven signatures and letting
  them drift from `registry.py`. The low-level `Server` instead takes
  `on_list_tools` / `on_call_tool` handlers (`mcp/server/lowlevel/server.py:146-160`)
  where **you supply the JSON Schema yourself** — so `registry.tool_specs()` is
  reusable as-is and stays the single source of truth for both the chat agent
  loop and MCP. The adaptation is a field rename: OpenAI's
  `{"function": {"name", "description", "parameters"}}` becomes MCP's
  `Tool(name=…, description=…, input_schema=…)`.
- **Two things the low-level tier does not do for you**, both of which
  `registry.dispatch` already handles: it does not validate arguments against
  the advertised schema, and an exception from a handler becomes a protocol
  error rather than a tool result. `dispatch` returns `"ERROR: …"` /
  `"SQL REJECTED: …"` strings instead of raising, which is exactly the contract
  this tier needs. Set `is_error=True` on those sentinel prefixes so clients
  render them as failures.
- **`auth=None` means no OAuth is advertised, exactly as we want.** In
  `mcp/server/lowlevel/server.py:720-860`, the `.well-known` protected-resource
  routes are added only `if auth and auth.resource_server_url`, and the
  `WWW-Authenticate`-emitting `RequireAuthMiddleware` is used only if a
  `token_verifier` is passed. Pass neither and the endpoint is a bare ASGI route
  we can put our own check in front of. Nothing to suppress.
- **The lifespan trap, and the one-shot trap right behind it.** The returned
  Starlette app carries `lifespan=lambda app: session_manager.run()`, and
  Starlette does **not** run a mounted or adopted sub-app's lifespan — so the
  manager never starts and the first request dies with
  `RuntimeError: Task group is not initialized. Make sure to use run().`
  (`mcp/server/streamable_http_manager.py:172`). The parent app's `lifespan`
  (`backend/app/main.py:87`) must enter `async with server.session_manager.run():`
  itself.

  **But `.run()` raises if called twice on the same instance**
  (`streamable_http_manager.py:136-143`), and this repo's tests enter
  `with TestClient(app)` dozens of times in a single process — `test_access_gate.py`
  alone does about fifteen. So **build the app inside the lifespan, not at
  import time**: each `streamable_http_app()` call assigns a fresh session
  manager (`mcp/server/lowlevel/server.py:756`), so a per-startup rebuild is
  both correct and the only thing the test suite tolerates. The route registered
  at import time points at a small shim that delegates to whatever the current
  lifespan built, and answers 503 when nothing has.

**The default that would break production:** `streamable_http_app(host=...)`
defaults to `"127.0.0.1"`, and when host is a loopback value the SDK
*auto-enables DNS-rebinding protection* with `allowed_hosts=["127.0.0.1:*",
"localhost:*", "[::1]:*"]`. Behind a reverse proxy that forwards the real
`Host: ipeds.example.edu`, every request would be rejected — and it would work
perfectly on localhost, so no local test would catch it. Pass an explicit
`transport_security=TransportSecuritySettings(...)` whose `allowed_hosts` and
`allowed_origins` are derived from the existing `APP_PUBLIC_URL` setting.

**Register it as a `Route`, never a `Mount`.** Starlette's `Mount` compiles its
pattern as `path + "/{path:path}"` (`starlette/routing.py:381`), so
`app.mount("/mcp", …)` does not match a bare `/mcp` — the request falls through
to the catch-alls at `main.py:217` and `:221`, and the endpoint answers `405` to
every POST while `GET /mcp` serves the React shell. Silently, comprehensively
wrong. Append a `Route("/mcp", endpoint=<the guarded ASGI app>)` to
`app.router.routes` **before** the `if WEB_DIST.exists():` block. A `Route` whose
endpoint is an ASGI instance rather than a function accepts every method, which
is what the transport needs.

Flags to pass: `stateless_http=True` and `json_response=True` — a POST-only
server with no server-side session state and plain JSON responses. Conformant
(the spec makes the GET/SSE side optional), best behaved behind a proxy, and it
is what makes the endpoint testable with the synchronous `TestClient` the rest
of the suite uses. Note `stateless_http` only affects the **legacy** protocol
leg; the 2026-07-28 path is sessionless by construction and never reads the flag.
Set both anyway, because you do not control which era a client picks. Set
`max_request_body_size` to match `max_request_body_mb` so the two body caps do
not disagree.

What `json_response=True` costs: the request-scoped notification channel, so
`ctx.report_progress` from inside a tool goes nowhere. All seven tools are
synchronous SQL and lookup calls with no progress to report. Fine.

### New package: `backend/app/mcpsrv/`

Named `mcpsrv`, not `mcp`, on purpose: a local package sharing a name with the
third-party dependency it imports is a readability trap and one accidental
relative import away from a confusing failure.

- `server.py` — `build_server()` returns the low-level `Server` with
  `on_list_tools` / `on_call_tool` / `on_list_resources` / `on_read_resource`,
  plus a `start_mcp()` async context manager that builds the Starlette app and
  runs the session manager for one process lifetime. **Every handler must wrap
  its blocking call in `run_in_threadpool`.** This is not optional at this tier:
  the high-level `MCPServer` hops sync tool functions onto a worker thread for
  you, but low-level handlers are `Awaitable` and are awaited directly on the
  event loop. The tool functions use blocking `sqlite3`, so an unwrapped handler
  stalls every live chat stream in the process. Same call the agent loop already
  uses at `backend/app/llm.py:71`, so both paths share one anyio thread limiter —
  worth a line in the module docstring, since `llm.py`'s note about a burst of
  heavy queries saturating that pool now covers MCP traffic too.
- `results.py` — builds the structured half of a `run_sql` result. MCP has a
  first-class channel for this: `content` carries the Markdown table for the
  model to read, `structured_content` carries `{columns, rows, row_count,
  truncated, sql, notes}` for the caller's code, and `output_schema` on the
  `Tool` is the published contract between them. Feed it by passing a
  per-request `result_sink` dict into `registry.dispatch` — the parameter
  already exists and each `tools/call` is its own request, so there is no shared
  state to race. Build the payload from `QueryResult.to_storage()`
  (`backend/app/tools/sql.py:179`); do not write a second serializer. The server
  does not validate `structured_content` against `output_schema`, but SDK
  clients do — declaring a schema is a promise you have to keep.
- `auth.py` — the bearer check, as a small ASGI callable wrapping the SDK's app:
  read `Authorization: Bearer …`, call `apikeys.verify`, call
  `ratelimit.enforce_mcp_rate_limit(key_id)`, then delegate. On failure return a
  bare `401`/`429` **with no `WWW-Authenticate` header naming OAuth resource
  metadata** — clients that see OAuth advertised have been reported to abandon a
  configured static header and go hunting for a login flow. Wrapping is
  preferred over a path-scoped `BaseHTTPMiddleware` on the parent app: it is
  explicit about what it protects, and it cannot be bypassed by a future route.
- `resources.py` — exposes `docs/SCHEMA.md` and `docs/DATASET.md` as MCP
  resources. This is not optional polish. The app's own prompt primes the model
  with these rules, and the `awlevel` rollup trap still shipped a wrong verified
  headline; a caller who gets the tables without the rules will reproduce that
  bug and you will never see it. `Dockerfile:78` copies only `docs/SCHEMA.md`
  into the image — **`docs/DATASET.md` must be added to that COPY in this PR**,
  or the resource is missing in every container build while working fine locally.

Mount it in `backend/app/main.py` **above** the SPA block that starts at line 209
— the any-method `/api/{full_path:path}` 404 at line 217 and the GET
`/{full_path:path}` fallback at line 221 will otherwise swallow `/mcp`.

### Rate limiting

Add `enforce_mcp_rate_limit(key_id)` to `backend/app/ratelimit.py`, a third
function in the same shape as the two that are there (`ratelimit.py:50` and
`:83`): opportunistic delete of old rows, count inside the window, compare
against the cap, insert. Non-positive cap disables it, matching the chat limiter.

Two new settings in `backend/app/config.py`, in the style of
`chat_rate_window_seconds` / `chat_rate_max_per_user` (`config.py:262-263`):

- `mcp_rate_window_seconds: float = 60.0`
- `mcp_rate_max_per_key: int = 60`

**Both must be added to `.env.example` and blanked in `scripts/ci_env.sh` in this
same PR**, pinned to the class defaults. `backend/tests/test_env_example.py` and
`backend/tests/test_ci_env.py` both fail otherwise.

### Tests — `backend/tests/test_mcp.py`

- No `Authorization` header → 401. Wrong key → 401. Revoked key → 401.
- A valid key can list tools and call `run_sql`; the response carries structured
  rows, not a Markdown blob.
- **The 401 response carries no `WWW-Authenticate` resource-metadata header**,
  and `/.well-known/oauth-protected-resource` is not served. This is the
  regression that would silently break every static-key client, and it comes
  back the moment someone passes `auth=` to the SDK.
- **A request whose `Host` header is the public hostname is accepted.** This is
  the DNS-rebinding default described above: it passes on localhost and fails
  behind the proxy, so the test has to set the header explicitly. Without it the
  first real deployment is a 100% failure that no local run reproduces.
- A rejected query (DDL, multiple statements) comes back as a clean tool error,
  not a 500.
- The per-key rate limit returns 429 at the cap.
- The `sqllint` findings appear in the response for a query that trips one.

- `GET /mcp` returns the MCP endpoint, **not the SPA shell**, and `GET /` still
  returns the SPA. This is the `Mount`-vs-`Route` regression; it fails loudly
  the moment someone "tidies" the route into a mount.
- `GET /.well-known/oauth-protected-resource/mcp` is not OAuth metadata.

Tests use the same `TestClient(app)` style as the rest of the suite, and **must**
use it as a context manager — a bare `TestClient(app)` never runs lifespan, so
the session manager never starts. Two existing bare constructions at
`backend/tests/test_backend.py:118` and `:134` need checking against the shim.

Header requirements are stricter than a plain JSON POST. On the modern protocol
leg the transport cross-checks headers against the body: `MCP-Protocol-Version`
must equal `params._meta["io.modelcontextprotocol/protocolVersion"]`, `Mcp-Method`
must equal the body's `method`, and `Mcp-Name` must equal the named parameter for
`tools/call`, `prompts/get`, and `resources/read`. `Accept` must include
`application/json`, and also `text/event-stream` unless `json_response=True`
(`mcp/server/streamable_http.py:505-512`). Write one small `mcp_post()` helper in
the test file rather than hand-assembling headers per case. **Cover both protocol
legs** — omitting `MCP-Protocol-Version` routes to the legacy path, and you do
not control which era a real client speaks. Assert on the JSON-RPC body, not only
the HTTP status: error codes map to non-200 statuses.

Do **not** reach for the SDK's in-memory `Client(server)` helper — it connects
straight to the server object and skips the HTTP layer, which is where the bearer
gate, the route registration, and the middleware stack all live.

---

## Step 3 — the `ask` tool — **DONE**

**PR title:** `feat(mcp): expose the full agent as an ask tool`

One more tool: `ask(question)` runs the agent loop and returns the answer, the
hero figure, and the figure's grounding status. Stateless — nothing is written to
`conversations` or `messages`.

### Pipeline

Reuse the stages `backend/app/routers/chat.py` already runs, in the same order,
minus persistence:

1. `ratelimit.enforce_chat_rate_limit(user_id)` — deliberately the **same**
   limiter and the same table as the web chat, so one person's total spend is
   capped whichever door they came through. The per-key MCP limit from Step 2
   still applies on top.
2. The no-data check (`ipeds_years()`, `chat.py:456`).
3. `guard.classify(question, [])` — non-negotiable. This endpoint is reachable
   from the internet and spends money; the topical guardrail must run before any
   model or tool work, exactly as it does at `chat.py:514`.
4. `skills.cache_lookup(question, user_id)` — a hit costs one guard call instead
   of a full turn.
5. `skills.retrieve_skills_block(question)` + `skills.bump_hits`.
6. `llm.stream_agent(question, skills_block=…)` — consume the async generator to
   completion and take the `done` event's `AgentResult` (`llm.py:1121-1132`).
7. `skills.cache_store(...)` on success, the same call chat makes at `chat.py:720`.
8. One `usage_log` row with `source='mcp'`.

Skipped on purpose, and say so in the docs: conversation titling, the feedback
distiller, and result persistence for cross-turn figure grounding — all three
exist to serve a chat thread that MCP does not have.

### One refactor, and only one

The `usage_log` INSERT lives inside `_persist` (`chat.py:950-957`) with a
22-column tuple. Copying that column list into the MCP path is exactly the
failure this repo has hit before — every hand-maintained duplicate list in it has
drifted. Extract the statement into a helper that takes an open connection, and
have both `_persist` and the MCP path call it. `_persist`'s transaction semantics
must not change: the helper runs inside the same `con`, not on a new one.

### Tests — extend `backend/tests/test_mcp.py`

- A question the guard refuses returns the refusal and **spends nothing beyond
  the guard call** — no agent turn.
- A successful `ask` writes exactly one `usage_log` row with `source='mcp'` and
  **zero** rows in `conversations` and `messages`.
- A cache hit returns the cached answer and does not run the agent.
- With no LLM key configured, `ask` returns a clean tool error (the data tools
  keep working).
- `enforce_chat_rate_limit` is hit by `ask`, proving MCP and chat share the
  per-user budget.

### What Step 3 found that this plan had wrong

**`ask` cannot go through `_call`, and the instruction not to add a second
try/except was still right.** `_call` is synchronous and runs in a worker
thread; `ask` awaits the agent loop, which is an async generator doing its own
thread hops for every blocking step, so it stays on the event loop. What the two
now share is the error SHAPE — `server._tool_error`, one definition, so an
escaped exception comes back as a failed TOOL and not a failed CALL whichever
handler it escaped from. Putting `ask` in a worker thread would have meant
running an event loop inside one.

**How the handler learns who is calling: a context variable, measured twice.**
The MCP tier hands a handler no route back to the HTTP request, and
`ctx.transport.headers` — which looks like the answer, and is what the SDK's own
`TransportContext` documents — **is `None` on both protocol legs here**, because
the session manager builds its context without them. So `app/mcpsrv/auth.py`
sets a `contextvars.ContextVar` after the gate admits a request and `ask` reads
it. That was verified before it was relied on, because both failure modes are
silent and one of them crosses users: the value **does** reach the handler on
both legs, and two overlapping requests on two different keys each saw their own
caller either side of an interleaving await. An unset caller is a REFUSAL, never
a default — there is no safe guess for whose budget a turn spends.

**The usage_log extraction landed in `app/db.py`**, next to the table's own DDL
and alongside `get_meta`/`set_meta`/`data_version`, which are already
connection-taking helpers for one table each. `_persist` passes `source=None`
and keeps its transaction: `record_usage` takes the open connection and does not
commit. No new module was needed.

**A decision this plan never made: `ask` records NO critic-derived lesson.** The
chat path files a critic's finding as an unverified lesson for an admin to
review; `ask` does not, because a key holder reaching this endpoint from the
internet could otherwise steer what lands in that queue by choosing questions,
and the queue is a human's attention. Worth revisiting once an operator can see
MCP traffic; it is not an oversight. Everything else the chat path does and
`ask` skips — titling, the feedback distiller, result persistence — follows from
statelessness rather than being a separate call.

**Six guards were mutation-tested, and the first pass found a vacuous test of my
own.** "An unidentified caller is refused" asserted only `isError`, which this
suite satisfies anyway — with no provider key configured *every* completed `ask`
ends in a tool error. It now asserts the refusal's own wording and runs a
boom-agent, and the mutant dies. The other five (dropping the shared rate limit,
dropping `source='mcp'`, admitting a refused question, caching an answer without
its result rows, un-advertising the tool) each killed exactly their intended
check.

---

## Step 4 — the front end — **DONE**

**PR title:** `feat(ui): API keys page and admin Keys tab`

The frontend is flat `frontend/src/*.jsx` with co-located `lowercase.js` logic
modules, one global `styles.css`, and no CSS framework. There is no account or
settings page today — user-level actions live in the avatar menu
(`frontend/src/UserMenu.jsx:54-76`).

### User-facing

- New route `/keys` in `frontend/src/App.jsx:288-312`, plus a branch in
  `routeAnnouncement` (`App.jsx:34-44`) — `frontend/e2e/route-announcer.spec.js`
  pins that.
- New item in the `UserMenu` items array (`UserMenu.jsx:56-76`), using the `to:`
  form so it renders a real `<Link>`.
- `frontend/src/Keys.jsx` — a `.panel` listing label, `ipeds_mcp_…<last4>`,
  created, last used, with a Revoke action per row. Small list, so plain markup;
  reach for `<DataTable>` only if it grows.
- **The one-shot reveal.** After minting, show the raw key in a dialog with a
  copy button and a plain warning that it will not be shown again. Model it on
  `frontend/src/AboutModal.jsx:33` (a non-action dialog reusing the `.modal-*`
  CSS), not `useConfirm` — this is not a confirmation.
- Revoke goes through `useConfirm` (`frontend/src/ConfirmModal.jsx:262`) with
  `variant: "danger"`, a `successToast`, and an `errorToast`. Never
  `window.confirm`.
- A failed load must render a visible error, not an empty state — use
  `loadErrorMessage` from `frontend/src/authcopy.js:56`, the pattern at
  `frontend/src/admin/Skills.jsx:37-51`.

### Admin

- Append `"keys"` to `ADMIN_TABS` (`frontend/src/Admin.jsx:25`), import the
  component, add the one `{tab === "keys" && …}` line after `Admin.jsx:107`.
- `frontend/src/admin/Keys.jsx` — `<DataTable>` over all keys with owner email,
  following `frontend/src/admin/Allowlist.jsx:929-1035`. Needs a config object
  (`fields`, `comparators`, `tiebreak`, `nouns`) in a co-located
  `frontend/src/admin/keys.js` — see `frontend/src/userlist.js:38-45`.
- Mint-for-a-user form, same one-shot reveal dialog.

### Shared clipboard helper

`copyText` exists three times: `frontend/src/Chat.jsx:56-66`,
`Chat.jsx:105-135`, and `frontend/src/Chart.jsx:263`. A fourth copy for the key
reveal is one too many. Extract it to `frontend/src/clipboard.js` and point the
existing callers at it. Real duplication, already at three, so this is
consolidation rather than speculative sharing.

### API client

Add to the flat map in `frontend/src/api.js:107-181`: `apiKeys()`,
`createApiKey(label)`, `revokeApiKey(id)`, and the three admin equivalents under
the `// admin` comment at line 125. No CSRF token needed — protection is
server-side and Origin-based.

### Frontend tests

- Vitest for pure logic only. **Adding `frontend/src/admin/keys.test.js`
  automatically puts `keys.js` under the per-file 80% line floor**
  (`frontend/vitest.config.js:20-25` derives the gated set from the filesystem).
- Playwright specs in `frontend/e2e/`, all `/api/**` calls mocked through
  `frontend/e2e/mocks.js`:
  - a user mints a key, sees it once, and does not see it after a reload —
    follow the stateful route-mock shape in `frontend/e2e/users-table.spec.js:12-40`;
  - revoke goes through the confirm modal and toasts;
  - the admin Keys tab lists, mints for a user, and revokes.
- **Add both new paths to the scan table at `frontend/e2e/a11y.spec.js:513-521`**,
  which enumerates every admin path in both themes as
  `[path, headingRegex, contentSelector]`. A tab that is not in that table is
  never scanned, and the gate stays green while the page is broken.
- Read `frontend/e2e/README.md`'s "Four traps" section before writing any of it.
  Use `fillStable()` for controlled inputs (`admin-lessons.spec.js:13-20`), and
  open any popover with `focus()`, never `click()`.

### What Step 4 found that this plan had wrong

**`copyText` existed once, not three times.** `Chat.jsx:105` is `copyHtml` and
`Chart.jsx:263` is `copyChart` — different functions that share an `execCommand`
fallback shape, not copies of the same one. So `frontend/src/clipboard.js` holds
the single `copyText`, with `Chat.jsx` and the reveal dialog as its two callers;
`copyHtml` and `copyChart` were left where they are, because things that merely
look alike are not duplication.

**One pure module, not two.** The plan put the DataTable config in
`frontend/src/admin/keys.js`. Both screens need the masking and the ordering, and
a top-level page importing out of `admin/` reads backwards, so it is all one
`frontend/src/apikeys.js` — masking, `isRevoked`, the comparators, and the config
— with `apikeys.test.js` beside it.

**The admin table needed a sixth column and could not afford two 210px
timestamps.** Owner · Label · Key · Created · Last used · Status leaves Owner and
Label under 200px between them inside the 1000px panel. Both date columns
therefore render DATE-ONLY through a new `fmtDay` (the full stamp stays in each
cell's `title`); the user's own page, which has no table to fit, keeps
`fmtDateTime`.

**A real focus defect, caught by its own test.** The mint button was
`disabled={minting}`, and disabling the focused control blurs it to `<body>` —
so `KeyReveal` captured `<body>` as its opener and dismissing the dialog stranded
a keyboard user at the top of the document. It is `aria-disabled` now, with
`create()` early-returning while a mint is in flight; `ConfirmModal.jsx` already
carried the same warning for the same reason.

**The axe scan table is 23 scans, not 19,** and `/keys` is in it despite not
being an admin path — the loop is the only place a page gets scanned at all, and
`adminA11yMocks` signs in as an admin, for whom `/keys` renders identically.

---

## Step 5 — docs, operator wiring, and the agent-definition sweep

**PR title:** `docs: how the MCP endpoint works, and how to put it behind a proxy`

- **`docs/MCP.md`** (new) — what the endpoint is, the tool and resource list, the
  structured result shape, how keys are minted and revoked, the rate limits, and
  the honest OAuth paragraph: static keys work with Claude Code and the Messages
  API connector, not with claude.ai custom connectors, and why we are not
  serving OAuth discovery.
- **Delete `docs/MCP_PLAN.md`.** Its job ends here.
- **`docs/AUTH_AND_SECURITY.md`** — an API-keys section: what is stored, why
  sha256, revocation, and the allowlist re-check.
- **`README.md` → Self-hosting** — the proxy snippet that forwards `/mcp`
  alongside `/api`, and a note that MCP clients need a trusted certificate.
  Include the client-side line a user will actually run:
  `claude mcp add --transport http ipeds https://<host>/mcp --header "Authorization: Bearer ipeds_mcp_…"`.
- **`docs/ARCHITECTURE.md`** — one paragraph and a pointer, not a copy.
- **`CLAUDE.md`** — one row in the "Read before you start" table pointing at
  `docs/MCP.md`. A pointer, not a paragraph; the file has a stated 150-line ceiling.
- **`.claude/agents/architect.md:60`** — currently states as fact that this is
  "embedded tool-calling (not a standalone MCP server)". That becomes false in
  this release. `CLAUDE.md` requires the agent-definition sweep in the same PR
  when it is small, and this one is.
- **`docs/USER_GUIDE.md` / `docs/ADMIN_GUIDE.md`** — how to get a key, and how an
  admin issues or revokes one.

---

## Step 6 — security review, then release

1. Run the `security-reviewer` agent over the whole branch diff. This is an
   internet-reachable authentication surface that spends money; the failure mode
   is a standing credential, not a wrong number. Fix what it finds in a PR on the
   branch.
2. Merge `feat/mcp-server` into `main` via PR. Every check green — that is now
   eight CI jobs plus CodeQL, not the five this plan was written against.
3. Check the code-scanning queue after the merge — findings never block a merge
   and sit silently in the Security tab:
   ```bash
   gh api "repos/toddawhittaker/ipeds-oracle/code-scanning/alerts?state=open" \
     --jq '.[] | "\(.number)\t\(.rule.security_severity_level)\t\(.rule.id)"'
   ```
4. **Regenerate `backend/requirements.lock` and let the audit re-run.** `httpx2`
   2.12.0 was pinned one day after it was published (2026-08-18), on a library
   first released 2026-05-11 — the newest release `pip-compile` could pick, where
   `mcp` only requires `>=2.5.0`. Nothing in the app imports it, so its only live
   exposure until now is the test harness; the tag is the point where that stops
   being true enough to ignore. Regenerating here takes whatever has aged in the
   intervening weeks and re-runs `pip-audit` over the result.
5. Write the `## v0.5.0` entry in `CHANGELOG.md`, seeded from
   `git log --oneline v0.4.0..HEAD`. House structure: narrative prose first, then
   `###` theme sections, opening with `### Read this before upgrading` and
   closing with `### For developers`. The entry becomes the GitHub Release body.
6. `git tag -a v0.5.0` on the merge commit, `git push --no-verify <remote> v0.5.0`,
   then `gh release create`. CI publishes `ghcr.io/toddawhittaker/ipeds-oracle:0.5.0`,
   `:0.5`, and `:latest`, with `APP_VERSION` baked from the tag.

Note for the release: `APP_VERSION` is the answer-cache version key
(`backend/app/skills.py:706-708`), so tagging wipes the cached answers on first
boot after the upgrade. Expected, worth a line in the changelog.

---

## Verification

**Per PR:** `scripts/run_ci_local.sh` (the pre-push hook runs it; `SKIP_E2E=1` to
skip the browser tier while iterating). Authoritative gate is GitHub CI.

**End to end, by hand, after Step 4:**

1. `make up` — starts the app on :8000 with an LLM key and no mail key; sign-in
   links go to the log.
2. Sign in, go to the keys page, mint a key, copy it.
3. Point a real client at it:
   ```bash
   claude mcp add --transport http ipeds http://localhost:8000/mcp \
     --header "Authorization: Bearer ipeds_mcp_…"
   ```
4. In a fresh Claude Code session: ask it to list the data families, then to run
   a query, then to use `ask`. Check the numbers against the same question typed
   into the app's own chat — they must match.
5. Revoke the key in the UI and confirm the client gets 401 on the next call.
6. Open Admin → Usage and confirm the `ask` call shows up and is attributable.

**Do not skip step 4.** One live session with real questions has found defects a
98%-covered suite missed, repeatedly. Use fresh questions each time, and restart
the server after any code change — uvicorn runs without `--reload`, so it serves
boot-time code and will happily give you a stale verdict.
