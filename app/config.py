"""Application configuration — all secrets/config come from the environment.

No secrets live in code. Locally, values are read from a `.env` file (see
`.env.example`); in production they come from the container environment /
Docker secrets. `pydantic-settings` validates and types them at startup.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root (…/ipeds). config.py lives in app/, so parents[1] is the root.
ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Paths -------------------------------------------------------------
    ipeds_db_path: Path = Field(default=ROOT / "ipeds.db")
    app_db_path: Path = Field(default=ROOT / "app.db")
    data_dir: Path = Field(default=ROOT / "data")
    upload_dir: Path = Field(default=ROOT / "data" / "uploads")
    schema_md_path: Path = Field(default=ROOT / "SCHEMA.md")

    # --- LLM (OpenRouter, OpenAI-compatible) -------------------------------
    openrouter_api_key: str = Field(default="")
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1")
    model_default: str = Field(default="deepseek/deepseek-chat")
    model_escalation: str = Field(default="deepseek/deepseek-chat-v3.1")
    llm_temperature: float = Field(default=0.0)
    llm_max_tool_iters: int = Field(default=8)
    # public URL + title used for OpenRouter attribution headers (optional)
    app_public_url: str = Field(default="http://localhost:8000")
    app_title: str = Field(default="IPEDS Query")

    # --- Query safety ------------------------------------------------------
    sql_timeout_seconds: float = Field(default=25.0)
    sql_row_cap_model: int = Field(default=200)   # rows fed back to the model
    sql_row_cap_download: int = Field(default=100_000)  # rows for CSV export
    max_upload_mb: int = Field(default=2048)  # cap on admin .accdb import uploads

    # --- Auth / sessions ---------------------------------------------------
    session_ttl_days: int = Field(default=30)
    magic_link_ttl_minutes: int = Field(default=15)
    cookie_secure: bool = Field(default=False)     # True in production (HTTPS)
    cookie_name: str = Field(default="ipeds_session")
    admin_emails: str = Field(default="")          # comma-separated bootstrap admins

    # --- Email (Resend) ----------------------------------------------------
    resend_api_key: str = Field(default="")
    mail_from: str = Field(default="IPEDS Query <noreply@example.com>")
    # Where "request access" notifications are sent (defaults to first admin).
    access_request_to: str = Field(default="")

    # --- Embeddings / self-learning ---------------------------------------
    embed_model: str = Field(default="BAAI/bge-small-en-v1.5")
    skill_retrieve_k: int = Field(default=5)
    skill_similarity_floor: float = Field(default=0.35)  # min cos to few-shot
    cache_similarity_threshold: float = Field(default=0.93)  # reuse SQL above this

    @property
    def admin_email_list(self) -> list[str]:
        return [e.strip().lower() for e in self.admin_emails.split(",") if e.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
