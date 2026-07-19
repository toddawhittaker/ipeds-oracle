---
name: interactive-testing
description: Run the IPEDS app locally for hands-on / interactive testing via the repo-root Makefile — `make up` (LLM key, NO Resend key; sign-in links go to the log), `make full` (also sends real email), `make down` to stop. Use when asked to start/serve/spin up the app on port 8000 for manual testing, sign in locally, or stop the local server. NOT for automated tests (scripts/run_ci_local.sh) or deployment (docs/DEPLOY.md).
---

# Interactive testing — run the app on :8000

The repo-root **`Makefile`** (a tracked dev-convenience helper) builds the SPA and
runs uvicorn detached on `0.0.0.0:8000`. This is the verified path for hands-on
testing; use it instead of hand-rolling a uvicorn invocation.

## Commands

| Command | What it does |
|---|---|
| `make up` | Build `frontend/dist` + start with the **LLM key** from `.env`, **no Resend key**, `COOKIE_SECURE=false`, `APP_PUBLIC_URL=http://localhost:8000`. Detaches; log → `server.log`. |
| `make full` | Same as `up`, but keeps the **real Resend key** — magic-link / invite emails are actually sent. |
| `make down` | Stop whatever is on `:8000` (port-scoped `fuser -k` — safe alongside worktree servers on other ports). |
| `make status` | Is `:8000` listening? |
| `make logs` | Follow `server.log`. |
| `make build` | Rebuild the SPA only. |

Reachable at `http://localhost:8000` and on the LAN at `http://<lan-ip>:8000`
(bound to `0.0.0.0`). `up`/`full` rebuild the SPA first, so frontend edits show up.

## Signing in (no-email `up` mode)

`make up` runs without a Resend key, so the mailer writes the sign-in link to the
**log** instead of emailing it. Request a link for an **allowlisted** address (e.g.
one of `ADMIN_EMAILS` in `.env`), then read it from `server.log`:

```bash
curl -sS -XPOST localhost:8000/api/auth/request \
  -H 'Content-Type: application/json' -d '{"email":"you@your-institution.edu"}'
grep -Eo 'http://localhost:8000/verify\?token=[^ ]+' server.log | tail -1
```

Open that URL to complete sign-in. An admin (`ADMIN_EMAILS` in `.env`, or an
allowlisted admin already in `app.db`) lands in `/admin`.

## Notes / caveats

- Uses the **real** local `app.db` / `logs.db` and the read-only `ipeds.db` — not throwaway fixtures.
- `make full` sends REAL email, but with `APP_PUBLIC_URL=localhost` links: fine for signing in on the same machine; an external recipient would get a non-routable localhost link.
- Bound to `0.0.0.0` with `COOKIE_SECURE=false` = plaintext http exposed to the LAN. Fine on a trusted network for dev; don't leave it up on an untrusted one.
- Do NOT confuse with the merge gate (`scripts/run_ci_local.sh`) or deployment (Docker — `docs/DEPLOY.md`).
- If you can't use `make` for some reason, the equivalent for `up` is:
  ```bash
  cd frontend && npm run build && cd ../backend && \
  RESEND_API_KEY= COOKIE_SECURE=false APP_PUBLIC_URL=http://localhost:8000 \
  nohup ../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > ../server.log 2>&1 &
  ```
