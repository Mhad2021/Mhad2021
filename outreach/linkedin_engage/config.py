"""Configuration from the environment. Safe defaults; nothing posts without being told to."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="ENGAGE_", extra="ignore"
    )

    # --- Model --------------------------------------------------------------
    # Opus 5 rejects temperature/top_p/top_k; thinking is adaptive by default.
    model: str = "claude-opus-5"
    max_tokens: int = 4096
    # A violating draft is rewritten from scratch this many times before it is
    # dropped. Dropping is the correct outcome, not a failure.
    max_draft_attempts: int = 3

    # --- Discovery ----------------------------------------------------------
    post_source: Literal["fixture", "crowdreply", "vendor", "browser"] = "fixture"
    fixture_path: str = "fixtures/sample_posts.json"
    fetch_limit: int = 50

    # --- Scoring ------------------------------------------------------------
    min_score: float = 25.0
    max_age_hours: float = 24.0
    author_cooldown_days: int = 14

    # --- Rate limits --------------------------------------------------------
    # Daily cap exists to keep the account alive. Automated commenting at
    # volume is what gets profiles restricted.
    daily_comment_cap: int = 15
    min_seconds_between_comments: int = 240

    # --- Publishing ---------------------------------------------------------
    # Nothing reaches LinkedIn while this is true.
    dry_run: bool = True
    require_approval: bool = True

    # --- Queue --------------------------------------------------------------
    queue_path: str = "queue.jsonl"
    sheet_id: str = Field(default="")
    sheet_tab: str = "Queue"


@lru_cache
def get_settings() -> Settings:
    return Settings()
