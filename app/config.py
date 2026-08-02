"""Environment configuration — mirrors packages/config/src/env.ts.

Loaded once at startup. Pydantic-settings reads from process env and `.env`.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

    # Application
    NODE_ENV: Literal["development", "test", "staging", "production"] = "development"
    PORT: int = 8005
    API_URL: str = "http://localhost:8005"
    WEB_URL: str = "http://localhost:3001"

    # Master DB (SQLAlchemy URL — use postgresql+psycopg://)
    MASTER_DATABASE_URL: str

    # Redis
    REDIS_ENABLED: Literal["true", "false"] = "true"
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str | None = None

    # JWT — keep names + semantics aligned with the Nest config
    JWT_SECRET: str = Field(min_length=32)
    JWT_EXPIRES_IN: str = "15m"
    REFRESH_SECRET: str = Field(min_length=32)
    REFRESH_EXPIRES_IN: str = "30d"

    # Encryption
    MASTER_ENCRYPTION_KEY: str = Field(min_length=64, max_length=64)

    # Email
    EMAIL_FROM: str = "noreply@qa-platform.local"
    SMTP_HOST: str | None = None
    SMTP_PORT: int | None = None
    SMTP_USER: str | None = None
    SMTP_PASS: str | None = None

    # Stripe (optional)
    STRIPE_SECRET_KEY: str | None = None
    STRIPE_WEBHOOK_SECRET: str | None = None

    PLATFORM_ADMIN_TOKEN: str | None = None

    # Tenant provisioning (superuser PG creds for CREATE DATABASE/USER)
    TENANT_DB_HOST: str = "localhost"
    TENANT_DB_PORT: int = 5432
    TENANT_DB_SUPERUSER: str = "postgres"
    TENANT_DB_SUPERUSER_PASSWORD: str = ""

    # Worker concurrency knobs (informational — Celery is started via CLI)
    EVAL_WORKER_CONCURRENCY: int = 5
    TENANT_PROVISION_WORKER_CONCURRENCY: int = 2

    # Autoscale policy for queue-metrics collector
    AUTOSCALE_EVAL_MIN_REPLICAS: int = 1
    AUTOSCALE_EVAL_MAX_REPLICAS: int = 20
    AUTOSCALE_EVAL_TARGET_BACKLOG_PER_REPLICA: int = 25
    AUTOSCALE_TENANT_PROVISION_MIN_REPLICAS: int = 1
    AUTOSCALE_TENANT_PROVISION_MAX_REPLICAS: int = 10
    AUTOSCALE_TENANT_PROVISION_TARGET_BACKLOG_PER_REPLICA: int = 5

    # Internal LLM prompt audit log file (JSONL)
    LLM_PROMPT_LOG_PATH: str | None = None

    # Default Admin Provisioning
    AUTO_PROVISION_ADMIN_EMAIL: str | None = None
    AUTO_PROVISION_ADMIN_PASSWORD: str | None = None
    AUTO_PROVISION_ADMIN_NAME: str = "Super Admin"
    AUTO_PROVISION_ADMIN_TENANT_SLUG: str | None = None
    AUTO_PROVISION_ADMIN_TENANT_NAME: str = "Super Admin Workspace"
    SUPER_ADMIN_EMAILS: str = "admin@qa.com,admin@dev.local,superadmin@qa.com"

    @property
    def redis_enabled(self) -> bool:
        return self.REDIS_ENABLED == "true"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
