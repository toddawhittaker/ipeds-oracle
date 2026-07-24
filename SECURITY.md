# Security Policy

IPEDS Oracle is a **self-hosted** web application. There is no hosted instance to
attack; each operator runs their own copy. Even so, the app handles
authentication (passwordless magic links), sessions, an allowlist, and executes
model-generated SQL against a read-only database, so we take security seriously.

## Reporting a vulnerability

**Please report security issues privately — do not open a public issue.**

- Preferred: use GitHub's **[Report a vulnerability](../../security/advisories/new)**
  (Security → Advisories) to open a private advisory.
- Alternatively, email **todd@thewhittakers.org** with details.

Please include enough to reproduce: affected version/commit, the endpoint or flow,
and a proof of concept if you have one. We'll acknowledge on a best-effort basis
and work with you on a fix and coordinated disclosure. This is a small,
volunteer-maintained project — there is no bug-bounty program.

## Scope

Most relevant areas: the magic-link auth and session handling (`backend/app/auth.py`,
`security.py`), the allowlist / access-request flow, the per-IP (auth) and
per-user (chat) rate limiting and `X-Forwarded-For` handling (`ratelimit.py`), the
CSRF and security-headers middleware (`csrf.py`, `secheaders.py`), and the
read-only SQL execution sandbox the agent uses (`tools/sql.py`, `tools/sqllint.py`).

## For operators

Because you run your own instance, a few things are on you:

- Keep dependencies current (the repo ships Dependabot config and pinned locks).
- Terminate TLS (a reverse proxy, a tunnel, or the built-in self-signed option)
  and keep `COOKIE_SECURE=true` — see the README's **Self-hosting** section.
- Set `TRUSTED_PROXY_COUNT` to match your ingress so the rate limiter can't be
  spoofed via `X-Forwarded-For`.
- Keep `LLM_API_KEY` / `RESEND_API_KEY` / SMTP credentials in `.env` only (it is
  gitignored) — never commit them.
- Cap runaway spend from a compromised or scripted account with the per-user chat
  throttle (`CHAT_RATE_MAX_PER_USER` / `CHAT_RATE_WINDOW_SECONDS`).
- The app makes one outbound call to GitHub to check for a newer release (cached,
  fails open). Set `UPDATE_CHECK_ENABLED=false` if you want zero outbound calls
  beyond the LLM/email providers you configure.
