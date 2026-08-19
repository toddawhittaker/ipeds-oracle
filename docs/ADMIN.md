# Admin areas

## Admin → Imports (dataset management)
- A live **NCES year catalog**: `backend/app/nces.py` probes `nces.ed.gov` (**SSRF-hardened**
  — URLs are built only from a fixed host + template + a validated year) for which
  start years have a Final/Provisional release; an admin multi-selects years to
  fetch + integrate. **Redirects are followed by hand, not by httpx (SEC-6):** the
  client runs `follow_redirects=False` and `_validated_redirect_target` checks each
  hop's host+scheme is `https://NCES_HOST` **before** issuing the next request (a hop
  cap defuses loops), so an intermediate redirect can never make the server request
  an off-host/internal URL — the old final-host-only check (kept as defense-in-depth)
  ran only after that request was already sent. Applies to both `head_release` and
  the streamed `download_zip`.
- Each run is a **full rebuild of the union** of already-integrated and
  newly-picked years (never an incremental merge), through the same **staging-DB +
  integrity-checks + atomic-swap** pipeline as a manual upload. Fetched `.accdb`
  files land in a transient `NCES_WORK_DIR` scratch dir **deleted after every run**,
  success or failure — never a permanent store.
- An integrated year can be **removed** (the "trashcan"): `importer.run_deintegrate`
  runs fully **offline** — copy live→staging, `DELETE` that year's rows everywhere,
  `VACUUM`, its own **`deintegrate_checks`** (deliberately *not* `integrity_checks`,
  whose shrink-detector would falsely fail an intentional removal), then the same
  atomic-swap tail. It never touches the network or mutates live in place, and
  (unlike a rebuild) never invokes the loader subprocess.
- **The swap keeps no `.prev` copy.** `_activate_staging` moves live aside only
  so the two-step move is recoverable if the process dies between them; once
  staging is in place it **deletes** that copy. Nothing used to, so every import
  or year-removal left a full extra ~2 GB dataset on disk, forever. Safe with
  queries in flight — the swap is a rename, so an open connection holds the old
  INODE and unlinking removes the name, not the file. Likewise
  `db._prune_snapshots` caps pre-migration `app.db.pre-v<N>` snapshots at
  `SNAPSHOTS_KEPT` (2). **Scheduled backups are deliberately NOT the app's job**
  — the operator snapshots the bind-mounted volume or crons
  `scripts/backup_app_db.py`; the pre-migration snapshot is an upgrade safety
  net, not a backup, and an in-container run of that script needs
  `BACKUP_DIR=/data/backups` (uid 10001 cannot create its relative default).
- **A manual upload's own files are discarded too**, and `run_import` owns their
  whole lifetime from a `finally` — success or failure, mirroring
  `run_integrate`'s work dir. Previously only the router's `except` unlinked the
  streamed `UPLOAD_DIR` copy, so a SUCCESSFUL import leaked two full 1–3 GB
  Access files: that one, and the `<name>.accdb.bak` a same-year re-upload moves
  aside in `DATA_DIR`. The `DATA_DIR` copy itself stays — it is the loader's
  source and the superset guard reads it. Two guards, both mutation-verified.
  **`UPLOAD_DIR` and `DATA_DIR` are free-form settings that can name the same
  directory**, where the "upload" IS the loader's source, so `_discard_uploads`
  skips an alias — and skips anything it cannot PROVE, since a stray file beats
  deleting the dataset on a wrong guess. Aliasing is decided by
  `_same_file` (device+inode via `os.path.samestat`, which also catches a hard
  link no path normalisation would), whose three-valued answer is load-bearing:
  a MISSING path must answer **False, not None**, or the preflight-failure exit
  — where `data_dir` does not exist yet — would skip the delete and leak every
  upload. And the swap flag is set from **inside `_activate_staging` the instant
  the rename completes**, not from `build_check_swap`'s return: a clean return
  only proves the whole swap TAIL succeeded, and a raise in that tail left the
  flag False, so `_restore_all()` rolled the old `.accdb` back under a live
  database built from the new one.
- A rebuild (manual upload or NCES integrate) streams `scripts/build_ipeds_db.py`'s
  `##PROGRESS##` markers into a determinate rebuild-progress bar on the Imports tab.

