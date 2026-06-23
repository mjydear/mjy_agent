"""Configuration loading with YAML defaults and environment overrides."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, PositiveInt, ValidationError

from athena.exceptions import ConfigError, ErrorCode


class LLMSettings(BaseModel):
    """LLM gateway settings."""

    provider: str = "litellm"
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_tokens: PositiveInt = 1024


class MemorySettings(BaseModel):
    """Memory-related settings."""

    working_max_tokens: PositiveInt = 8000
    vector_top_k: PositiveInt = 5


class AgentSettings(BaseModel):
    """Agent execution-loop settings."""

    max_steps: PositiveInt = 6


class LoggingSettings(BaseModel):
    """Logging settings."""

    level: str = "INFO"


class AthenaSettings(BaseModel):
    """Top-level Athena settings."""

    llm: LLMSettings = Field(default_factory=LLMSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


def load_settings(path: Path | None = None) -> AthenaSettings:
    """Load Athena settings from YAML and environment variables.

    Args:
        path: Optional config path. Defaults to ``config.yaml`` in cwd.

    Returns:
        Validated Athena settings.

    Raises:
        ConfigError: If YAML loading or validation fails.
    """
    config_path = path or Path("config.yaml")
    raw_data: dict[str, object] = {}
    try:
        if config_path.exists():
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw_data = dict(loaded)
        settings = AthenaSettings.model_validate(raw_data)
        return _apply_env_overrides(settings)
    except (OSError, ValidationError, yaml.YAMLError) as exc:
        raise ConfigError(ErrorCode.CONFIG_INVALID, str(exc)) from exc


def _apply_env_overrides(settings: AthenaSettings) -> AthenaSettings:
    """Apply narrow environment overrides after YAML validation."""
    llm_model = os.getenv("ATHENA_LLM_MODEL")
    log_level = os.getenv("ATHENA_LOG_LEVEL")
    updated = settings.model_copy(deep=True)
    if llm_model:
        updated.llm.model = llm_model
    if log_level:
        updated.logging.level = log_level
    return updated
