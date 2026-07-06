"""
Configuration management with environment variables.
Loads from .env file and provides typed settings.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(default="Prompt2PBI", validation_alias="APP_NAME")
    app_version: str = Field(default="1.0.0", validation_alias="APP_VERSION")
    app_env: str = Field(default="development", validation_alias="APP_ENV")
    debug: bool = Field(default=True, validation_alias="APP_DEBUG")
    api_host: str = Field(default="0.0.0.0", validation_alias="API_HOST")
    api_port: int = Field(default=8000, validation_alias="API_PORT")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_dir: Path = Field(default=Path("./output/logs"), validation_alias="LOG_DIR")
    log_file: str = Field(default="prompt2pbi.log", validation_alias="LOG_FILE")
    log_max_bytes: int = Field(default=5 * 1024 * 1024, validation_alias="LOG_MAX_BYTES")
    log_backup_count: int = Field(default=3, validation_alias="LOG_BACKUP_COUNT")

    upload_dir: Path = Field(default=Path("./data/uploads"), validation_alias="UPLOAD_DIR")
    output_dir: Path = Field(default=Path("./output"), validation_alias="OUTPUT_DIR")
    sample_data_dir: Path = Field(
        default=Path("./data/sample_data"), validation_alias="SAMPLE_DATA_DIR"
    )

    max_file_size: int = Field(default=10 * 1024 * 1024, validation_alias="MAX_FILE_SIZE")
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [".csv", ".xlsx", ".xls", ".json"],
        validation_alias="ALLOWED_EXTENSIONS",
    )
    cors_origins: list[str] = Field(
        default_factory=lambda: ["*"],
        validation_alias="CORS_ORIGINS",
    )

    redis_url: str = Field(default="redis://localhost:6379/0", validation_alias="REDIS_URL")
    llm_provider: Literal["gemini", "openai", "auto"] = Field(
        default="gemini", validation_alias="LLM_PROVIDER"
    )
    llm_model: str = Field(default="gemini-1.5-flash", validation_alias="LLM_MODEL")
    llm_timeout_seconds: int = Field(default=30, validation_alias="LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=3, validation_alias="LLM_MAX_RETRIES")
    llm_cache_ttl_seconds: int = Field(
        default=3600, validation_alias="LLM_CACHE_TTL_SECONDS"
    )
    llm_circuit_breaker_threshold: int = Field(
        default=3, validation_alias="LLM_CIRCUIT_BREAKER_THRESHOLD"
    )
    llm_circuit_breaker_cooldown_seconds: int = Field(
        default=120, validation_alias="LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS"
    )
    rate_limit_requests: int = Field(default=60, validation_alias="RATE_LIMIT_REQUESTS")
    rate_limit_window_seconds: int = Field(
        default=60, validation_alias="RATE_LIMIT_WINDOW_SECONDS"
    )
    job_timeout_seconds: int = Field(default=120, validation_alias="JOB_TIMEOUT_SECONDS")
    app_api_key: SecretStr | None = Field(default=None, validation_alias="APP_API_KEY")

    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    power_bi_client_id: SecretStr | None = Field(
        default=None, validation_alias="POWER_BI_CLIENT_ID"
    )
    power_bi_client_secret: SecretStr | None = Field(
        default=None, validation_alias="POWER_BI_CLIENT_SECRET"
    )
    power_bi_tenant_id: SecretStr | None = Field(
        default=None, validation_alias="POWER_BI_TENANT_ID"
    )

    @field_validator("allowed_extensions", "cors_origins", mode="before")
    @classmethod
    def _parse_list_like_values(cls, value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed if str(item).strip()]
                except json.JSONDecodeError:
                    pass
            return [item.strip() for item in raw.split(",") if item.strip()]
        return [str(value).strip()]

    def ensure_directories(self) -> None:
        """Create runtime directories if they do not exist."""

        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.sample_data_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""

    settings = Settings()
    settings.ensure_directories()
    return settings
