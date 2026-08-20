# The MCP endpoint

`POST /mcp` is IPEDS Oracle's second front door. It speaks the **Model Context
Protocol** over streamable HTTP, so an MCP client — Claude Code, the Anthropic
Messages API's MCP connector, anything else that can set an `Authorization`
header — reaches the same dataset and the same agent the web chat reaches,
authenticated with a **per-user API key** instead of the browser's session
cookie.

It is mounted inside the existing FastAPI app rather than run as a second
process. The app already hands every blocking query to a worker thread
(`app/llm.py`), so a separate service would buy only crash isolation, at the
price of a second port and two processes migrating one `app.db`.

The code is `backend/app/mcpsrv/`; the credential is `backend/app/apikeys.py`.

## Getting a key

A signed-in user opens the account menu → **API keys** (`/keys`), types an
optional label, and creates one. The raw value — `ipeds_mcp_` followed by 32
random bytes — is shown **exactly once**. Nothing stores it and no later request
can return it, so a lost key is revoked and replaced, never recovered.

An admin can also mint a key for someone else and revoke anyone's, from
**Admin → Keys**. The recipient must already be an allowlisted user who has
signed in at least once: a key is a credential for an existing account, not a
way to create one.

A key carries its owner's access in full. There are **no per-key scopes** —
every key can call every tool as its owner. The way to take one back is to
revoke it, which leaves the person's web access untouched. Removing someone from
the allowlist revokes their keys too, in the same transaction that ends their
sessions: a session and a key are two doors onto the same data, and closing one
is not offboarding.

A key **never expires**, and a key an admin mints for someone sends that person
no mail (unlike an access approval). Both are deliberate for a private
deployment, and together they mean a key created on your behalf is a standing
credential you would only notice on your own `/keys` page — worth knowing before
you decide who mints.

One user may hold **ten live keys** (`routers/keys.py::MAX_ACTIVE_KEYS`);
revoked ones do not count. Minting is otherwise free and charges no rate limit,
and each key carries its own request budget, so an uncapped mint loop is an
uncapped multiple of that budget from one person.

| Endpoint | Who | What |
|---|---|---|
| `GET /api/keys` | any signed-in user | the caller's own LIVE keys (never a secret) |
| `POST /api/keys` | any signed-in user | mint; the response is the only copy of the key |
| `PATCH /api/keys/{id}` | any signed-in user | relabel one of the caller's own LIVE keys; the label is the only editable field |
| `DELETE /api/keys/{id}` | any signed-in user | revoke one of the caller's own — someone else's answers 404, not 403 |
| `GET /api/admin/keys` | admin | every key, with its owner's email |
| `POST /api/admin/keys` | admin | mint for an allowlisted user, recording `created_by` |
| `DELETE /api/admin/keys/{id}` | admin | revoke anyone's; idempotent |
| `POST /api/admin/keys/bulk-action` | admin | revoke a selection of keys in one transaction; reports what it skipped |

## Connecting a client

Claude Code:

```bash
claude mcp add --transport http ipeds https://<host>/mcp \
  --header "Authorization: Bearer ipeds_mcp_…"
```

The Anthropic Messages API's MCP connector, which needs the endpoint to be
reachable from Anthropic's servers (beta flag `mcp-client-2025-11-20`):

```python
client.beta.messages.create(
    model="claude-opus-5", max_tokens=1024,
    betas=["mcp-client-2025-11-20"],
    mcp_servers=[{"type": "url", "url": "https://<host>/mcp",
                  "name": "ipeds", "authorization_token": "ipeds_mcp_…"}],
    tools=[{"type": "mcp_toolset", "mcp_server_name": "ipeds"}],
    messages=[...],
)
```

Anything else that speaks streamable HTTP works the same way: one URL, one
`Authorization: Bearer` header. The scheme name is matched case-insensitively.

