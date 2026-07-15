# IPEDS Query — web app deployment

A private, invitation-only web app that lets approved colleagues ask IPEDS
questions in natural language. FastAPI backend + React chat UI, an embedded
tool-calling agent over `ipeds.db` (DeepSeek via OpenRouter), passwordless
magic-link auth, a self-learning skill library, and an admin console for loading
each new IPEDS year. Runs as a single Docker stack on a small VPS.

> For the user-facing overview see [README.md](README.md); for local development
> and the code layout see [CONTRIBUTING.md](CONTRIBUTING.md).

## Architecture

```
Browser ─► Caddy (auto-HTTPS) ─► FastAPI (app/) ─► OpenRouter (DeepSeek + escalation)
                                   │  read-only, immutable ─► ipeds.db  (survey data)
                                   │  read/write           ─► app.db    (users, skills, chats, usage)
                                   └  fastembed (local, CPU) for skill retrieval + semantic cache
```

- **Query safety** (`app/tools/sql.py`): every model query runs on a read-only,
  immutable connection, single-SELECT only, with a watchdog that interrupts any
  query exceeding `SQL_TIMEOUT_SECONDS`.
- **Self-learning** (`app/skills.py`): validated NL→SQL exemplars are retrieved
  as few-shot context; 👍 promotes new ones; a semantic cache reuses SQL for
  near-identical repeat questions and is invalidated on each data import.
- **Imports** (`app/importer.py`): reuses `scripts/build_ipeds_db.py` to rebuild
  into a **staging** DB, runs integrity + magnitude checks, then **atomically
  swaps** — the live DB is never written in place.

## Prerequisites

- A VPS with Docker + Docker Compose (Hetzner CX22 / Fly / any small box).
- A domain pointed at the server (Cloudflare DNS works; set the record to
  "DNS only" or Full(strict) if proxied).
- An **OpenRouter** API key and a **Resend** API key (+ a verified sending
  domain in Resend).

## First deploy

```bash
# 1. Get the code onto the server
git clone <your-repo> ipeds && cd ipeds

# 2. Provide the data volume (NOT in git — too large)
mkdir -p srv-data/accdb srv-data/uploads
cp /path/to/ipeds.db        srv-data/ipeds.db      # the built database (~1.9 GB)
cp /path/to/IPEDS*.accdb    srv-data/accdb/        # source files, for re-imports

# 3. Configure secrets
cp .env.example .env
$EDITOR .env            # OPENROUTER_API_KEY, RESEND_API_KEY, SESSION_SECRET,
                        # ADMIN_EMAILS, MAIL_FROM, APP_PUBLIC_URL, DOMAIN
#   Generate a session secret:
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
#   Add DOMAIN=ipeds.yourschool.edu to .env (used by Caddy)

# 4. Launch
docker compose up -d --build
```

Visit `https://$DOMAIN`. Sign in with an address in `ADMIN_EMAILS` (auto
allowlisted + admin on first boot). Add colleagues under **Admin → Allowlist**.

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

**Admin → Imports →** upload `IPEDS{YYYY}{YY}.accdb`. The job streams its log,
runs checks, and swaps the new database in only if they pass. The live app keeps
serving the old data until the swap; a failed check leaves it untouched.

## Configuration (`.env`)

Every setting is listed in [`.env.example`](.env.example); the essentials:

| Key | Purpose |
|-----|---------|
| `OPENROUTER_API_KEY` | LLM access (required) |
| `MODEL_DEFAULT` / `MODEL_ESCALATION` | primary + escalation model (defaults: `deepseek/deepseek-v4-flash` → `deepseek/deepseek-v4-pro`; see below) |
| `LLM_MAX_TOOL_ITERS` | max agent tool‑call rounds per question (default 12) |
| `RESEND_API_KEY` / `MAIL_FROM` | magic-link + access-request email |
| `SESSION_SECRET` | signs session cookies (set a long random value) |
| `ADMIN_EMAILS` | comma-separated bootstrap admins (auto-allowlisted) |
| `APP_PUBLIC_URL` | base URL for links + OpenRouter attribution |
| `DOMAIN` | hostname Caddy obtains a TLS cert for |
| `COOKIE_SECURE` | `true` in production (HTTPS) |
| `SQL_TIMEOUT_SECONDS` | per-query watchdog (default 25) |

Secrets live only in `.env` (gitignored) / the container environment — never in
code.

> **Model routing:** OpenRouter enforces any "Allowed Providers" allowlist on
> your account. If a model's live providers aren't on that list you get a 404
> ("No allowed providers are available") — even for a model whose name matches an
> allowed provider. We default to `deepseek/deepseek-v4-flash` (cheap) and
> auto-escalate to `deepseek/deepseek-v4-pro` on repeated SQL errors / failed
> magnitude checks; both pass `eval/eval_nl2sql.py` (flash 3/3 with no escalation,
> ~3x cheaper per query). If your account can't route them, either relax the
> allowlist (add DeepInfra / Novita) or set `MODEL_DEFAULT`/`MODEL_ESCALATION` to a
> model it can. Watch the escalation rate in the admin usage dashboard — if flash
> escalates on a large fraction of real queries, reconsider the split.

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
cp .env.example .env && $EDITOR .env        # at least OPENROUTER_API_KEY, ADMIN_EMAILS
.venv/bin/uvicorn app.main:app --reload     # API on :8000
cd web && npm install && npm run dev         # UI on :5173 (proxies /api → :8000)
```

## Tests / eval

```bash
.venv/bin/python eval/test_sql_guards.py     # SQL safety + timeout (no key needed)
.venv/bin/python eval/test_backend.py        # auth, admin, skills, cache, CSV (no key)
.venv/bin/python eval/eval_nl2sql.py         # full NL→SQL accuracy (needs OPENROUTER_API_KEY)
```

`eval/eval_nl2sql.py` doubles as the regression gate when swapping models — it
checks known answers (e.g. CA public CS bachelor's = 7,679).
