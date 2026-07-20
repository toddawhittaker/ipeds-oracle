# IPEDS Oracle — web app deployment

A private, invitation-only web app that lets approved colleagues ask IPEDS
questions in natural language. FastAPI backend + React chat UI, an embedded
tool-calling agent over `ipeds.db` (DeepSeek via OpenRouter), passwordless
magic-link auth, a self-learning skill library, and an admin console for loading
each new IPEDS year. Runs as a single Docker stack on a small VPS.

> For the user-facing overview see [README.md](README.md); for local development
> and the code layout see [CONTRIBUTING.md](CONTRIBUTING.md).

## Architecture

```
Browser ─► Caddy (auto-HTTPS) ─► FastAPI (backend/app/) ─► OpenRouter (DeepSeek + escalation)
                                   │  read-only, immutable ─► ipeds.db  (survey data)
                                   │  read/write           ─► app.db    (users, skills, chats, usage)
                                   └  fastembed (local, CPU) for skill retrieval + semantic cache
```

- **Query safety** (`backend/app/tools/sql.py`): every model query runs on a read-only,
  immutable connection, single-SELECT only, with a watchdog that interrupts any
  query exceeding `SQL_TIMEOUT_SECONDS`.
- **Self-learning** (`backend/app/skills.py`): validated lessons (a generalized
  headline + description + commented SQL example) are retrieved as few-shot
  context; the post-answer critic is the sole source of new (unverified) lessons;
  a semantic cache reuses SQL for near-identical repeat questions and is
  invalidated on each data import.
- **Imports** (`backend/app/importer.py`): reuses `scripts/build_ipeds_db.py` to rebuild
  into a **staging** DB, runs integrity + magnitude checks, then **atomically
  swaps** — the live DB is never written in place. The **Imports** tab's year
  catalog (`backend/app/nces.py`) fetches `.accdb` releases directly from
  `nces.ed.gov` — the only outbound HTTPS dependency this app has — into a
  transient scratch dir, then runs the same staging/checks/swap pipeline over
  the full union of years.

## Prerequisites

- A VPS with Docker + Docker Compose (Hetzner CX22 / Fly / any small box).
- A domain pointed at the server (Cloudflare DNS works; set the record to
  "DNS only" or Full(strict) if proxied).
- An **OpenRouter** API key and a **Resend** API key (+ a verified sending
  domain in Resend).
