"""REIG settings — loaded from REIG_* environment variables.

All config comes from env (or the compose-mounted .env file). pydantic-settings
validates on instantiation; missing required values (encryption key, API
credentials) raise at import time so a misconfigured container fails fast
rather than silently serving 500s.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed view of every REIG_* env var.

    The `env_prefix="REIG_"` strips the prefix so the attribute `database_url`
    binds to `REIG_DATABASE_URL`. `extra="ignore"` is important: the compose
    `.env` file also contains values used by other tooling (ngrok domain, etc.)
    that should not cause a validation error here.
    """

    model_config = SettingsConfigDict(
        env_prefix="REIG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core (required) ---
    database_url: str = "postgresql://reig:reig@db:5432/reig"
    encryption_key: str = Field(..., description="32-byte base64 key for pgcrypto")
    retell_api_key: str = Field(..., description="Retell workspace API key")
    sfdc_client_id: str = Field(..., description="Salesforce consumer key")
    sfdc_client_secret: str = Field(..., description="Salesforce consumer secret")

    # --- Webhook timing ---
    webhook_timestamp_skew_seconds: int = 300
    webhook_response_sla_seconds: int = 2

    # --- Retry ---
    retry_max_attempts: int = 5
    retry_backoff_base_seconds: int = 2
    retry_backoff_max_seconds: int = 60

    # --- Adapter routing ---
    active_adapter: str = "salesforce"

    # --- PII / PHI ---
    pii_redaction_enabled: bool = True
    pii_entities: str = "PHONE_NUMBER,US_SSN,EMAIL_ADDRESS,CREDIT_CARD"
    phi_mode: bool = False

    # --- Salesforce ---
    sfdc_login_url: str = "https://login.salesforce.com"
    sfdc_api_version: str = "v60.0"
    sfdc_instance_url: str | None = None
    sfdc_callback_url: str | None = None

    # --- Observability ---
    otel_endpoint: str = "http://jaeger:4317"
    otel_service_name: str = "retell-integration-gateway"
    log_level: str = "INFO"

    # --- Tenant keys ---
    tenant_api_key_prefix: str = "reig_"

    # --- Misc (ignored but accepted from .env) ---
    ngrok_domain: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance.

    lru_cache so importing many modules doesn't re-parse env on every call.
    Tests that need alternative settings should clear the cache manually via
    `get_settings.cache_clear()`.
    """
    return Settings()  # type: ignore[call-arg]
