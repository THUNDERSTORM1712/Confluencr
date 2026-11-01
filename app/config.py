"""Environment-driven configuration, validated at startup (fail fast)."""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PRODUCTION_ENVS = {"production", "prod"}


class Settings(BaseSettings):
    # ENV_FILE lets tests point at a nonexistent file so a developer's local .env can never leak into them.
    model_config = SettingsConfigDict(env_file=os.environ.get("ENV_FILE", ".env"), extra="ignore")

    app_env: str = "development"
    app_name: str = "transaction-webhook-service"

    database_url: str = ""
    database_ssl_mode: str = "require"

    api_pool_size: int = Field(5, ge=1)
    api_max_overflow: int = Field(5, ge=0)
    api_pool_timeout_seconds: int = Field(5, ge=1)
    worker_pool_size: int = Field(3, ge=1)
    worker_max_overflow: int = Field(2, ge=0)
    pool_recycle_seconds: int = Field(240, ge=30)

    processing_delay_seconds: float = Field(30, ge=0)
    worker_poll_interval_seconds: float = Field(2, gt=0)
    worker_lease_seconds: float = Field(90, gt=0)
    worker_heartbeat_interval_seconds: float = Field(15, gt=0)
    worker_concurrency: int = Field(10, ge=1)
    # Free-tier mode: run the worker loop on a thread inside the API process (single Render free web service).
    run_worker_in_api: bool = False
    worker_stale_after_seconds: float = Field(60, gt=0)
    max_job_attempts: int = Field(3, ge=1)
    retry_backoff_base_seconds: float = Field(5, ge=0)
    retry_backoff_max_seconds: float = Field(300, ge=0)

    webhook_signature_enabled: bool = False
    webhook_signature_required_in_production: bool = True
    webhook_signature_provider: str = "generic"  # generic | razorpay
    webhook_signing_secret: str = ""
    webhook_signature_header: str = "X-Webhook-Signature"
    webhook_timestamp_header: str = "X-Webhook-Timestamp"
    webhook_timestamp_tolerance_seconds: int = Field(300, gt=0)

    max_request_bytes: int = Field(65536, gt=0)

    # Serve the small built-in web UI at /ui (no effect on the API).
    enable_ui: bool = True

    log_level: str = "INFO"
    log_format: str = "json"

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in PRODUCTION_ENVS

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        for prefix in ("postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if not self.database_url:
            raise ValueError("DATABASE_URL is required")
        if self.worker_heartbeat_interval_seconds * 2 > self.worker_lease_seconds:
            raise ValueError("WORKER_LEASE_SECONDS must be at least twice the heartbeat interval")
        if self.webhook_signature_provider not in ("generic", "razorpay"):
            raise ValueError("WEBHOOK_SIGNATURE_PROVIDER must be 'generic' or 'razorpay'")
        if self.webhook_signature_enabled and not self.webhook_signing_secret:
            raise ValueError("WEBHOOK_SIGNING_SECRET is required when signature verification is enabled")
        if self.is_production:
            if self.webhook_signature_required_in_production and not self.webhook_signature_enabled:
                raise ValueError(
                    "Signature verification must be enabled in production "
                    "(or set WEBHOOK_SIGNATURE_REQUIRED_IN_PRODUCTION=false explicitly)"
                )
            if self.processing_delay_seconds < 30:
                raise ValueError("PROCESSING_DELAY_SECONDS must be >= 30 in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
