# shellcheck shell=bash
#
# ci_env.sh — the key-free test environment, in ONE place. Source it; don't run it.
#
#   source "$(dirname "${BASH_SOURCE[0]}")/ci_env.sh"
#
# CI runs with NO .env; a local prod .env (loaded by backend/app/config.py via an
# absolute path, so CWD tricks don't help) bleeds real settings into the tests.
# OS env vars take precedence over the .env file in pydantic-settings, so we
# blank the ones that change behavior to reproduce CI's environment.
#
# WHY THIS IS A SHARED FILE: both run_ci_local.sh (the pre-push gate) and
# coverage_check.sh (called by that gate AND directly by CI) need these blanks.
# When each kept its own copy, the lists silently drifted — coverage_check.sh
# was missing EMAIL_DOMAIN, which no gate could catch: run_ci_local.sh exported
# it before calling coverage_check.sh, and CI has no .env to bleed. It only
# failed for someone running coverage_check.sh directly on a box with a real
# .env, where it looked like a genuine test failure rather than a rig problem.
#
# ADDING A SETTING: any new setting whose production value changes behavior gets
# blanked here, in the same PR that introduces it. This is the only list.
#
#   * COOKIE_SECURE=false — a prod true drops the Secure cookie under the http
#     TestClient -> spurious "Not signed in".
#   * LLM_API_KEY blank — with a key present the chat/agent suites take the LIVE
#     path and hit the fixture's absent tables (e.g. _family_map); CI (no key)
#     takes the deterministic short path.
#   * RESEND_API_KEY blank — so the mailer logs the link instead of sending REAL
#     email through Resend during tests.
#   * EMAIL_DOMAIN blank — a real domain gates access requests to that domain, so
#     test_backend.py's out-of-domain stranger@x.com never records a request row
#     and the suite fails locally while GitHub (no .env) stays green.
#   * TRUST_LLM_PROVIDER blank — a prod true suppresses the chat privacy warning,
#     so test_backend.py's /me trust_llm_provider=False-by-default assertion would
#     fail locally while GitHub (no .env) stays green.
#   * TRUSTED_PROXY_COUNT=0 — a prod 1 makes client_ip trust X-Forwarded-For, so
#     a dev .env would change per-IP rate-limit resolution in the tests vs CI
#     (which has no .env → 0). Explicit 0 pins the key-free/no-proxy behavior.
#   * MAIL_BACKEND + SMTP_HOST blank — with RESEND_API_KEY also blank, the mailer
#     resolves to the console (log-only) backend; a dev .env pointing SMTP_HOST at
#     a real relay would otherwise make the suite attempt a live SMTP send.
#   * LLM_INPUT/OUTPUT_COST_PER_MTOK=0 — a dev .env with real fallback prices would
#     make effective_cost() estimate a nonzero spend where CI (no .env → 0) records
#     0, drifting any cost assertion. Explicit 0 pins the provider-reported-only path.

export COOKIE_SECURE=false
export LLM_API_KEY=""
export RESEND_API_KEY=""
export EMAIL_DOMAIN=""
export TRUST_LLM_PROVIDER=""
export TRUSTED_PROXY_COUNT=0
export MAIL_BACKEND=""
export SMTP_HOST=""
export LLM_INPUT_COST_PER_MTOK=0
export LLM_OUTPUT_COST_PER_MTOK=0
#   * FIGURE_RETRY_ENABLED=false — the missing-figure retry makes a real LLM call
#     when a data answer emits no figure. test_agent_loop.py sets its own test key,
#     so with the retry ON its figureless numeric-answer cases would each attempt a
#     live call (fail-open, but slow + network in CI). Off in the key-free posture;
#     the retry's own tests monkeypatch llm.retry_missing_figure, so they don't
#     depend on this. Prod default is true.
export FIGURE_RETRY_ENABLED=false
#   * CHAT_RATE_MAX_PER_USER — deliberately NOT pinned here, for the same reason as
#     STRUCTURED_EMISSION_ENABLED below. It DEFAULTS to 30 (limiter ON) in config.py,
#     and ci_env.sh does NOT run in CI (CI has no .env → config defaults). The test
#     modules that fire many stream turns for one user pin CHAT_RATE_MAX_PER_USER=0
#     themselves at import (test_chat_router / test_guard / test_security), and
#     test_rate_limit.py sets its own tight cap. Pinning 0 here would let a NEW
#     stream-heavy module pass run_ci_local (limiter off) while failing CI (default
#     30) — the exact masking trap. Leave it unset so local reproduces CI's on-default.
#   * STRUCTURED_EMISSION_ENABLED — deliberately NOT pinned here. It now DEFAULTS
#     ON in config.py, and ci_env.sh does NOT run in CI (CI has no .env → config
#     defaults). Pinning it false here would make run_ci_local diverge from CI
#     (local off, CI on) and MASK failures — which it did once. The fence-path
#     test suites that need it off pin it themselves at import
#     (test_agent_loop.py / test_critic.py: os.environ["STRUCTURED_EMISSION_ENABLED"]
#     = "false"), so they're deterministic on BOTH; the structured tests flip it on
#     per-test. Leaving it unset here lets run_ci_local reproduce CI's on-default.
