"""Validated configuration for the Agent Runtime service."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, cast

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, PositiveInt, ValidationError

from athena.exceptions import ConfigError, ErrorCode

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)


class RoutingSettings(BaseModel):
    enabled: bool = False
    light_model: str = "gpt-4o-mini"
    heavy_model: str = "gpt-4o"
    threshold: float = 0.5


class LLMSettings(BaseModel):
    enabled: bool = False
    provider: str = "litellm"
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_tokens: PositiveInt = 1024
    routing: RoutingSettings = Field(default_factory=RoutingSettings)


class RuntimeSettings(BaseModel):
    profile: Literal["demo", "production"] = "demo"


class LoggingSettings(BaseModel):
    level: str = "INFO"


class WebSettings(BaseModel):
    host: str = "127.0.0.1"
    port: PositiveInt = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    session_ttl_seconds: PositiveInt = 3600


class SecuritySettings(BaseModel):
    api_keys: dict[str, str] = Field(default_factory=dict)
    require_auth: bool = False
    default_tenant: str = "public"
    roles: dict[str, list[str]] = Field(default_factory=dict)
    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    secret_master_key: str | None = None


class CacheSettings(BaseModel):
    redis_url: str | None = None
    namespace: str = "athena"
    idempotency_ttl_seconds: PositiveInt = 86400


class DatabaseSettings(BaseModel):
    backend: Literal["memory", "durable"] = "memory"
    url: str | None = None
    echo: bool = False
    auto_migrate: bool = False
    pool_size: PositiveInt = 8
    max_overflow: int = 8


class QueueSettings(BaseModel):
    enabled: bool = True
    stream_name: str = "athena:runtime:tasks"
    consumer_group: str = "athena-runtime"
    block_ms: PositiveInt = 1000
    reclaim_idle_seconds: PositiveInt = 30
    max_attempts: PositiveInt = 5


class EvidenceSettings(BaseModel):
    backend: Literal["local"] = "local"
    local_root: str = "data/evidence"
    max_content_bytes: PositiveInt = 524288


class WorkerSettings(BaseModel):
    lease_ttl_seconds: PositiveInt = 30
    poll_interval_seconds: float = 0.2
    batch_size: PositiveInt = 16


class TaskSettings(BaseModel):
    max_concurrency: PositiveInt = 8
    result_ttl_seconds: PositiveInt = 3600
    thread_pool_workers: PositiveInt = 8


class RateLimitSettings(BaseModel):
    enabled: bool = True
    global_per_minute: PositiveInt = 600
    per_tenant_per_minute: PositiveInt = 120
    per_route_per_minute: PositiveInt = 90
    burst_multiplier: float = 1.0


class ObservabilitySettings(BaseModel):
    metrics_enabled: bool = True


class AthenaSettings(BaseModel):
    llm: LLMSettings = Field(default_factory=LLMSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    evidence: EvidenceSettings = Field(default_factory=EvidenceSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    task: TaskSettings = Field(default_factory=TaskSettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)


def load_settings(path: Path | None = None) -> AthenaSettings:
    config_path = path or Path("config.yaml")
    raw_data: dict[str, object] = {}
    try:
        if config_path.exists():
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError("configuration root must be a mapping")
            raw_data = loaded
        return _apply_env_overrides(AthenaSettings.model_validate(raw_data))
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
        raise ConfigError(ErrorCode.CONFIG_INVALID, str(exc)) from exc


def _apply_env_overrides(settings: AthenaSettings) -> AthenaSettings:
    updated = settings.model_copy(deep=True)

    profile = os.getenv("ATHENA_RUNTIME_PROFILE")
    if profile:
        updated.runtime.profile = cast(Literal["demo", "production"], profile)
    llm_enabled = os.getenv("ATHENA_LLM_ENABLED")
    if llm_enabled:
        updated.llm.enabled = llm_enabled.lower() in {"1", "true", "yes", "on"}
    llm_model = os.getenv("ATHENA_LLM_MODEL")
    if llm_model:
        updated.llm.model = llm_model
    log_level = os.getenv("ATHENA_LOG_LEVEL")
    if log_level:
        updated.logging.level = log_level
    host = os.getenv("ATHENA_WEB_HOST")
    if host:
        updated.web.host = host
    port = os.getenv("ATHENA_WEB_PORT")
    if port:
        updated.web.port = int(port)
    cors = os.getenv("ATHENA_WEB_CORS_ORIGINS")
    if cors:
        updated.web.cors_origins = [
            item.strip() for item in cors.split(",") if item.strip()
        ]
    session_ttl = os.getenv("ATHENA_WEB_SESSION_TTL_SECONDS")
    if session_ttl:
        updated.web.session_ttl_seconds = int(session_ttl)
    redis_url = os.getenv("ATHENA_REDIS_URL")
    if redis_url:
        updated.cache.redis_url = redis_url
    database_url = os.getenv("ATHENA_DATABASE_URL")
    if database_url:
        updated.database.url = database_url
    database_backend = os.getenv("ATHENA_DATABASE_BACKEND")
    if database_backend:
        updated.database.backend = cast(Literal["memory", "durable"], database_backend)
    database_echo = os.getenv("ATHENA_DATABASE_ECHO")
    if database_echo:
        updated.database.echo = database_echo.lower() in {"1", "true", "yes", "on"}
    auto_migrate = os.getenv("ATHENA_DATABASE_AUTO_MIGRATE")
    if auto_migrate:
        updated.database.auto_migrate = auto_migrate.lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    evidence_root = os.getenv("ATHENA_EVIDENCE_LOCAL_ROOT")
    if evidence_root:
        updated.evidence.local_root = evidence_root
    api_keys = os.getenv("ATHENA_API_KEYS")
    if api_keys:
        parsed = {
            key.strip(): tenant.strip() or "public"
            for item in api_keys.split(",")
            if ":" in item
            for key, tenant in [item.split(":", 1)]
            if key.strip()
        }
        if parsed:
            updated.security.api_keys = parsed
            updated.security.require_auth = True
    secret_master_key = os.getenv("ATHENA_SECRET_MASTER_KEY")
    if secret_master_key:
        updated.security.secret_master_key = secret_master_key
    return updated


def production_readiness_issues(settings: AthenaSettings) -> tuple[str, ...]:
    """Return stable, non-secret reasons that make production not ready."""
    if settings.runtime.profile != "production":
        return ()
    issues: list[str] = []
    if not settings.security.require_auth:
        issues.append("AUTH_REQUIRED")
    if not settings.security.api_keys and not settings.security.jwt_secret:
        issues.append("AUTH_CREDENTIALS_MISSING")
    if not settings.security.secret_master_key:
        issues.append("SECRET_MASTER_KEY_REQUIRED")
    if not settings.security.roles:
        issues.append("AUTH_SCOPE_POLICY_MISSING")
    elif any("*" in scopes for scopes in settings.security.roles.values()):
        issues.append("AUTH_WILDCARD_SCOPE_FORBIDDEN")
    if "*" in settings.web.cors_origins:
        issues.append("CORS_WILDCARD_FORBIDDEN")
    if not settings.cache.redis_url:
        issues.append("CACHE_BACKEND_REQUIRED")
    if not settings.database.url:
        issues.append("DATABASE_BACKEND_REQUIRED")
    if not settings.rate_limit.enabled:
        issues.append("RATE_LIMIT_REQUIRED")
    return tuple(issues)


__all__ = [
    "AthenaSettings",
    "DatabaseSettings",
    "load_settings",
    "production_readiness_issues",
]
