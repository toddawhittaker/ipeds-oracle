# Local interactive-testing runner for the IPEDS app.
#
# A repo-root developer convenience (tracked). It is NOT the deployment path
# (self-hosting runs via Docker — see the README) and NOT the test gate
# (scripts/run_ci_local.sh). It just builds the SPA and runs uvicorn detached on
# 0.0.0.0:8000 for hands-on testing. See .claude/skills/interactive-testing.
#
#   make up      Build the SPA + start the server  (LLM key, NO resend key)
#   make full    Same as `up`, but WITH the resend key (real emails are sent)
#   make down    Stop whatever is listening on :8000
#   make status  Show whether :8000 is listening
#   make logs    Follow the server log (server.log)
#   make build   Rebuild the SPA only (frontend/dist)
#
# Both `up` and `full` bind 0.0.0.0:8000, keep LLM_API_KEY from .env, set
# COOKIE_SECURE=false (a Secure cookie can't persist over plain http), and point
# APP_PUBLIC_URL at http://localhost:8000 so sign-in links work locally.
#   up   blanks RESEND_API_KEY  -> no email; the mailer writes sign-in links to
#        the log (grep server.log — see `make logs`).
#   full leaves the real RESEND_API_KEY in place -> magic-link / invite emails
#        are actually sent (with localhost links — good for same-machine sign-in).

PORT    ?= 8000
ROOT    := $(CURDIR)
UVICORN := $(ROOT)/.venv/bin/uvicorn
LOG     := $(ROOT)/server.log          # gitignored via *.log

.PHONY: up full down status logs build

build:
	cd frontend && npm run build

up: build down
	@cd backend && RESEND_API_KEY= COOKIE_SECURE=false APP_PUBLIC_URL=http://localhost:$(PORT) \
	  nohup $(UVICORN) app.main:app --host 0.0.0.0 --port $(PORT) > $(LOG) 2>&1 & \
	  echo "up:   http://localhost:$(PORT) (0.0.0.0) — LLM key, NO resend key — logs: make logs"

full: build down
	@cd backend && COOKIE_SECURE=false APP_PUBLIC_URL=http://localhost:$(PORT) \
	  nohup $(UVICORN) app.main:app --host 0.0.0.0 --port $(PORT) > $(LOG) 2>&1 & \
	  echo "full: http://localhost:$(PORT) (0.0.0.0) — LLM key + REAL resend key (emails send) — logs: make logs"

down:
	@fuser -k $(PORT)/tcp >/dev/null 2>&1 && echo "down: stopped server on :$(PORT)" || echo "down: nothing on :$(PORT)"
	@for i in 1 2 3 4 5 6 7 8 9 10; do ss -ltn 2>/dev/null | grep -q ':$(PORT) ' || break; sleep 0.2; done

status:
	@ss -ltn 2>/dev/null | grep -q ':$(PORT) ' && echo ":$(PORT) LISTENING" || echo ":$(PORT) not listening"

logs:
	@tail -n 40 -f $(LOG)
