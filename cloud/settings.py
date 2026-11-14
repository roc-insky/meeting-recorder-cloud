"""Explicit cloud configuration, including the candidate budgets.

Two rules drive this module:

* the budgets from plan line 55 are named settings, never magic numbers
  scattered through handlers and workers;
* production startup must refuse unsafe combinations (fake identity provider,
  non-Secure cookies, missing HTTPS base URL) instead of silently degrading.
"""

from __future__ import annotations

import os
import re
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Plan line 55 and the global constraints: resumés are at most 20k characters
# and images at most 5 MiB each (at most five images per interview input).
IMAGE_MAX_BYTES = 5 * 1024 * 1024
RESUME_MAX_CHARS = 20000

_FORBIDDEN_URL_PARTS = re.compile(r"[?#]")
_PUBLIC_BASE_URL = re.compile(r"^https?://[A-Za-z0-9.\-]+(:[0-9]{1,5})?$")


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    database_url: str = Field(min_length=1)
    secret_key: str = Field(min_length=16)
    environment: Literal["development", "test", "production"] = "development"
    auth_provider: str = "fake_test"

    cookie_secure: bool = True
    session_ttl_s: int = Field(default=43200, gt=0)
    login_state_ttl_s: int = Field(default=600, gt=0)
    session_cookie_name: str = "cloud_session"
    csrf_cookie_name: str = "cloud_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    public_base_url: str | None = None

    host: str = "127.0.0.1"
    port: int = Field(default=8300, ge=1, le=65535)

    # -- candidate budgets (plan line 55) ---------------------------------
    chunk_target_ms: int = Field(default=3000, gt=0)
    saved_lag_target_ms: int = Field(default=10000, gt=0)
    live_latency_target_ms: tuple[int, int] = (5000, 15000)
    buffer_max_ms: int = Field(default=300000, gt=0)
    buffer_max_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    meeting_max_ms: int = Field(default=7200000, gt=0)
    active_capture_per_user: int = Field(default=1, ge=1)
    recovery_window_s: int = Field(default=86400, gt=0)
    cleanup_target_s: int = Field(default=86400, gt=0)
    ephemeral_cleanup_s: int = Field(default=3600, gt=0)
    backup_retention_days: int = Field(default=30, gt=0)
    log_retention_days: int = Field(default=30, gt=0)
    image_max_pixels: int = Field(default=20000000, gt=0)
    request_max_bytes: int = Field(default=30 * 1024 * 1024, gt=0)
    ai_output_max_chars: int = Field(default=40000, gt=0)
    request_timeout_s: int = Field(default=120, gt=0)
    worker_lease_s: int = Field(default=60, gt=0)
    worker_heartbeat_s: int = Field(default=20, gt=0)

    @field_validator("database_url")
    @classmethod
    def _postgres_psycopg_only(cls, value: str) -> str:
        if not value.startswith("postgresql+psycopg://"):
            raise ValueError("database_url must be a postgresql+psycopg:// URL")
        if _FORBIDDEN_URL_PARTS.search(value):
            raise ValueError("database_url must not contain query or fragment parts")
        return value

    @field_validator("public_base_url")
    @classmethod
    def _public_base_url_is_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.rstrip("/")
        if not _PUBLIC_BASE_URL.match(normalized):
            raise ValueError("public_base_url must be an http(s) origin without a path")
        return normalized

    @field_validator("secret_key")
    @classmethod
    def _secret_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("secret_key must not be blank")
        return value

    @model_validator(mode="after")
    def _budgets_are_coherent(self) -> "Settings":
        low, high = self.live_latency_target_ms
        if low <= 0 or low > high:
            raise ValueError("live_latency_target_ms must be two ascending positive values")
        if self.buffer_max_ms > self.meeting_max_ms:
            raise ValueError("buffer_max_ms must not exceed meeting_max_ms")
        if self.worker_heartbeat_s >= self.worker_lease_s:
            raise ValueError("worker_heartbeat_s must be shorter than worker_lease_s")
        if self.recovery_window_s > 7 * self.cleanup_target_s:
            raise ValueError("recovery_window_s must stay within a week of cleanup")
        return self

    def validate_for_startup(self) -> None:
        """Refuse configurations that would silently weaken production security."""
        if self.environment != "production":
            return
        if not self.cookie_secure:
            raise RuntimeError(
                "production requires Secure session cookies (cookie_secure=True)"
            )
        if len(self.secret_key) < 32:
            raise RuntimeError("production requires a secret_key of at least 32 characters")
        if not self.public_base_url or not self.public_base_url.startswith("https://"):
            raise RuntimeError("production requires an https public_base_url for auth redirects")
        if self.auth_provider == "fake_test":
            raise RuntimeError(
                "production startup must refuse the fake identity provider "
                "(gate G1: real provider not selected)"
            )
        raise RuntimeError(
            f"identity provider {self.auth_provider!r} is not implemented "
            "(gate G1: real provider not selected)"
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        database_url = env.get("CLOUD_DATABASE_URL") or env.get("TEST_DATABASE_URL")
        if not database_url:
            raise RuntimeError("CLOUD_DATABASE_URL (or TEST_DATABASE_URL) is required")
        secret_key = env.get("CLOUD_SECRET_KEY")
        if not secret_key:
            raise RuntimeError("CLOUD_SECRET_KEY is required")
        return cls(
            database_url=database_url,
            secret_key=secret_key,
            environment=env.get("CLOUD_ENVIRONMENT", "development"),
            auth_provider=env.get("CLOUD_AUTH_PROVIDER", "fake_test"),
            cookie_secure=_env_bool(env.get("CLOUD_COOKIE_SECURE"), default=True),
            public_base_url=env.get("CLOUD_PUBLIC_BASE_URL") or None,
            host=env.get("CLOUD_HOST", "127.0.0.1"),
            port=int(env.get("CLOUD_PORT", "8300")),
        )


def _env_bool(value: str | None, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(f"invalid boolean value {value!r}")
