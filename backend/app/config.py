"""Application configuration — all secrets/config come from the environment.

No secrets live in code. Locally, values are read from a `.env` file (see
`.env.example`); in production they come from the container environment /
Docker secrets. `pydantic-settings` validates and types them at startup.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("ipeds.config")

_DEFAULT_TZ = "America/New_York"

# Repo root (…/ipeds). config.py lives in backend/app/, so parents[2] is the
# root — the runtime DBs, data/, and docs/ all resolve relative to it (prod
# overrides each via env; see compose.yaml).
ROOT = Path(__file__).resolve().parents[2]

# The product's display name — used anywhere copy needs a human-facing name
# (transactional emails, the FastAPI app title) instead of a per-install
# setting. config.py is the only app/ module that imports nothing from app,
# so importing this constant elsewhere never risks a cycle.
PRODUCT_NAME = "IPEDS Oracle"

# Values (case-insensitive, whitespace-trimmed) that turn an opt-in string
# setting ON. Everything else — "false"/"f"/"no"/"n"/"0", blank, and any
# unrecognized text — is OFF. Kept as a plain string parse (not a pydantic bool
# field) so an invalid value FAILS SAFE to off rather than raising at startup:
# a bool field would reject "maybe" and crash the app, whereas a privacy flag
# must default to its protective state on anything it doesn't understand.
_TRUTHY = {"true", "t", "yes", "y", "1"}


def is_truthy(value: str) -> bool:
    """True only for an explicit opt-in token; everything else is False."""
    return value.strip().lower() in _TRUTHY


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Paths -------------------------------------------------------------
    ipeds_db_path: Path = Field(default=ROOT / "ipeds.db")
    app_db_path: Path = Field(default=ROOT / "app.db")
    # Persistent server-log store. Defaults to sit beside app.db (see
    # resolved_log_db_path) unless LOG_DB_PATH is set explicitly.
    log_db_path: Path | None = Field(default=None)
    data_dir: Path = Field(default=ROOT / "data")
    upload_dir: Path = Field(default=ROOT / "data" / "uploads")
    schema_md_path: Path = Field(default=ROOT / "docs" / "SCHEMA.md")
    # Scratch space for the NCES year-catalog "integrate" flow: each run
    # downloads+extracts .accdb files here, then deletes the directory
    # (success or failure) — never a permanent store.
    nces_work_dir: Path = Field(default=ROOT / "data" / "work")

    # --- LLM (any OpenAI-compatible provider; OpenRouter by default) -------
    llm_api_key: str = Field(default="")
    llm_base_url: str = Field(default="https://openrouter.ai/api/v1")
    model_default: str = Field(default="deepseek/deepseek-v4-flash")
    model_escalation: str = Field(default="deepseek/deepseek-v4-pro")
    llm_temperature: float = Field(default=0.0)
    llm_max_tool_iters: int = Field(default=20)
    # Fallback token prices (USD per 1,000,000 tokens) for the Usage spend total.
    # Spend normally uses the provider-reported per-request cost (OpenRouter's
    # usage.cost). A provider that doesn't report it leaves cost at 0 → set these to
    # your model's list prices and spend is estimated as
    # prompt_tokens*input + completion_tokens*output (per Mtok). 0 = no estimate
    # (spend stays 0 when the provider is silent). The estimate does NOT discount
    # cached input tokens, so it slightly over-states spend on cache-heavy traffic.
    llm_input_cost_per_mtok: float = Field(default=0.0)
    llm_output_cost_per_mtok: float = Field(default=0.0)
    # Topical input guardrail: a cheap pre-flight classifier refuses off-topic /
    # prompt-injection messages before the agent runs. Set false to disable.
    guard_enabled: bool = Field(default=True)
    # Post-answer critic: after the agent answers from SQL, a cheap review call
    # judges the result for likely aggregation/magnitude errors and, if flagged,
    # drives ONE revision round. Adds a call per data answer. Set false to disable.
    critic_enabled: bool = Field(default=True)
    # Missing-figure retry: when a data-backed answer that should lead with a hero
    # figure emits none (prompt-compliance decays with conversation depth — every
    # prompt-level fix was tried and measured insufficient), one targeted call asks
    # ONLY for the figure fence. A forced figure that can't be grounded against the
    # turn's results is suppressed, not shipped. Adds at most one cheap call per
    # figureless data answer. Set false to disable (and for the on/off A/B).
    figure_retry_enabled: bool = Field(default=True)
    # Structured emission ("structured output, not fenced text"): the model
    # FINISHES a turn by calling an `emit_answer` / `ask_clarification` tool whose
    # fields the provider validates, instead of free-typing ```figure/```chart/
    # ```followups/```clarify fences it can mangle. The server reconstructs
    # WELL-FORMED fences from those validated args, so nothing is manglea​ble and
    # the whole downstream (extract/critic/ground/retry/persist/frontend) is
    # unchanged. **DEFAULT ON** (0.2): dark-shipped in PR-1, adoption tuned in 0.1
    # (reject-and-reprompt), then validated 100%-structured / 0-leaks across FOUR
    # vendors (DeepSeek/MiniMax/Anthropic/Moonshot). A model that can't (or won't)
    # call the tool FALLS BACK to the fence path + the leak sentinel — kept as the
    # safety net for tool-incapable self-hosted models. Set false to force the
    # fence path. Watch Admin → Usage "Answer leaks · N% structured".
    structured_emission_enabled: bool = Field(default=True)
    # public URL used both as the magic-link/invite base (app/mailer.py,
    # app/routers/admin.py) and as the LLM provider attribution header
    # (dual-purpose). `llm_app_title` is the attribution title only; it
    # defaults to PRODUCT_NAME but is a separate, overridable setting because
    # provider attribution headers are optional in general.
    app_public_url: str = Field(default="http://localhost:8000")
    llm_app_title: str = Field(default=PRODUCT_NAME)
    # Server-side FALLBACK timezone (IANA name) — used only to bucket the admin
    # usage graph when a request doesn't carry the viewer's own zone. Display is
    # otherwise the END-USER's browser timezone everywhere (chat stamps + graphs),
    # so this is rarely hit. Read via `tzinfo()`, which degrades an invalid zone to
    # America/New_York rather than 500-ing a request.
    timezone: str = Field(default=_DEFAULT_TZ)
    # Suppresses the chat privacy warning ("don't enter proprietary/confidential
    # info") ONLY. Off by default so the warning always shows unless a deployment
    # has DELIBERATELY judged its provider/contract/deployment/data-use terms safe
    # for non-public data. Stored raw (not a bool field) so an invalid value fails
    # safe to false instead of crashing startup; read via trust_llm_provider_enabled.
    # This flag changes no provider, model, logging, retention, or data-handling
    # behavior — it hides a warning, nothing more.
    trust_llm_provider: str = Field(default="false")

    # --- Query safety ------------------------------------------------------
    sql_timeout_seconds: float = Field(default=25.0)
    sql_row_cap_model: int = Field(default=200)   # rows fed back to the model
    sql_row_cap_download: int = Field(default=100_000)  # rows for CSV export
    max_upload_mb: int = Field(default=2048)  # cap on admin .accdb import uploads

    # --- NCES year-catalog fetch (app/nces.py) ------------------------------
    # The NCES base URL + year bounds are fixed constants in app/nces.py (the
    # SSRF choke point), not config — these are only the operational knobs.
    nces_http_timeout_seconds: float = Field(default=60.0)
    nces_zip_max_mb: int = Field(default=512)     # per-year compressed download cap
    nces_accdb_max_mb: int = Field(default=3072)  # per-year uncompressed extract cap
    nces_total_max_mb: int = Field(default=51200)  # ceiling across one integrate run's union
    # Disk/time preflight estimator (app/estimate.py) calibration knobs — the
    # "how much room/time will this integrate take" math the Imports tab's
    # disk meter and importer.run_integrate's pre-fetch refusal both read.
    nces_accdb_expand_factor: float = Field(default=3.0)  # uncompressed .accdb vs. zip size
    nces_est_bandwidth_mbps: float = Field(default=10.0)  # assumed download speed
    nces_est_build_seconds_per_year: float = Field(default=60.0)  # rebuild time per union year
    nces_default_per_year_db_mb: int = Field(default=380)  # fallback when live_db_bytes is unknown
    nces_download_deadline_seconds: float = Field(default=1800.0)  # per-transfer wall-clock cap
    nces_disk_safety_factor: float = Field(default=1.2)  # pad the estimated need by this much
    nces_probe_concurrency: int = Field(default=5)  # concurrent HEAD probes in probe_catalog
    nces_download_concurrency: int = Field(default=5)  # concurrent fetches in run_integrate

    # --- Server logs -------------------------------------------------------
    log_retention_days: int = Field(default=30)  # older log rows are pruned
    # A hard ceiling on rows, independent of age: retention alone is unbounded
    # WITHIN its window, so a log storm (a retry loop, a chatty dependency) can
    # run the file away in a day and the 30-day sweep won't touch it. Whichever
    # limit bites first wins. 0 disables the cap and leaves age-only pruning.
    log_max_rows: int = Field(default=50_000)

    # --- Auth / sessions ---------------------------------------------------
    session_ttl_days: int = Field(default=30)
    magic_link_ttl_minutes: int = Field(default=15)
    cookie_secure: bool = Field(default=False)     # True in production (HTTPS)
    cookie_name: str = Field(default="ipeds_session")
    admin_emails: str = Field(default="")          # comma-separated bootstrap admins
    # The institution's email domain, e.g. "yourschool.edu". Restricts who may file
    # an ACCESS REQUEST, and supplies the login form's placeholder hint. It does NOT
    # gate sign-in: the allowlist is the sole authority there, so an allowlisted
    # address outside this domain still gets its link. Empty = no restriction.
    email_domain: str = Field(default="")
    # Rate limit on POST /api/auth/request (magic-link / access-request spam).
    auth_rate_window_seconds: float = Field(default=900.0)  # 15-minute sliding window
    auth_rate_max_per_email: int = Field(default=5)
    auth_rate_max_per_ip: int = Field(default=20)
    # How many trusted reverse proxies / tunnels sit in front of the app. The
    # per-IP rate limiter's real client IP comes from X-Forwarded-For, but that
    # header is attacker-controlled — a client can prepend a bogus left-most entry.
    # A trusted reverse proxy/tunnel APPENDS the real peer as the right-most hop,
    # so we read the Nth entry FROM THE RIGHT, where N = this count. 0 (the safe
    # default, and CI's) means "no trusted proxy" → ignore XFF entirely and use
    # the socket peer, so a spoofed header can't split or evade the per-IP bucket.
    # Set to 1 in production behind a single proxy hop (see the README).
    trusted_proxy_count: int = Field(default=0)

    # --- Email --------------------------------------------------------------
    # Which transport delivers the magic-link / access-request / approval emails.
    # "auto" (default) picks resend if a Resend key is set, else smtp if SMTP_HOST
    # is set, else console (log-only, for dev). Force one with resend/smtp/console.
    mail_backend: str = Field(default="auto")     # auto | console | resend | smtp
    mail_from: str = Field(default="IPEDS Oracle <noreply@example.com>")
    # Where "request access" notifications are sent (defaults to first admin).
    access_request_to: str = Field(default="")
    # Resend (a hosted email API — the easiest path for a pilot).
    resend_api_key: str = Field(default="")
    # SMTP (use your own mail: Google Workspace, Microsoft 365, or any relay).
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_username: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_starttls: bool = Field(default=True)     # STARTTLS on 587 (the common case)
    smtp_ssl: bool = Field(default=False)         # implicit TLS on 465 instead
    smtp_timeout: float = Field(default=15.0)

    # --- Embeddings / self-learning ---------------------------------------
    embed_model: str = Field(default="BAAI/bge-small-en-v1.5")
    # Self-learning retrieval master switch: gates BOTH lesson retrieval and the
    # semantic answer cache. Set false for a clean self-learning-off A/B baseline
    # (SKILLS_ENABLED=0 vs 1 over the NL→SQL eval).
    skills_enabled: bool = Field(default=True)
    skill_retrieve_k: int = Field(default=5)
    skill_similarity_floor: float = Field(default=0.35)  # min cos to inject a lesson
    skill_dedup_threshold: float = Field(default=0.92)  # collapse near-duplicate lessons
    cache_similarity_threshold: float = Field(default=0.93)  # reuse SQL above this

    @property
    def trust_llm_provider_enabled(self) -> bool:
        """Resolved boolean the chat UI reads to decide whether to hide the
        privacy warning. False for absent/blank/invalid/false-ish values."""
        return is_truthy(self.trust_llm_provider)

    @property
    def admin_email_list(self) -> list[str]:
        return [e.strip().lower() for e in self.admin_emails.split(",") if e.strip()]

    @property
    def resolved_log_db_path(self) -> Path:
        """Where the server-log store lives — LOG_DB_PATH if set, else next to
        app.db (so a temp APP_DB_PATH in tests keeps logs isolated too)."""
        return self.log_db_path or (self.app_db_path.parent / "logs.db")

    def tzinfo(self) -> ZoneInfo:
        """The configured fallback timezone, degrading an invalid TIMEZONE to
        America/New_York (with a warning) so a typo never 500s a request."""
        return resolve_tz(self.timezone)


def resolve_tz(name: str | None) -> ZoneInfo:
    """An IANA zone name → ZoneInfo, falling back to America/New_York on an empty
    or unknown zone. Used for both the config default and a request's `tz` param,
    so a bad value anywhere degrades instead of erroring."""
    try:
        if name:
            return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone %r; falling back to %s", name, _DEFAULT_TZ)
    return ZoneInfo(_DEFAULT_TZ)


@lru_cache
def get_settings() -> Settings:
    return Settings()
