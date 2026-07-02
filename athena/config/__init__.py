"""
Athena 配置系统 — 多环境、Vault 集成、热加载。

架构位置：基础设施配置层，位于 CLI/API 启动入口和各业务模块之间。

核心能力：
- 多环境配置: config/{base,dev,staging,prod}.yaml，通过 ATHENA_ENV 切换
- Vault 集成: 敏感信息从 HashiCorp Vault 读取，降级到环境变量
- 热加载: 文件变更时自动重载配置，无需重启服务
- 环境变量覆盖: ATHENA_* 前缀的变量覆盖 YAML 配置

配置优先级（从低到高）：
    代码默认值 → config.base.yaml → config.{env}.yaml → 环境变量 → Vault
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable, Awaitable

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, PositiveInt, ValidationError

from athena.exceptions import ConfigError, ErrorCode

logger = logging.getLogger(__name__)

# 加载 .env 文件，override=False 保证系统环境变量优先级更高
load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env", override=False)

_DEFAULT_CONFIG_DIR = Path(__file__).parent.parent.parent / "config"

# ============================================================
# Settings Models
# ============================================================


class LLMProviderItem(BaseModel):
    """Single LLM provider configuration for multi-provider setups."""

    name: str = Field(default="default")
    provider: str = Field(default="openai")
    model: str = Field(default="gpt-4o-mini")
    api_key_env: str = Field(default="OPENAI_API_KEY")
    api_base: str | None = Field(default=None)
    weight: float = Field(default=1.0, ge=0.0)
    timeout: float = Field(default=60.0, ge=1.0)
    max_retries: int = Field(default=3, ge=0)


class LLMSettings(BaseModel):
    """LLM gateway settings with single/multi-provider support."""

    provider: str = "litellm"
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_tokens: PositiveInt = 1024
    providers: list[LLMProviderItem] = Field(default_factory=list)
    load_balancing: str = Field(default="weighted")
    circuit_breaker_failures: int = Field(default=5, ge=1)
    circuit_breaker_recovery: float = Field(default=30.0, ge=1.0)
    connection_pool_size: int = Field(default=100, ge=1)
    connection_pool_per_host: int = Field(default=20, ge=1)


class MemorySettings(BaseModel):
    """Memory-related settings."""

    working_max_tokens: PositiveInt = 8000
    vector_top_k: PositiveInt = 5
    embedding_provider: str = Field(default="hash")
    embedding_model: str = Field(default="text-embedding-3-small")
    embedding_dimension: int = Field(default=1536)
    embedding_api_key_env: str = Field(default="OPENAI_API_KEY")
    milvus_uri: str = Field(default="http://localhost:19530")
    milvus_collection: str = Field(default="athena_memory")


class AgentSettings(BaseModel):
    """Agent execution-loop settings."""

    max_steps: PositiveInt = 6


class LoggingSettings(BaseModel):
    """Logging settings."""

    level: str = "INFO"


class WebSettings(BaseModel):
    """Web 控制台配置."""

    host: str = "127.0.0.1"
    port: PositiveInt = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    session_ttl_seconds: PositiveInt = 3600


class AthenaSettings(BaseModel):
    """Top-level Athena settings."""

    llm: LLMSettings = Field(default_factory=LLMSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    web: WebSettings = Field(default_factory=WebSettings)


# ============================================================
# Config Loader
# ============================================================


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dicts. Override values take precedence."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml_file(path: Path) -> dict[str, object]:
    """Load a YAML file, returning empty dict if not found or invalid."""
    if not path.exists():
        return {}
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
        return dict(content) if isinstance(content, dict) else {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Failed to load config file %s: %s", path, exc)
        return {}


def resolve_config_paths(
    config_dir: Path | None = None,
    env: str | None = None,
) -> list[Path]:
    """Resolve config file paths in priority order.

    Priority (low to high):
        1. config.base.yaml
        2. config.{env}.yaml
        3. config.local.yaml (gitignored, for local overrides)

    Args:
        config_dir: Config directory path. Defaults to ``config/`` in project root.
        env: Environment name. Defaults to ``ATHENA_ENV`` env var or ``dev``.

    Returns:
        List of config file paths in priority order.
    """
    if config_dir is None:
        config_dir = _DEFAULT_CONFIG_DIR
    if env is None:
        env = os.getenv("ATHENA_ENV", "dev")

    paths = [config_dir / "config.base.yaml"]
    if env:
        paths.append(config_dir / f"config.{env}.yaml")
    paths.append(config_dir / "config.local.yaml")
    return paths


def load_settings(
    path: Path | None = None,
    env: str | None = None,
    config_dir: Path | None = None,
) -> AthenaSettings:
    """Load Athena settings from multi-environment config files.

    Supports both old single-file mode (``path`` parameter) and new
    multi-env mode (``config_dir`` + ``env`` parameters).

    Args:
        path: Optional single config file path. If provided, uses legacy mode.
        env: Environment name (dev/staging/prod). Defaults to ATHENA_ENV or 'dev'.
        config_dir: Config directory. Defaults to ``config/`` in project root.

    Returns:
        Validated AthenaSettings.

    Raises:
        ConfigError: If YAML loading or validation fails.
    """
    raw_data: dict[str, object] = {}

    try:
        if path is not None and path.exists():
            # Legacy single-file mode
            loaded = _load_yaml_file(path)
            raw_data = dict(loaded) if loaded else {}
        else:
            # Multi-environment mode
            for config_path in resolve_config_paths(config_dir, env):
                loaded = _load_yaml_file(config_path)
                if loaded:
                    raw_data = _deep_merge(raw_data, loaded)

        settings = AthenaSettings.model_validate(raw_data)
        return _apply_env_overrides(settings)
    except (ValidationError, yaml.YAMLError) as exc:
        raise ConfigError(ErrorCode.CONFIG_INVALID, str(exc)) from exc
    except OSError as exc:
        raise ConfigError(ErrorCode.CONFIG_INVALID, str(exc)) from exc


def _apply_env_overrides(settings: AthenaSettings) -> AthenaSettings:
    """Apply ATHENA_* environment variable overrides."""
    llm_model = os.getenv("ATHENA_LLM_MODEL")
    log_level = os.getenv("ATHENA_LOG_LEVEL")
    web_host = os.getenv("ATHENA_WEB_HOST")
    web_port = os.getenv("ATHENA_WEB_PORT")
    web_cors = os.getenv("ATHENA_WEB_CORS_ORIGINS")
    web_session_ttl = os.getenv("ATHENA_WEB_SESSION_TTL_SECONDS")

    updated = settings.model_copy(deep=True)
    if llm_model:
        updated.llm.model = llm_model
    if log_level:
        updated.logging.level = log_level
    if web_host:
        updated.web.host = web_host
    if web_port:
        updated.web.port = int(web_port)
    if web_cors:
        updated.web.cors_origins = [
            origin.strip() for origin in web_cors.split(",") if origin.strip()
        ]
    if web_session_ttl:
        updated.web.session_ttl_seconds = int(web_session_ttl)
    return updated


# ============================================================
# Hot Reload
# ============================================================

ReloadCallback = Callable[[AthenaSettings], Awaitable[None]]


class ConfigReloader:
    """Watch config files for changes and reload settings automatically.

    Uses polling-based file monitoring (no external dependencies).
    For production use with lower latency, install ``watchfiles``.

    Usage:
        reloader = ConfigReloader(load_settings)
        reloader.on_reload(lambda s: logger.info("Config reloaded: %s", s.logging.level))
        await reloader.start()
        # ... app runs ...
        await reloader.stop()
    """

    def __init__(
        self,
        settings_factory: Callable[[], AthenaSettings],
        poll_interval: float = 5.0,
        config_dir: Path | None = None,
    ) -> None:
        """Initialize the config reloader.

        Args:
            settings_factory: Callable that returns fresh settings.
            poll_interval: Polling interval in seconds.
            config_dir: Config directory to watch. Defaults to ``config/``.
        """
        self._settings_factory = settings_factory
        self._poll_interval = poll_interval
        self._config_dir = config_dir or _DEFAULT_CONFIG_DIR
        self._callbacks: list[ReloadCallback] = []
        self._task: asyncio.Task[None] | None = None
        self._last_mtimes: dict[str, float] = {}

    def on_reload(self, callback: ReloadCallback) -> None:
        """Register a callback to be called when config is reloaded.

        Args:
            callback: Async function that receives the new settings.
        """
        self._callbacks.append(callback)

    async def start(self) -> None:
        """Start watching for config changes."""
        if self._task is not None:
            return
        self._last_mtimes = self._scan_mtimes()
        self._task = asyncio.create_task(self._watch_loop())
        logger.info("Config reloader started (interval: %ss)", self._poll_interval)

    async def stop(self) -> None:
        """Stop watching for config changes."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Config reloader stopped")

    def _scan_mtimes(self) -> dict[str, float]:
        """Scan config directory and return file mtimes."""
        mtimes: dict[str, float] = {}
        if not self._config_dir.exists():
            return mtimes
        for f in self._config_dir.glob("config*.yaml"):
            try:
                mtimes[str(f)] = f.stat().st_mtime
            except OSError:
                pass
        return mtimes

    async def _watch_loop(self) -> None:
        """Polling loop that checks for config changes."""
        while True:
            await asyncio.sleep(self._poll_interval)
            current_mtimes = self._scan_mtimes()
            if current_mtimes != self._last_mtimes:
                logger.info("Config file change detected, reloading...")
                try:
                    new_settings = self._settings_factory()
                    for callback in self._callbacks:
                        try:
                            await callback(new_settings)
                        except Exception:
                            logger.exception("Config reload callback failed")
                    self._last_mtimes = current_mtimes
                    logger.info("Config reloaded successfully")
                except Exception:
                    logger.exception("Config reload failed, keeping old settings")