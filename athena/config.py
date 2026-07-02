"""
📦 模块名称：Athena 配置加载与环境变量覆盖
📍 架构位置：基础设施配置层，位于 CLI/API 启动入口和各业务模块之间。
🎯 核心作用：从 config.yaml 和环境变量读取配置，并用 Pydantic 做类型校验。
🔗 依赖关系：依赖 PyYAML、python-dotenv、Pydantic；被 CLI、Web API、Agent 构建流程依赖。
💡 设计思路：使用“配置对象 + 环境变量覆盖”模式，默认配置写在 YAML，部署时用环境变量临时覆盖。
📚 学习重点：看 WebSettings 如何接入顶层 AthenaSettings，以及 _apply_env_overrides 如何保证命令行/部署灵活性。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, PositiveInt, ValidationError

# 💡 学习提示：先加载 .env，但 override=False 表示系统环境变量优先级更高，部署时不容易被本地文件误覆盖。
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)

from athena.exceptions import ConfigError, ErrorCode


class LLMProviderItem(BaseModel):
    """Single LLM provider configuration for multi-provider setups."""

    name: str = Field(
        default="default",
        description="Human-readable provider identifier, e.g. 'openai-primary'",
    )
    provider: str = Field(
        default="openai",
        description="Provider type: openai, azure, deepseek, anthropic, etc.",
    )
    model: str = Field(
        default="gpt-4o-mini",
        description="Model name, e.g. 'gpt-4o', 'deepseek-chat'",
    )
    api_key_env: str = Field(
        default="OPENAI_API_KEY",
        description="Environment variable for the API key",
    )
    api_base: str | None = Field(
        default=None,
        description="Custom API base URL for proxies or Azure endpoints",
    )
    weight: float = Field(
        default=1.0, ge=0.0, description="Load balancing weight"
    )
    timeout: float = Field(
        default=60.0, ge=1.0, description="Request timeout in seconds"
    )
    max_retries: int = Field(
        default=3, ge=0, description="Max retry attempts per provider"
    )


class LLMSettings(BaseModel):
    """LLM gateway settings.

    Supports both single-provider (legacy) and multi-provider configurations.
    When 'providers' is populated, the LLMGateway is used instead of LiteLLMClient.
    """

    # Legacy single-provider fields (kept for backward compatibility)
    provider: str = "litellm"
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_tokens: PositiveInt = 1024

    # Multi-provider configuration
    providers: list[LLMProviderItem] = Field(
        default_factory=list,
        description="Multi-provider configuration list. When populated, "
        "LLMGateway with connection pooling and failover is used.",
    )
    load_balancing: str = Field(
        default="weighted",
        description="Load balancing strategy: 'weighted' or 'round_robin'",
    )
    circuit_breaker_failures: int = Field(
        default=5, ge=1,
        description="Consecutive failures before circuit breaker opens",
    )
    circuit_breaker_recovery: float = Field(
        default=30.0, ge=1.0,
        description="Seconds before circuit breaker attempts recovery",
    )
    connection_pool_size: int = Field(
        default=100, ge=1,
        description="Max total HTTP connections in pool",
    )
    connection_pool_per_host: int = Field(
        default=20, ge=1,
        description="Max HTTP connections per host",
    )


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


class WebSettings(BaseModel):
    """
    Web 控制台配置。

    功能说明：保存 FastAPI 服务启动和 Web 会话管理相关参数。
    参数说明：
        host：服务监听地址，127.0.0.1 表示只允许本机访问。
        port：服务端口，必须是正整数。
        cors_origins：允许跨域访问的前端来源列表。
        session_ttl_seconds：会话空闲过期时间。
    返回值：Pydantic 配置对象。
    设计思路：把 Web 配置放进统一 AthenaSettings，CLI 和 API 都能使用同一份配置来源。
    使用示例：settings.web.port

    🎯 面试考点：为什么 port/session_ttl_seconds 用 PositiveInt？答案：端口和 TTL 不能是负数，类型层提前挡住错误配置。
    """

    host: str = (
        "127.0.0.1"  # 💡 学习提示：默认绑定本机，比默认 0.0.0.0 更安全，适合本地演示。
    )
    port: PositiveInt = 8000
    cors_origins: list[str] = Field(
        default_factory=lambda: ["*"]
    )  # 💡 学习提示：列表用 default_factory，避免多个配置实例共享同一个可变列表。
    session_ttl_seconds: PositiveInt = 3600


class AthenaSettings(BaseModel):
    """Top-level Athena settings."""

    llm: LLMSettings = Field(default_factory=LLMSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    web: WebSettings = Field(
        default_factory=WebSettings
    )  # 💡 学习提示：新增配置段用 default_factory，可保证旧 config.yaml 没有 web 段时仍能启动。


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
    """
    应用环境变量覆盖。

    功能说明：在 YAML 校验通过后，用 ATHENA_* 环境变量覆盖少量常用配置。
    参数说明：settings 是从 YAML/默认值构造出的 AthenaSettings。
    返回值：覆盖后的新 AthenaSettings。
    设计思路：先复制再修改，避免调用者手里的原配置对象被悄悄改变。
    使用示例：ATHENA_WEB_PORT=9000 athena web

    🔍 原理讲解：
    配置优先级是：默认值 → config.yaml → 环境变量。
    举个例子：
    config.yaml 写 port=8000，环境变量 ATHENA_WEB_PORT=9000 → 最终 settings.web.port 是 9000。
    """
    llm_model = os.getenv("ATHENA_LLM_MODEL")
    log_level = os.getenv("ATHENA_LOG_LEVEL")
    web_host = os.getenv("ATHENA_WEB_HOST")
    web_port = os.getenv("ATHENA_WEB_PORT")
    web_cors = os.getenv("ATHENA_WEB_CORS_ORIGINS")
    web_session_ttl = os.getenv("ATHENA_WEB_SESSION_TTL_SECONDS")
    updated = settings.model_copy(
        deep=True
    )  # 💡 学习提示：deep=True 会复制嵌套对象，修改 updated.web 不会影响原 settings.web。
    if llm_model:
        updated.llm.model = llm_model
    if log_level:
        updated.logging.level = log_level
    if web_host:
        updated.web.host = web_host
    if web_port:
        updated.web.port = int(
            web_port
        )  # 💡 学习提示：环境变量都是字符串，写入 PositiveInt 字段前要转成 int。
    if web_cors:
        updated.web.cors_origins = [
            origin.strip() for origin in web_cors.split(",") if origin.strip()
        ]  # 💡 学习提示：逗号分隔让一个环境变量能配置多个前端来源。
    if web_session_ttl:
        updated.web.session_ttl_seconds = int(web_session_ttl)
    return updated


"""
🤔 思考题：

1. 如果要支持生产环境和开发环境两套 config.yaml，你会怎么扩展 load_settings？
2. 为什么环境变量覆盖要放在 YAML 校验之后？
3. ATHENA_WEB_CORS_ORIGINS="https://a.com,https://b.com" 会被解析成什么？
4. ⚡ 优化建议：当前 int(web_port) 失败会抛 ValueError，未来可以把它包装成 ConfigError，错误信息更友好。
"""