- **Outbound HTTPS egress to `nces.ed.gov`** — the Imports tab's year catalog
  fetches `.accdb` releases from there directly (`backend/app/nces.py`). If your
  network/firewall only allows specific outbound destinations, allowlist that
  host; nothing else is required (OpenRouter/Resend are already outbound-only
  API calls). Without it, the year-catalog view degrades gracefully (a "could
  not reach NCES" notice with retry) and the manual `.accdb` upload fallback
  still works.

## How releases ship (CI/CD)

The app runs from a **pre-built image**, not a build-on-the-box. CI
(`.github/workflows/ci.yml`, the **image** job) builds the Docker image, boots
it, and smoke-tests `/api/health` on every PR — so a broken build can't land —
and publishes to **GHCR** (`ghcr.io/toddawhittaker/ipeds-ai`) on pushes:

- push to `main` → `:edge` + `:sha-<short>` (the tip, for staging/testing)
- a **`v*` git tag** (a release) → `:vX.Y.Z` + `:X.Y` + `:latest`

Deploy is **pull-on-the-box** (no inbound SSH from GitHub). Cut a release, then
on the VPS run `scripts/deploy.sh` (below) to roll onto it.

```bash
# Cut a release from your workstation once CI is green on main:
git tag v1.2.0 && git push origin v1.2.0     # CI publishes :v1.2.0 + :latest
```

## First deploy

```bash
# 1. Get the code onto the server (compose.yaml, .env, scripts/ — not the image)
git clone <your-repo> ipeds && cd ipeds

# 2. Provide the data volume (NOT in git — too large)
mkdir -p srv-data/accdb srv-data/uploads srv-data/work
cp /path/to/ipeds.db        srv-data/ipeds.db      # the built database (~1.9 GB)
cp /path/to/IPEDS*.accdb    srv-data/accdb/        # source files, for re-imports

# 3. Configure secrets
cp .env.example .env
$EDITOR .env            # LLM_API_KEY, RESEND_API_KEY, ADMIN_EMAILS,
                        # MAIL_FROM, APP_PUBLIC_URL, DOMAIN,
                        # IPEDS_TAG (pin a release, or leave :latest)

# 4. Launch — pulls the published image (GHCR is public-read for this repo's
#    packages; if you made them private, `docker login ghcr.io` first).
docker compose up -d
```

Visit `https://$DOMAIN`. Sign in with an address in `ADMIN_EMAILS` (auto
allowlisted + admin on first boot). Add colleagues under **Admin → Allowlist**.

> No published image yet (before your first CI release)? `docker compose up -d
> --build` still builds locally from source — `compose.yaml` keeps a `build:`
> stanza for exactly this.

## Deploying a new release

```bash
cd /srv/ipeds && git pull        # refresh compose.yaml / scripts if they changed
scripts/deploy.sh v1.2.0         # pin & roll onto an exact release (persists to .env)
scripts/deploy.sh                # or: pull whatever IPEDS_TAG/:latest resolves to
```

`scripts/deploy.sh` pulls the `app` image, recreates just that service, waits for
`/api/health`, and prunes dangling images. Caddy and the data volume are
untouched, so `app.db`/`ipeds.db` survive the swap. Roll back by re-running it
with the previous tag (`scripts/deploy.sh v1.1.0`). To make releases fully
hands-off later, a host cron or systemd timer can call `scripts/deploy.sh` on a
schedule.

> No `ipeds.db` yet? Put the source `.accdb` files in `srv-data/accdb/`, start
> the app, and run the first build with
> `docker compose exec app python scripts/build_ipeds_db.py --data-dir /data/accdb --out /data/ipeds.db`,
> or upload a year through **Admin → Imports** (first import builds from scratch).

## Admin console

Sign in with an `ADMIN_EMAILS` address and open the **Admin** tab:

- **Allowlist** — approve/remove people and act on access requests.
- **Imports** — load a new IPEDS year (below).
- **Usage** — queries, tokens, and **spend** over a chosen range
  (hour / day / 7 / 30 days / custom), charted, with a per‑user breakdown. Spend
  is captured from OpenRouter's per‑request cost.
- **Skills** — review/curate the learned NL→SQL exemplars.
- **Logs** — recent server activity (secrets are scrubbed from this view).

### Adding a new IPEDS year

**Admin → Imports →** the year catalog shows every NCES start year as a card
(already integrated / Final / Provisional / not yet available). Multi-select
one or more years and click **Integrate selected (N)** — it fetches each
`.accdb` from NCES, rebuilds the **full union** of already-integrated years
plus the newly-picked ones into a staging DB, and swaps in only if integrity +
magnitude checks pass. The job streams its log the same way a manual upload
does. The live app keeps serving the old data until the swap; a failed check
(or a failed NCES fetch) leaves it untouched, and the fetched `.accdb`s are
always cleaned up afterward (they're never kept around).

No network access, or you already have the file? The same tab has a collapsed
**manual upload** fallback for a single `IPEDS{YYYY}{YY}.accdb`.

**Disk headroom:** an integrate run needs room for the source zip(s) +
extracted `.accdb`(s) for every year in the union (~1 GB uncompressed per year,
roughly) **plus** the ~1.9 GB staging DB it rebuilds into — size `NCES_WORK_DIR`
and the data volume accordingly if you integrate several years' worth in one
run. The scratch dir is deleted after each run, so this is peak, not steady-state,
usage.

## Configuration (`.env`)

Every setting is listed in [`.env.example`](.env.example); the essentials:

| Key | Purpose |
|-----|---------|
| `LLM_API_KEY` | LLM access (required) |
| `LLM_BASE_URL` | OpenAI-compatible `/chat/completions` endpoint; default OpenRouter (`https://openrouter.ai/api/v1`) |
| `MODEL_DEFAULT` / `MODEL_ESCALATION` | primary + escalation model (defaults: `deepseek/deepseek-v4-flash` → `deepseek/deepseek-v4-pro`; see below) |
| `LLM_MAX_TOOL_ITERS` | max agent tool‑call rounds per question (default 12) |
| `LLM_APP_TITLE` | attribution title sent to the provider (OpenRouter's `X-Title` header) |
| `TRUST_LLM_PROVIDER` | `true` hides the chat's proprietary/confidential-info privacy warning. Default `false` (warning shown). Set `true` **only** once the org has determined its provider, contract, deployment, and data-use terms permit non-public data — it suppresses the warning, it does **not** make the provider trustworthy or change any data handling. True values (case-insensitive): `true`/`t`/`yes`/`y`/`1`; all else false |
| `RESEND_API_KEY` / `MAIL_FROM` | magic-link + access-request email |
| `ADMIN_EMAILS` | comma-separated bootstrap admins (auto-allowlisted) |
| `APP_PUBLIC_URL` | base URL for magic-link/invite emails + the LLM provider's attribution header |
| `DOMAIN` | hostname Caddy obtains a TLS cert for |
| `COOKIE_SECURE` | `true` in production (HTTPS) |
| `TRUSTED_PROXY_COUNT` | number of trusted reverse proxies in front of the app (**`1`** behind the standard single Caddy hop; already pinned in `compose.yaml`). The per-IP auth rate limiter reads the real client from the right-most `X-Forwarded-For` hop, so a client-spoofed header can't evade it. `0` (dev/no proxy) ignores `X-Forwarded-For` and uses the socket peer. Bump if you add another proxy (e.g. Cloudflare) in front of Caddy |
| `EMAIL_DOMAIN` | restrict who may file an access request (blank = anyone). Set it in production so a stranger can't flood admins / burn Resend quota — a real defense alongside `TRUSTED_PROXY_COUNT` for the access-request-spam surface |
| `SQL_TIMEOUT_SECONDS` | per-query watchdog (default 25) |
| `NCES_WORK_DIR` | scratch dir for the Imports year-catalog's fetched `.accdb`s (default `./data/work`; put it on the data volume) |
| `NCES_ZIP_MAX_MB` / `NCES_ACCDB_MAX_MB` / `NCES_TOTAL_MAX_MB` | per-year download/extract caps + a ceiling across one integrate run's whole union (defaults 512 / 3072 / 51200) |

Secrets live only in `.env` (gitignored) / the container environment — never in
code.

> **Model routing:** the app speaks the OpenAI-compatible `/chat/completions`
> API — `LLM_BASE_URL` selects the provider (default OpenRouter). We default to
> `deepseek/deepseek-v4-flash` (cheap) and auto-escalate to
> `deepseek/deepseek-v4-pro` on repeated SQL errors / failed magnitude checks;
> both pass `backend/tests/eval_nl2sql.py` (flash 3/3 with no escalation, ~3x cheaper per
> query). If your account can't route them, set `MODEL_DEFAULT`/`MODEL_ESCALATION`
> to models your provider can. Watch the escalation rate in the admin usage
> dashboard — if flash escalates on a large fraction of real queries, reconsider
> the split.
>
> **If you're on OpenRouter:** it enforces any "Allowed Providers" allowlist on
> your account. If a model's live providers aren't on that list you get a 404
> ("No allowed providers are available") — even for a model whose name matches an
> allowed provider. Relax the allowlist (add DeepInfra / Novita) or swap the
> model ID if your account can't route it.
>
> **Caveat:** two OpenRouter extensions to the `/chat/completions` response
> degrade silently on other providers, by design — neither errors, both just go
> quiet. The admin Usage tab's **spend** column reads the non-standard
> `usage.cost` field, so spend reads $0.00 elsewhere (tokens/queries still track
> correctly). The chat's **thinking** indicator reads the non-standard
> `message.reasoning` field, so it simply never appears elsewhere.

## Backups

`app.db` holds the irreplaceable state (users, skills, chat history); `ipeds.db`
is rebuildable from `srv-data/accdb/`. `scripts/backup_app_db.py` takes a
consistent online snapshot (WAL-safe, no downtime), prunes to the most recent N,
and — if `R2_REMOTE` is set (an `rclone` remote:path) — uploads it off-site.

```bash
# host crontab; needs the venv + (for off-site) rclone with an R2 remote configured
0 3 * * *  APP_DB_PATH=/srv/ipeds/srv-data/app.db R2_REMOTE=r2:ipeds-backups \
           /srv/ipeds/.venv/bin/python /srv/ipeds/scripts/backup_app_db.py --keep 30
```

Configure the R2 remote once with `rclone config` (New remote → S3 → provider
Cloudflare R2 → your R2 access key/secret + account endpoint). Without
`R2_REMOTE` the backup is local-only under `backups/`.

### Restore drill

Practice this so a real restore is muscle memory. `restore_app_db.py` validates
the backup (integrity check + expected tables), snapshots the current file to
`app.db.pre-restore-<ts>`, then swaps the backup in.

```bash
# 1. Stop the app so nothing is writing app.db.
docker compose down            # or: systemctl stop ipeds

# 2. (If restoring from R2) pull the chosen backup down first.
rclone copy r2:ipeds-backups/app-20260714-030000.db ./backups/

# 3. Restore (validates + snapshots the current file first; needs --yes).
APP_DB_PATH=/srv/ipeds/srv-data/app.db \
  .venv/bin/python scripts/restore_app_db.py backups/app-20260714-030000.db --yes

# 4. Start the app and confirm a known user can log in and see their history.
docker compose up -d
```

If the restore looks wrong, the pre-restore snapshot lets you roll straight back.

## Development (without Docker)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
cp .env.example .env && $EDITOR .env        # at least LLM_API_KEY, ADMIN_EMAILS
.venv/bin/uvicorn app.main:app --reload     # API on :8000
cd frontend && npm install && npm run dev         # UI on :5173 (proxies /api → :8000)
```

## Tests / eval

```bash
.venv/bin/python backend/tests/test_sql_guards.py     # SQL safety + timeout (no key needed)
.venv/bin/python backend/tests/test_backend.py        # auth, admin, skills, cache, CSV (no key)
.venv/bin/python backend/tests/eval_nl2sql.py         # full NL→SQL accuracy (needs LLM_API_KEY)
```

`backend/tests/eval_nl2sql.py` doubles as the regression gate when swapping models — it
checks known answers (e.g. CA public CS bachelor's = 7,679).
