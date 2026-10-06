"""
Enterprise SQL Agent — Centralized Configuration.

All environment variables, model IDs, resource limits, and feature flags
are defined here as typed, validated Pydantic Settings. Values are loaded
from .env files with sensible defaults for local development.

Usage:
    from src.config import get_settings
    settings = get_settings()
    print(settings.tier1_model)
"""

from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Environment(str, Enum):
    """Deployment environment."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """
    Centralized, typed configuration for the entire agent system.

    Every subsystem reads from this single source of truth.
    Secrets (API keys) are loaded from environment / .env file and
    never logged or serialized.
    """

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"

    api_key: Optional[str] = Field(default=None, repr=False)
    openrouter_api_key: Optional[str] = Field(default=None, repr=False)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    enable_reasoning: bool = True
    openai_api_key: Optional[str] = Field(default=None, repr=False)
    anthropic_api_key: Optional[str] = Field(default=None, repr=False)
    gemini_api_key: Optional[str] = Field(default=None, repr=False)

    tier1_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    tier1_fallback_model: str = "nvidia/nemotron-3.5-lightning:free"
    tier1_max_latency_ms: int = 15000
    ollama_api_base: str = "http://localhost:11434"

    tier2_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    tier2_fallback_model: str = "nvidia/nemotron-3.5-lightning:free"
    fallback_models_pool: list[str] = [
        "nvidia/nemotron-3.5-lightning:free",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    ]

    @property
    def effective_openrouter_key(self) -> Optional[str]:
        """Return OpenRouter API key from openrouter_api_key, api_key, or env."""
        return (
            self.openrouter_api_key
            or self.api_key
            or os.getenv("OPENROUTER_API_KEY")
            or os.getenv("API_KEY")
        )

    database_path: Path = Field(default=PROJECT_ROOT / "data" / "analytics.duckdb")
    db_memory_limit: str = "512MB"
    db_max_threads: int = 2
    db_query_timeout_seconds: float = 5.0
    db_max_result_rows: int = 5001

    router_confidence_threshold: float = 0.65
    schema_linker_top_k: int = 5
    embedding_model: str = "all-MiniLM-L6-v2"

    max_retries: int = 2

    nemo_config_path: Path = Field(
        default=PROJECT_ROOT / "src" / "guardrails" / "nemo_config"
    )

    report_output_dir: Path = Field(default=PROJECT_ROOT / "outputs" / "reports")
    chart_output_dir: Path = Field(default=PROJECT_ROOT / "outputs" / "charts")

    redis_url: str = "redis://localhost:6379/0"
    semantic_cache_similarity_threshold: float = 0.92

    langfuse_public_key: Optional[str] = Field(default=None, repr=False)
    langfuse_secret_key: Optional[str] = Field(default=None, repr=False)
    langfuse_host: str = "https://cloud.langfuse.com"

    @field_validator("db_query_timeout_seconds")
    @classmethod
    def validate_timeout(cls, v: float) -> float:
        if v <= 0 or v > 30:
            raise ValueError("Query timeout must be between 0 and 30 seconds")
        return v

    @field_validator("db_max_result_rows")
    @classmethod
    def validate_max_rows(cls, v: int) -> int:
        if v < 1 or v > 50_000:
            raise ValueError("Max result rows must be between 1 and 50,000")
        return v

    @field_validator("router_confidence_threshold")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        if v < 0.0 or v > 1.0:
            raise ValueError("Confidence threshold must be between 0.0 and 1.0")
        return v

    @field_validator("semantic_cache_similarity_threshold")
    @classmethod
    def validate_cache_threshold(cls, v: float) -> float:
        if v < 0.5 or v > 1.0:
            raise ValueError("Cache similarity threshold must be between 0.5 and 1.0")
        return v

    def ensure_output_dirs(self) -> None:
        """Create output directories if they don't exist."""
        self.report_output_dir.mkdir(parents=True, exist_ok=True)
        self.chart_output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_development(self) -> bool:
        return self.environment == Environment.DEVELOPMENT

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION

    def get_litellm_env(self) -> dict[str, str]:
        """Return environment variables needed by LiteLLM."""
        env: dict[str, str] = {}
        if self.openai_api_key:
            env["OPENAI_API_KEY"] = self.openai_api_key
        if self.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = self.anthropic_api_key
        env["OLLAMA_API_BASE"] = self.ollama_api_base
        return env


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Singleton settings accessor.

    Cached so that repeated calls don't re-parse the .env file.
    Use dependency injection in FastAPI; direct import elsewhere.
    """
    settings = Settings()

    for key, value in settings.get_litellm_env().items():
        os.environ.setdefault(key, value)

    return settings