Four refusals a client can see before any tool runs: **401** for a key that is
unknown, revoked, or whose owner has left the allowlist (all three are worded
identically on purpose — probing tells an attacker nothing); **405** for any
method other than POST (see *Operating it*); **429** when a rate limit is hit;
**503** while the app is starting, before the transport's lifespan has built the
server. A browser-hosted client sending an `Origin` header is **not** refused —
`/mcp` is exempt from the app's CSRF layer, because a bearer-authenticated
endpoint has no ambient credential for a cross-origin page to borrow.

## What the endpoint exposes

### Tools

The seven data tools are the same specs `backend/app/tools/registry.py` hands
the chat agent, renamed into MCP's shape. There is one definition of what a tool
is and does, so the two surfaces cannot drift.

| Tool | What it does |
|---|---|
| `run_sql` | one read-only `SELECT`/`WITH` against `ipeds.db`, under the same timeout and row cap the chat agent gets |
| `list_families` | the unified tables, their row counts, and the years each covers |
| `get_columns` | a family's column names |
| `describe_variables` | human-readable titles for a family's variables, optionally keyword-filtered |
| `lookup_code` | code → label for a categorical variable (`AWLEVEL`, `CONTROL`, `SECTOR`, …) |
| `find_variable` | search every IPEDS variable by keyword |
| `find_cip` | CIP program codes by program name |
| `ask` | the whole agent: a plain-language question in, a grounded written answer out |

`ask` is appended at the MCP boundary rather than added to the registry, and
that is what makes it safe: the registry is what the chat agent's own tool loop
is handed, so declaring `ask` there would let the agent call the agent.

A tool that fails comes back as a **failed tool result** (`is_error`), never as
a broken call — refusals from the SQL sandbox (`SQL REJECTED`, `SQL TIMEOUT`,
`SQL TOO LARGE`) and any escaped exception both take that path, so a client can
read and retry them.

### Resources

Two documents, which are the rules for reading the tables the tools hand over:

| URI | Name | What |
|---|---|---|
| `ipeds://docs/SCHEMA.md` | `schema-guide` | the data model, join keys, discovery queries, and the aggregation rules — award-level nesting, CIP rollups — that silently produce wrong totals when ignored |
| `ipeds://docs/DATASET.md` | `dataset-guide` | what is actually loaded here: surveys, collection years, how IPEDS codes and labels work, known gaps |

Read `schema-guide` before writing SQL. The app's own system prompt is built
from the same file for the same reason, and the award-level trap still shipped a
confidently wrong headline once.

### Structured results