## Admin → Usage (privacy)
`GET /api/admin/usage` returns **only aggregates** (totals / series / top_users)
and **deliberately never verbatim question text**. `usage_log.question` is still
written, but echoing it back would be an attributable privacy leak (the
caller-controlled `since`/`until` narrows the window; `top_users` names the user).
A sentinel test in `backend/tests/test_admin_router.py` pins this. The stat cards
are the totals + the observe-only integrity/telemetry rates: **Grounded figures**,
**Grounded cells**, **Answer leaks**, and **Exhausted** — a COUNT (not a rate) of
turns that burned the whole tool budget (`usage_log.exhaustion` NOT NULL), with a
`· N degraded` sub-label for those the S5 grounding gate degraded (see the exhaustion
bullet in [`AGENT_LOOP.md`](AGENT_LOOP.md)). A rising Exhausted count is the signal to lift
`LLM_MAX_TOOL_ITERS`.

**THERE ARE TWO DOORS ONTO THE AGENT, AND `usage_log.source` IS WHAT TELLS THEM
APART.** The web chat is one; the MCP endpoint's `ask` tool (`app/mcpsrv/ask.py`,
migration 37) is the other, and it runs the same pipeline — guard, answer cache,
learned lessons, tool loop, critic, grounding — minus everything that serves a
conversation. It writes `source='mcp'`; a chat turn leaves the column NULL, so
every row predating the MCP endpoint keeps reading as the chat traffic it was.
Two consequences for reading spend: an `ask` turn is a **full-price turn** and
appears in every Usage total alongside chat, and it charges the **same per-user
limiter** (`chat_request_attempts`, `CHAT_RATE_MAX_PER_USER`), so one person's
budget is capped across both doors rather than once each. A key that is spending
is revoked from Admin → Keys, which ends that door without touching the person's
web access. The INSERT itself is `db.record_usage`, one statement both callers
use — a 22-column list hand-copied into the second door is how a billing row
silently starts under-reporting.

