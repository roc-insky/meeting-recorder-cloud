"""Explicit cloud configuration, including the candidate budgets.

Two rules drive this module:

* the budgets from plan line 55 are named settings, never magic numbers
  scattered through handlers and workers;
* production startup must refuse unsafe combinations (fake identity provider,
  non-Secure cookies, missing HTTPS base URL) instead of silently degrading.
"""

from __future__ import annotations

import base64
import math
import os
import re
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Plan line 55 and the global constraints: resumés are at most 20k characters
# and images at most 5 MiB each (at most five images per interview input).
IMAGE_MAX_BYTES = 5 * 1024 * 1024
RESUME_MAX_CHARS = 20000
IMAGE_TOTAL_MAX_BYTES = 30 * 1024 * 1024
IMAGE_MAX_PIXELS = 20_000_000
AI_OUTPUT_MAX_CHARS = 40000
EPHEMERAL_TIMEOUT_S = 120

#: Provider names an operator may select. A provider instance is always
#: injected; no HTTP endpoint or URL is ever read from configuration.
ALLOWED_AI_PROVIDERS = ("disabled", "injected")

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

    # -- deployment readiness (Task 17) -----------------------------------
    #: Local, operator-provided model manifest. Unset means the readiness
    #: probe reports "model: false"; nothing is ever downloaded to satisfy it.
    model_manifest_path: str | None = None
    #: Directory that manifest file entries are relative to. Unset means the
    #: manifest's own directory. Only local bytes are ever verified.
    model_cache_dir: str | None = None
    #: Proxy hops whose X-Forwarded-For/Proto headers uvicorn may trust.
    #: A wildcard is refused: the API only ever sees the TLS proxy.
    forwarded_allow_ips: str = "127.0.0.1"

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
    image_max_pixels: int = Field(default=IMAGE_MAX_PIXELS, gt=0)
    request_max_bytes: int = Field(default=IMAGE_TOTAL_MAX_BYTES, gt=0)
    ai_output_max_chars: int = Field(default=AI_OUTPUT_MAX_CHARS, gt=0)
    request_timeout_s: int = Field(default=EPHEMERAL_TIMEOUT_S, gt=0)
    worker_lease_s: int = Field(default=60, gt=0)
    worker_heartbeat_s: int = Field(default=20, gt=0)

    # -- isolation budgets (Task 16) --------------------------------------
    # Provisional request budgets, not approved capacity or SLAs. One fixed
    # window is shared by every action; the per-action counts below are
    # deliberately generous for normal use and are enforced in PostgreSQL.
    rate_limit_window_s: int = Field(default=60, gt=0)
    rate_limit_chunk_write: int = Field(default=600, gt=0)
    rate_limit_lease_write: int = Field(default=120, gt=0)
    rate_limit_task_enqueue: int = Field(default=120, gt=0)
    rate_limit_ai_call: int = Field(default=60, gt=0)
    rate_limit_playback: int = Field(default=600, gt=0)
    rate_limit_voiceprint_enroll: int = Field(default=30, gt=0)
    #: A decode slot outlives one worker lease so a live claim is never stolen
    #: while its task lease is still valid; expiry is always reclaimable.
    decode_slot_s: int = Field(default=180, gt=0)

    # -- privacy, tombstones and cleanup (Task 15) ------------------------
    #: Append-only JSONL tombstone audit. Required in production, where a
    #: durable path outside the database and object store must be configured
    #: explicitly; the G1 identity-provider refusal still fires first.
    privacy_audit_path: str | None = None
    #: How long a revoked account's still-referenced records are retained
    #: before run_cleanup may hard-delete them. 0 allows immediate purge.
    account_revocation_wait_s: int = Field(default=604800, ge=0)
    #: Root of disposable local scratch files; cleanup never follows symlinks.
    ephemeral_root: str | None = None
    #: During a restore the API refuses every /api/cloud request with 503.
    privacy_maintenance: bool = False

    # -- voiceprints (Task 13) -------------------------------------------
    #: Base64url/base64 32-byte AES-GCM key; never rendered by repr().
    voiceprint_key: str | None = Field(default=None, repr=False)
    voiceprint_sample_max_bytes: int = Field(default=10_000_000, gt=0)
    voiceprint_high_threshold: float | None = None
    voiceprint_low_threshold: float | None = None

    # -- ephemeral AI (Task 14) ------------------------------------------
    #: Selected provider *name* only. The provider object is injected by the
    #: application; settings never carry a network endpoint or URL.
    ai_provider: str | None = None
    ai_model: str | None = Field(default=None, max_length=200)
    ai_api_key: str | None = Field(default=None, repr=False)
    ai_timeout_s: int = Field(
        default=EPHEMERAL_TIMEOUT_S, ge=1, le=EPHEMERAL_TIMEOUT_S
    )
    ai_image_max_pixels: int = Field(default=IMAGE_MAX_PIXELS, gt=0)
    ai_image_total_max_bytes: int = Field(
        default=IMAGE_TOTAL_MAX_BYTES, gt=0
    )

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

    @field_validator("forwarded_allow_ips")
    @classmethod
    def _forwarded_allow_ips_is_a_list(cls, value: str) -> str:
        entries = [item.strip() for item in value.split(",")]
        if not entries or any(not item for item in entries):
            raise ValueError(
                "forwarded_allow_ips must be a non-empty comma-separated list"
            )
        if "*" in entries:
            raise ValueError("forwarded_allow_ips must not trust every peer")
        return ",".join(entries)

    @field_validator("ai_provider")
    @classmethod
    def _ai_provider_is_allowlisted(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if value not in ALLOWED_AI_PROVIDERS:
            raise ValueError(
                "ai_provider must be disabled or an allowlisted injected name"
            )
        return value

    @field_validator("voiceprint_key")
    @classmethod
    def _voiceprint_key_is_32_bytes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        raw = decode_voiceprint_key(value)
        if len(raw) != 32:
            raise ValueError("voiceprint_key must decode to exactly 32 bytes")
        return value

    @model_validator(mode="after")
    def _voiceprint_thresholds_pair(self) -> "Settings":
        high = self.voiceprint_high_threshold
        low = self.voiceprint_low_threshold
        if (high is None) != (low is None):
            raise ValueError(
                "voiceprint_high_threshold and voiceprint_low_threshold must be set together"
            )
        for name, value in (("high", high), ("low", low)):
            if value is not None and (
                not math.isfinite(value) or value < 0.0 or value > 1.0
            ):
                raise ValueError(
                    f"voiceprint_{name}_threshold must be finite and between 0 and 1"
                )
        if high is not None and low is not None and high <= low:
            raise ValueError(
                "voiceprint_high_threshold must be greater than voiceprint_low_threshold"
            )
        return self

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
        self._require_voiceprint_key_for_production()

    def _require_voiceprint_key_for_production(self) -> None:
        # Kept separate from validate_for_startup so the identity-provider gate
        # (G1) remains the authoritative refusal when no real provider exists.
        # A future real provider must call this before serving traffic.
        if self.voiceprint_key is None:
            raise RuntimeError(
                "production requires a voiceprint_key for encrypted enrolments"
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
            model_manifest_path=env.get("CLOUD_MODEL_MANIFEST") or None,
            model_cache_dir=env.get("CLOUD_MODEL_CACHE") or None,
            forwarded_allow_ips=env.get(
                "CLOUD_FORWARDED_ALLOW_IPS", "127.0.0.1"
            ),
            privacy_audit_path=env.get("CLOUD_PRIVACY_AUDIT_PATH") or None,
            account_revocation_wait_s=int(
                env.get("CLOUD_ACCOUNT_REVOCATION_WAIT_S", "604800")
            ),
            ephemeral_root=env.get("CLOUD_EPHEMERAL_ROOT") or None,
            privacy_maintenance=_env_bool(
                env.get("CLOUD_PRIVACY_MAINTENANCE"), default=False
            ),
            voiceprint_key=env.get("CLOUD_VOICEPRINT_KEY") or None,
            voiceprint_high_threshold=_env_float(
                env.get("CLOUD_VOICEPRINT_HIGH_THRESHOLD")
            ),
            voiceprint_low_threshold=_env_float(
                env.get("CLOUD_VOICEPRINT_LOW_THRESHOLD")
            ),
            ai_provider=env.get("CLOUD_AI_PROVIDER") or None,
            ai_model=env.get("CLOUD_AI_MODEL") or None,
            ai_api_key=env.get("CLOUD_AI_API_KEY") or None,
            ai_timeout_s=int(
                env.get("CLOUD_AI_TIMEOUT_S", str(EPHEMERAL_TIMEOUT_S))
            ),
            ai_output_max_chars=int(
                env.get("CLOUD_AI_OUTPUT_MAX_CHARS", str(AI_OUTPUT_MAX_CHARS))
            ),
            ai_image_max_pixels=int(
                env.get("CLOUD_AI_IMAGE_MAX_PIXELS", str(IMAGE_MAX_PIXELS))
            ),
            ai_image_total_max_bytes=int(
                env.get("CLOUD_AI_IMAGE_TOTAL_MAX_BYTES", str(IMAGE_TOTAL_MAX_BYTES))
            ),
        )


def decode_voiceprint_key(value: str) -> bytes:
    """Decode a base64/base64url voiceprint key; padding is optional."""
    text = value.strip()
    if not text:
        raise ValueError("voiceprint_key must not be blank")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError("voiceprint_key must be valid base64") from exc


def _env_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _env_bool(value: str | None, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(f"invalid boolean value {value!r}")
