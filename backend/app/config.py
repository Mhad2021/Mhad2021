"""Application configuration loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Core ---------------------------------------------------------------
    app_name: str = "Presence"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    base_url: str = "http://localhost:8000"

    # --- Database -----------------------------------------------------------
    database_url: str = "postgresql+psycopg://presence:presence@localhost:5432/presence"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # --- Security -----------------------------------------------------------
    # Used to sign JWTs. MUST be overridden in production.
    secret_key: str = Field(default="CHANGE-ME-IN-PRODUCTION-32-CHARS-MIN")
    # Used to encrypt integration credentials at rest (Fernet key).
    encryption_key: str = Field(default="")
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14
    agent_token_ttl_days: int = 90
    password_min_length: int = 10
    max_failed_logins: int = 8
    lockout_minutes: int = 15

    # --- Monitoring defaults (overridable per-org in the settings table) -----
    monitor_interval_seconds: int = 30
    sse_poll_seconds: int = 5

    # --- Server -------------------------------------------------------------
    # NoDecode is required: without it pydantic-settings JSON-decodes list
    # fields straight from the environment and rejects a comma-separated
    # value before the validator below ever runs.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    trusted_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["*"]
    )
    session_cookie_name: str = "presence_session"
    secure_cookies: bool = False

    # --- Notifications ------------------------------------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "presence@example.com"
    smtp_use_tls: bool = True
    notification_worker_interval_seconds: int = 10
    notification_max_attempts: int = 5

    @field_validator("cors_origins", "trusted_hosts", mode="before")
    @classmethod
    def _split_csv(cls, v):
        """Accept a comma-separated string, a JSON array, or a real list.

        The usual form is plain and comma-separated:
            TRUSTED_HOSTS=presence.example.com,presence.internal
        but some deployment tools render list values as JSON, so both load.
        """
        if not isinstance(v, str):
            return v

        value = v.strip()
        if value.startswith("["):
            import json

            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]

        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