**EVERY LLM call a turn causes is billed, not just the agent's.** `usage_log` used
to record only `stream_agent`'s usage, so three probes were invisible: the topical
**guard** (`guard.classify` — runs on EVERY question, *before* the answer cache and
before the agent), the **title** call, and the **feedback distiller**. The guard was
the serious one and an oversight rather than a decision — `Verdict` carried a bare
token count that reached `messages.tokens` on the refusal path and nowhere else, and
on the allowed path the verdict was never referenced again. Consequences worth
knowing: **an answer-cache hit is NOT free** (the guard ran before the lookup, so its
row carries that one call — `cache_hits` is unaffected, it counts rows), and a
**refusal row is real spend** (`model_used='guard'`). Three shapes, three
mechanisms, because two of them have no `AgentResult` to accumulate into: the agent
path does a plain `+=` onto `result`; refusal and cache-hit share
**`_guard_usage_kwargs`**; and the two probes that finish *after* `_persist` has
committed (title, distiller) add to the existing row via **`_add_usage`**
(`UPDATE … cost_estimated = MAX(…)`, so a later estimated call taints an otherwise
billed row and a later billed one can't clear the flag). **`_persist` therefore
returns a THIRD value, the `usage_log` row id.** Two things deliberately NOT done:
the title call is **not** moved ahead of `_persist` — that would put a network probe
in front of the only statement that saves the user's answer, trading data safety for
tidier accounting; and `first_call_*` is **never** touched by a probe, since it
isolates the AGENT's schema-prefix reuse and the guard's prompt is a different
prefix (polluting it would corrupt the Schema-cache stat). One **known gap, and it
is CANCELLATION — not the `result is None` branch** (an earlier comment there named
the wrong cause and has been corrected): when a client disconnects, `gen()` unwinds
at its current `yield`, so `_persist` never runs and the whole turn's spend is lost.
`result is None` is reached only via `stream_agent`'s no-API-key return, where
`guard.classify` short-circuits on the same setting and nothing was spent — every
other exit, transport errors included, yields a terminal `done` carrying the result.
**"Stop generating" is unaffected** (abandon-and-drain: the request completes and
bills). The fix, when worth doing: let the caller supply the `AgentResult` that
`stream_agent` mutates in place, so the (already cancellation-shielded) `finally`
has a live reference to bill from — with a did-we-already-persist guard, or a normal
turn bills twice. A row there would NOT distort `queries`: a cancelled turn is a real
question, unlike a title/feedback probe. The `priceable_turns` /
`estimated_turns` / `cost_warning` predicates dropped their `cached=0` clause as a
direct consequence — it was justified by "a cache hit and a guard refusal spend
nothing", which was never true of the guard and is no longer true of a hit; **tokens
spent** is the honest test, and both halves of the `cost_warning` probe are scoped
identically or a single guard-billed refusal could clear a warning for a window
whose agent turns all recorded `cost=0`. A shared **`llmhttp.Usage`** +
`from_response()` is the one extractor for all five probes (`Critique` and
`_FigureRetry` keep their flat fields and just populate from it — which is what made
adopting it behaviour-neutral, proven by `test_critic.py` passing untouched). Pinned
in `test_chat_router.py` (the three turn shapes + `_add_usage`), `test_guard.py`,
`test_feedback.py`, `test_admin_router.py`.

**Spend is two different numbers, and the tile says which.** `usage_log.cost` holds
either the provider's own per-request charge (OpenRouter reports `usage.cost`) or
**our list-price estimate** for a provider that reports none — DeepSeek direct, and
most self-hosted gateways. `llm.effective_cost` picks; the bill always wins.
**Cached-prefix tokens are priced separately** via `LLM_CACHE_READ_COST_PER_MTOK`:
they are a SUBSET of `prompt_tokens` (DeepSeek's `prompt_cache_hit_tokens +
prompt_cache_miss_tokens == prompt_tokens`), so the uncached count is a
subtraction — no new provider key to read, `llmhttp.cached_tokens()` already
normalizes both shapes. This matters because the app is cache-heavy **by
construction** (the whole schema rides every prompt; measured **77.7%** hit rate),
and a provider discounts a hit steeply (DeepSeek **50×**: $0.0028/M vs $0.140/M) —
so pricing every prompt token at the input rate over-stated spend **5.0×**, measured
against OpenRouter's own billed figure ($3.16 estimated vs $0.63 actual over 307
turns). Tiering takes that to ~1.5×. **It remains an ESTIMATE and an upper bound** —
list prices drift from what a vendor bills, and one price pair covers both
`MODEL_DEFAULT` and `MODEL_ESCALATION`. Two traps: `0` means **not configured**
(prompt tokens priced at the full input rate, reproducing the old number exactly),
**never "cache reads are free"** — reading it that way would silently UNDER-state
spend, the one direction a cost estimate must not err in; and the uncached count is
`max(0, …)`-clamped, since `cached_tokens()`'s `or` chain could otherwise drive it
negative and **credit** the turn. The setting is deliberately **NOT** part of
`admin.py`'s `prices_configured` — it never enables an estimate alone, so including
it could only suppress a true `cost_warning`. Provenance is per-row
(`usage_log.cost_estimated`, **migration 34**, stamped from the shared
`llm.cost_is_estimated`) because it **cannot be derived after the fact**: a
deployment that switches providers has both kinds of row in one window, which no
config-derived boolean can describe. The Usage tile marks an estimate with a leading
**`~`** and carries the split in its label (`Spend · 12 of 40 estimated` —
`usagestats.js`'s `spendEstimated`/`spendLabel`, vitest-pinned); absent counts render
**unmarked**, so an older backend or a fixture never has an "estimated" claim
invented for it. Historical rows keep the cost recorded at the time, so the spend
series **steps** on the day prices change — documented in `ADMIN_GUIDE.md`, not a
bug. Pinned in `test_agent_loop.py` (the `effective_cost`/`cost_is_estimated` block)
+ `test_admin_router.py` + `test_migrations.py`.

**Timezone + per-turn timing (viewer's browser tz EVERYWHERE — see
[[date-formatting-preference]]).** The `/usage` **series buckets in the VIEWER's
timezone**: the browser sends `?tz=<IANA>` (its resolved zone), and the endpoint
aggregates in Python with `zoneinfo` (SQLite can't do IANA zones), falling back to
the `TIMEZONE` config default (`config.resolve_tz`) — the chart's x-axis is
labeled "Time (EST)" (the viewer's short zone). Chat **stamps every user turn**
("2:47 PM EST") and shows **"Thought for N seconds"** under each answer — pure
frontend `Intl` (viewer tz) via `frontend/src/datetime.js`
(`formatStamp`/`shortZone`/`thoughtLabel`, vitest-pinned). The duration is
**server-measured wall-clock** persisted on `messages.duration_ms` (**migration
26**) + carried in the `done` SSE event — it can't come from timestamps because
`_persist` stamps the user AND assistant rows with one `now`. The `TIMEZONE`
`.env` setting is only the graph's if-not-specified fallback (the browser normally
provides its own).