MCP carries a tool result in two channels: `content` (the Markdown a model
reads) and `structured_content` (real fields for the caller's own code), with
the tool's `output_schema` as the contract between them. An MCP caller should
never have to parse numbers back out of a Markdown table — that parse is exactly
where a digit gets lost.

`run_sql` publishes `columns`, `rows`, `row_count`, `truncated`, `sql`, and
`notes`. Two of those decide whether a number is right: `truncated` means the
row cap cut the result, so an aggregate computed over it is wrong (re-query with
the aggregation in SQL); `notes` carries the SQL linter's query-shape warnings,
such as an award-level rollup that double-counts.

`ask` publishes `answer`, `figure`, `chart`, and `figure_grounding`. The answer's
chart arrives as a **declared field**, not as a fenced block in the middle of
the prose — the web app's ```` ```chart ```` fence is a rendering directive for
the browser and does not travel.

## The `ask` tool

`ask` runs the same pipeline `app/routers/chat.py` runs, in the same order: the
topical guardrail, the answer cache, the learned lessons, the tool loop, the SQL
linter, the critic, and the grounding checks. What it does not do is keep a
conversation.

**It is stateless.** No `conversations` row, no `messages` row, no history, no
follow-up. Each call must carry a complete, self-contained question. Everything
the chat path does to serve a *conversation* is therefore skipped — titling
(there is nothing to name), the feedback distiller (it mines a correction made
on a later turn), and result persistence for cross-turn grounding (the rows are
kept for the next turn, and there is no next turn). `ask` still grounds its own
turn's figure, and reports the outcome in `figure_grounding`.

One omission is **not** a consequence of statelessness and is deliberate: `ask`
records **no critic-derived lesson**. The chat path files a critic's finding as
an unverified lesson for an admin to review; a key holder reaching this endpoint
from the internet would otherwise be able to steer that queue by choosing
questions, and the queue is a human's attention. Chat is the door where a lesson
is earned. Worth revisiting once an operator can see MCP traffic in its own
right.

**A clarifying question comes back as the answer.** When the model judges a
question ambiguous enough that the reading changes the headline, it asks instead
of querying. The web UI renders that as chips to click; here the caller gets the
question as prose and re-asks a narrower one. Such a turn is not cached, exactly
as in chat.

**Spend is recorded on every path that has one** — a guard refusal, a cache
hit's guard call, a full turn — as a `usage_log` row with `source='mcp'`, written
before the answer returns.

Two honest gaps, both shared with the web chat rather than special to this door.
A turn that is **cancelled** before it finishes — the client gave up, or the
process is shutting down — bills nothing, exactly as `routers/chat.py` documents
for a cancelled stream; it matters more here, because MCP clients routinely set
short timeouts. And the billing write itself is **best-effort**: if `app.db` is
locked by a concurrent writer the row is lost and logged rather than taking the
finished answer down with it. Spend stays *bounded* either way, because the
per-user rate limiter is charged first.

`source` is written, but nothing reads it yet: Admin → Usage shows MCP turns in
its totals alongside chat and has no control to separate them. Splitting the two
doors on that screen is filed as follow-up work; until then the separation is a
SQL query away, not a click. See `docs/ADMIN.md`.

## Rate limits and spend

Two limiters, and they stack:

- **Per key**, on every MCP request: `MCP_RATE_MAX_PER_KEY` (default **60** per
  `MCP_RATE_WINDOW_SECONDS`, default 60s), a sliding window over
  `mcp_request_attempts`. Keyed on the key rather than its owner, so a runaway
  scheduled job cannot lock its owner out of their own editor, and revoking a
  leaked key ends its budget along with its access.
- **Per user**, on `ask` only: the same `CHAT_RATE_MAX_PER_USER` limiter
  (default **30/60s**) the web chat charges. One person's spend is capped
  whichever door they came through, not once per door.

A non-positive maximum disables either limiter, which is the off-switch tests
and self-hosters use.

Neither of those is a bound on **concurrency**, so there is a third limit that
is not a rate at all: at most `MCP_TOOL_CONCURRENCY` (8) data-tool calls occupy
the shared worker-thread pool at once. The rate limiters count requests started
per minute, not requests in flight, and the pool they would otherwise flood is
the same 40 threads every synchronous web route runs in — 55 concurrent
`run_sql` calls, comfortably inside the 60/minute ceiling, took `/api/health`
from 0.01s to 22.28s before this existed.

An `ask` turn is a full-price turn and appears in every Admin → Usage total
alongside chat. The data tools cost no provider spend at all — they are SQLite
queries — but they still charge the per-key limit.

## Authentication, and what is stored

An API key reuses `app/security.py`'s primitives, so it has the same strength as
a session token (32 bytes from `secrets.token_urlsafe`) and the same storage
rule: **only the SHA-256 hash is ever written**. A dump of `app.db` mints
nothing. The input is random, not a password, so a slow KDF would add a cost to
every MCP request and buy nothing against an attacker who cannot enumerate the
space anyway.

The `api_keys` row (migration 37) also holds `last4` so a user can tell three
keys apart when revoking one, an optional `label`, `created_by` when an admin
minted it for somebody, `last_used_at`, and `revoked_at`.

- **The `label` is the only editable field.** Everything else is either the
  credential or a record of what happened to it. `PATCH /api/keys/{id}` puts the
  owner check and the live check in the UPDATE itself rather than in a read
  first, so there is no window between "yours and usable" and the write, and one
  refusal covers both — a revoked key answers 404 exactly as somebody else's
  does. There is no admin relabel: a withdrawn key's label is part of the record
  an administrator reads later.

- **`last_used_at` is best-effort** and written at most once a minute
  (`apikeys.TOUCH_INTERVAL_SECONDS`). The column exists so a user can spot a key
  they forgot they had, which needs day resolution; without the floor every call
  would write to `app.db` purely to record that it happened.
- **Revocation sets `revoked_at`; the row survives.** An administrator asking
  what a withdrawn key had access to needs it to still be there. It survives
  where that question gets asked: **Admin → Keys** keeps the row marked
  *Revoked*, while `GET /api/keys` filters it out, because on the owner's own
  page a revoked key is a line with no action left on it.
- **Verification re-checks the allowlist**, the same check
  `auth._user_from_request` makes on a session. Removing someone from the
  allowlist has to end every way they can reach the data, not just the browser
  one, and a key that outlived that check would be a standing grant to someone
  an admin believes they removed.

The gate itself (`app/mcpsrv/auth.py`) is an ASGI wrapper around the transport
app, not a path-scoped middleware: wrapping is explicit about what it protects
and cannot be bypassed by a route added later. It hands the admitted caller to
the tool handlers through a `ContextVar` — the MCP tier gives a handler no route
back to the HTTP request, and `ctx.transport.headers` is `None` on both protocol
legs, so a handler reading it would silently see nobody. An unset caller is a
refusal, never a default: there is no safe guess for whose budget a turn spends.

## OAuth, honestly

The MCP specification says an HTTP server SHOULD implement OAuth 2.1 with RFC
9728 discovery. **This deployment does not, deliberately.** It issues static keys
and runs no authorization server.

What that means in practice: static keys work with **Claude Code** (`--header`)
and with the **Messages API MCP connector**. They do **not** work with
claude.ai custom connectors, which only offer OAuth.

Two things follow, and neither is an oversight to be "fixed": a rejection
carries **no `WWW-Authenticate` header**, and the app serves **no
`/.well-known/oauth-protected-resource`**. Clients that see OAuth resource
metadata advertised have been reported to abandon a perfectly good configured
header and go hunting for a login flow that does not exist. Passing
`auth=`/`token_verifier=` to the SDK's `streamable_http_app()` is what would
bring both back.

## The endpoint is stateless

The server returns **no `mcp-session-id` header**. That is a deliberate profile
of the transport, confirmed on 2026-08-19, not an omission. Three reasons, so
the next reader does not re-derive them:

- **A session id would not give `ask` conversation memory.** It is a routing
  token for server-to-client pushes — progress on a long call, list-changed
  notifications, sampling — none of which this server does. Nothing in the tool
  would read it unless we wrote that code.
- **It would cost per-session state in server memory**, which ends both
  restart-safety and the ability to run more than one process without sticky
  routing or shared state. Today any request can land anywhere and a redeploy
  interrupts nothing.
- **Clients do not require one.** The spec makes sessions optional and describes
  the stateless server explicitly; a client that receives no id does not send
  one. Claude Code and the Messages API connector both handle it.

If follow-ups over MCP are ever wanted, the design is an explicit
`conversation_id` **argument** on `ask`, backed by the rows the chat path
already persists. It works across restarts and processes and does not depend on
transport state. Not a session id.

## Operating it

- **Behind a reverse proxy**, `/mcp` needs to be forwarded like everything else.
  A proxy that forwards the whole site already covers it; one that lists paths
  needs `/mcp` added beside `/api`. See the README's **Self-hosting → HTTPS**.
- **Give the proxy a long read timeout on this path.** With `json_response`
  and no progress notifications, nothing goes over the wire until the whole call
  finishes, so a long `ask` sends no bytes for its entire duration — up to
  `LLM_MAX_TOOL_ITERS` tool calls, each able to hold `SQL_TIMEOUT_SECONDS`.
  nginx's default `proxy_read_timeout` is 60s, which would hand the client a 504
  while the server finishes, caches and bills the answer. The web chat is immune
  because SSE keeps producing bytes; this path is not.
- **An MCP client needs a certificate it trusts.** A browser can be told to
  accept a self-signed certificate; a client library generally cannot, so the
  self-signed posture is a LAN convenience, not a way to serve MCP.
- **DNS-rebinding protection is explicitly off**, and passing that setting at
  all is the load-bearing part. The SDK's `streamable_http_app()` defaults to
  `host="127.0.0.1"` and auto-enables rebinding protection for a loopback host,
  which answers **421 to every request behind a proxy** — and works perfectly on
  localhost, so no local run would ever catch it. The protection also buys
  nothing here: a rebinding attack borrows a victim's *ambient* credentials, and
  this endpoint has none to borrow — every call needs a bearer header an
  attacker's page cannot produce.
- **`/mcp` is exempt from the CSRF layer, deliberately** (`app/main.py` passes
  the path to `CSRFMiddleware`). There is no cookie and no ambient credential, so
  a cross-origin page cannot make an authenticated call and there is nothing for
  that layer to defend. Without the exemption every browser-hosted client — the
  MCP Inspector included — was refused with a 403 about cross-origin requests
  before the bearer gate ran, which is neither true of the request nor
  diagnosable from the client's side.
- **POST only; anything else is a 405.** The route has to accept every method to
  be registered at all (see below), and the SDK's transport answers a GET by
  opening an SSE stream that lives until the client leaves. This server pushes
  nothing, so that stream carries nothing — but the rate limiter charges it once
  when it opens and never sees the hold, which made parked connections free.
- **MCP tool calls are bounded to `MCP_TOOL_CONCURRENCY` at a time**, so MCP
  traffic cannot starve the web app of the shared worker pool. See the rate-limit
  section above.
- **The body cap follows the app-wide one** (`MAX_REQUEST_BODY_MB`, 10 MB), so
  the transport and `app/bodylimit.py` cannot disagree about how big a request
  may be.
- **The route is registered as a `Route`, never a `Mount`.** Starlette compiles
  a Mount's pattern as `path + "/{path:path}"`, so `app.mount("/mcp", …)` would
  not match a bare `/mcp` at all, and the request would fall through to the SPA
  catch-all — POST answering 405 and GET serving the React shell. Nothing
  errors; the endpoint simply stops being there.

## Where the code lives

| File | What |
|---|---|
| `backend/app/mcpsrv/server.py` | the low-level MCP server, its handlers, the transport, and the lifespan |
| `backend/app/mcpsrv/auth.py` | the bearer-key gate, and the caller it admitted |
| `backend/app/mcpsrv/ask.py` | the agent loop as one stateless tool |
| `backend/app/mcpsrv/results.py` | the structured half of a `run_sql` result |
| `backend/app/mcpsrv/resources.py` | `SCHEMA.md` and `DATASET.md` as MCP resources |
| `backend/app/apikeys.py` | minting, verification, revocation |
| `backend/app/routers/keys.py` | a user's own keys over HTTP (admin's are in `routers/admin.py`) |
| `backend/tests/test_mcp.py` | the endpoint's contract, over real HTTP, on both protocol legs |
| `backend/tests/test_api_keys.py` | the credential's contract |
| `frontend/src/Keys.jsx`, `src/admin/Keys.jsx`, `src/KeyReveal.jsx` | the two key screens and the one-shot reveal |

The package is named `mcpsrv`, not `mcp`, on purpose: a local package sharing a
name with the third-party dependency it imports is one accidental relative
import away from a confusing failure.

The low-level `Server` is used rather than the high-level `MCPServer` because
the high-level tier derives each tool's schema from a Python signature, which
would mean re-declaring all seven here and letting them drift from the registry.
Two things that tier does not do for you, and which `server.py` therefore does:
it does not validate arguments against the advertised schema, and an exception
out of a handler becomes a protocol error rather than a tool result. It also
does not move blocking work off the event loop — the data tools run in
`run_in_threadpool`, and `ask` stays on the loop because it awaits an async
generator that does its own thread hops.
