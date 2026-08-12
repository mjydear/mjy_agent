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
from typing import Literal, cast

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, PositiveInt, ValidationError

# 💡 学习提示：先加载 .env，但 override=False 表示系统环境变量优先级更高，部署时不容易被本地文件误覆盖。
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)

from athena.exceptions import ConfigError, ErrorCode


class RoutingSettings(BaseModel):
    """多模型复杂度路由：简单查询走轻量模型，复杂查询走强模型。"""

    enabled: bool = False  # 默认关闭；开启需同时配置 light/heavy 模型凭证
    light_model: str = "gpt-4o-mini"
    heavy_model: str = "gpt-4o"
    threshold: float = 0.5


class LLMSettings(BaseModel):
    """LLM gateway settings."""

    # Credentials alone must not switch the application into network mode. Live
    # model calls are an explicit deployment choice; tests and local demos stay
    # deterministic even when a developer has a .env file.
    enabled: bool = False
    provider: str = "litellm"
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_tokens: PositiveInt = 1024
    routing: RoutingSettings = Field(default_factory=RoutingSettings)


class EmbeddingSettings(BaseModel):
    """嵌入模型设置：启用真实向量模型，缺凭证时自动降级哈希嵌入。"""

    enabled: bool = False  # 默认关闭，无 API Key 时用哈希嵌入
    model: str = "text-embedding-3-small"
    dimension: PositiveInt = 1536  # 需与所选模型输出维度一致（哈希降级同维）


class VectorStoreSettings(BaseModel):
    """向量库设置：backend=milvus 连不上时自动降级内存。"""

    backend: str = "memory"  # memory | milvus
    uri: str = "http://localhost:19530"
    collection_name: str = "athena_memory"
    dimension: PositiveInt = 1536
    metric_type: str = "COSINE"


class MemorySettings(BaseModel):
    """Memory-related settings."""

    working_max_tokens: PositiveInt = 8000
    vector_top_k: PositiveInt = 5


class RuntimeSettings(BaseModel):
    """Deployment profile controlling fail-open versus fail-closed behavior."""

    profile: Literal["demo", "production"] = "demo"


class AgentSettings(BaseModel):
    """Agent execution-loop settings."""

    max_steps: PositiveInt = 6
    execution_mode: Literal["legacy_react", "policy_workflow"] = "legacy_react"


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


class SecuritySettings(BaseModel):
    """接口安全设置：API Key 鉴权、RBAC 授权与多租户映射。"""

    # api_keys 为空表示鉴权关闭（本地演示友好）；配置后所有写接口需带合法 Key。
    # 格式：{"<api-key>": "<tenant_id>"}
    api_keys: dict[str, str] = Field(default_factory=dict)
    require_auth: bool = False
    default_tenant: str = "public"
    # RBAC：租户 → 授予的 scope 列表；["*"] 表示全部权限。
    # 未配置的租户默认拥有全部权限，保证向后兼容与本地演示友好。
    roles: dict[str, list[str]] = Field(default_factory=dict)
    # Machine-to-machine Alertmanager integration tokens:
    # {"integration-token": "tenant_id"}. The resolved principal only receives
    # alerts:ingest.
    alert_integration_tokens: dict[str, str] = Field(default_factory=dict)
    # JWT/OIDC（可选）：配置 jwt_secret 后支持 Authorization: Bearer <token>。
    # 缺失时仅走 API Key，PyJWT 未安装时自动禁用 Bearer 校验（自动降级）。
    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    secret_master_key: str | None = None


class CacheSettings(BaseModel):
    """缓存设置：Redis URL 为空或连不上时自动降级为内存缓存。"""

    redis_url: str | None = None
    namespace: str = "athena"
    vector_ttl_seconds: PositiveInt = 300
    idempotency_ttl_seconds: PositiveInt = 86400


class DatabaseSettings(BaseModel):
    """Durable PostgreSQL/SQLite persistence settings."""

    url: str | None = None
    echo: bool = False
    auto_migrate: bool = False
    pool_size: PositiveInt = 8
    max_overflow: int = 8


class QueueSettings(BaseModel):
    """Redis Streams transport settings for durable task delivery."""

    enabled: bool = True
    stream_name: str = "athena:ops:tasks"
    consumer_group: str = "athena-workers"
    block_ms: PositiveInt = 1000
    reclaim_idle_seconds: PositiveInt = 30
    max_attempts: PositiveInt = 5


class EvidenceSettings(BaseModel):
    """Content-addressed Evidence storage; metadata stays in the task database."""

    backend: Literal["local"] = "local"
    local_root: str = "data/evidence"
    max_content_bytes: PositiveInt = 524288


class WorkerSettings(BaseModel):
    """Worker lease and bounded execution settings."""

    lease_ttl_seconds: PositiveInt = 30
    poll_interval_seconds: float = 0.2
    batch_size: PositiveInt = 16


class TaskSettings(BaseModel):
    """异步任务设置：控制后台任务并发与保留时长。"""

    max_concurrency: PositiveInt = 8
    result_ttl_seconds: PositiveInt = 3600
    thread_pool_workers: PositiveInt = 8


class RateLimitSettings(BaseModel):
    """限流设置：全局与单用户每分钟请求上限。"""

    enabled: bool = True
    global_per_minute: PositiveInt = 600
    per_tenant_per_minute: PositiveInt = 120
    per_route_per_minute: PositiveInt = 90
    burst_multiplier: float = 1.0


class SandboxSettings(BaseModel):
    """沙箱资源限制：CPU 时间、内存、超时。"""

    cpu_time_seconds: PositiveInt = 5
    memory_mb: PositiveInt = 256
    timeout_seconds: float = 5.0


class ObservabilitySettings(BaseModel):
    """可观测性设置：OpenTelemetry 链路追踪与 Prometheus 指标。"""

    tracing_enabled: bool = False  # 默认关闭，避免本地演示刷屏 Console span
    otlp_endpoint: str | None = None  # 为空则用 Console 导出器
    service_name: str = "athena-agent"
    metrics_enabled: bool = True


class K8sSettings(BaseModel):
    """
    Kubernetes 只读运维连接设置。

    功能说明：保存真实集群接入所需的 kubeconfig、context、命名空间白名单与超时。
    参数说明：
        kubeconfig：kubeconfig 文件路径，None 时用 SDK 默认查找（集群内配置或 ~/.kube/config）。
        context：kubeconfig 中的 context 名称，None 时用当前默认 context。
        namespace_allowlist：允许访问的命名空间白名单；为空列表表示不限制（本地演示友好）。
        timeout：单次 K8s API 调用的超时时间（秒），避免慢集群阻塞 Agent。
    返回值：Pydantic 配置对象。
    设计思路：把安全边界（白名单）与连接参数集中配置，工具层只消费结论不做散落判断。
    使用示例：settings.ops.kubernetes.namespace_allowlist

    🎯 面试考点：为什么白名单为空默认放行？答案：与项目其它安全项（api_keys 为空=关闭鉴权）保持一致，
    本地无集群演示零配置可跑；生产通过配置显式收窄边界。
    """

    kubeconfig: str | None = None
    context: str | None = None
    namespace_allowlist: list[str] = Field(default_factory=list)
    timeout: float = 10.0  # 单位：秒；真实 SDK 调用的 _request_timeout
    fallback_policy: Literal["allow_mock", "fail_closed"] = "allow_mock"


class PrometheusSettings(BaseModel):
    """
    CloudOps Prometheus 查询设置。

    功能说明：保存真实 Prometheus 接入开关、地址与超时；关闭时 K8s 诊断仍正常运行。
    参数说明：enabled 控制是否查询 Prometheus；base_url 是 Prometheus HTTP API 地址；timeout_seconds 是查询超时。
    返回值：Pydantic 配置对象。
    设计思路：默认关闭，保证本地无 Prometheus 时不影响 K8s 只读诊断；演示可用 mock://prometheus 打开确定性指标。
    使用示例：settings.ops.prometheus.enabled
    """

    enabled: bool = False
    base_url: str = "mock://prometheus"
    timeout_seconds: float = 5.0
    fallback_policy: Literal["allow_mock", "fail_closed"] = "allow_mock"


class OpsSecuritySettings(BaseModel):
    """CloudOps 写操作安全治理设置。"""

    default_readonly: bool = True
    allowed_resource_kinds: list[str] = Field(default_factory=lambda: ["Deployment"])
    allowed_verbs: list[str] = Field(
        default_factory=lambda: ["rollout_restart", "scale", "pause", "resume"]
    )
    blocked_actions: list[str] = Field(
        default_factory=lambda: [
            "delete namespace",
            "delete pvc",
            "patch secret",
            "rbac",
            "batch delete",
        ]
    )
    environments: list[str] = Field(default_factory=lambda: ["dev", "staging", "prod"])
    prod_write_enabled: bool = False


class OpsSettings(BaseModel):
    """
    云运维（CloudOps）设置：控制 K8s 诊断走 mock 还是真实集群。

    功能说明：mode 决定数据来源，kubernetes 保存真实接入参数。
    参数说明：
        mode：mock 表示始终用演示数据；real 表示优先真实集群，缺 kubeconfig/连接失败自动降级 mock。
        kubernetes：真实集群连接与安全边界配置。
    返回值：Pydantic 配置对象。
    设计思路：新增独立配置段，旧 config.yaml 无 ops 段时用默认值（mock）仍能启动。
    使用示例：settings.ops.mode

    🎯 面试考点：为什么默认 mock？答案：无集群环境（CI、本地）也能跑通全链路，符合“自动降级”约束。
    """

    mode: Literal["mock", "real"] = "mock"
    kubernetes: K8sSettings = Field(default_factory=K8sSettings)
    prometheus: PrometheusSettings = Field(default_factory=PrometheusSettings)
    security: OpsSecuritySettings = Field(default_factory=OpsSecuritySettings)


class AthenaSettings(BaseModel):
    """Top-level Athena settings."""

    llm: LLMSettings = Field(default_factory=LLMSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    vector_store: VectorStoreSettings = Field(default_factory=VectorStoreSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    web: WebSettings = Field(
        default_factory=WebSettings
    )  # 💡 学习提示：新增配置段用 default_factory，可保证旧 config.yaml 没有 web 段时仍能启动。
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    evidence: EvidenceSettings = Field(default_factory=EvidenceSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    task: TaskSettings = Field(default_factory=TaskSettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    ops: OpsSettings = Field(
        default_factory=OpsSettings
    )  # 💡 学习提示：云运维配置段，旧 config.yaml 无 ops 段时用默认 mock 模式启动。


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
    runtime_profile = os.getenv("ATHENA_RUNTIME_PROFILE")
    llm_enabled = os.getenv("ATHENA_LLM_ENABLED")
    llm_model = os.getenv("ATHENA_LLM_MODEL")
    execution_mode = os.getenv("ATHENA_AGENT_EXECUTION_MODE")
    log_level = os.getenv("ATHENA_LOG_LEVEL")
    web_host = os.getenv("ATHENA_WEB_HOST")
    web_port = os.getenv("ATHENA_WEB_PORT")
    web_cors = os.getenv("ATHENA_WEB_CORS_ORIGINS")
    web_session_ttl = os.getenv("ATHENA_WEB_SESSION_TTL_SECONDS")
    updated = settings.model_copy(
        deep=True
    )  # 💡 学习提示：deep=True 会复制嵌套对象，修改 updated.web 不会影响原 settings.web。
    if runtime_profile:
        if runtime_profile not in {"demo", "production"}:
            raise ConfigError(
                ErrorCode.CONFIG_INVALID,
                "ATHENA_RUNTIME_PROFILE must be demo or production",
            )
        updated.runtime.profile = cast(Literal["demo", "production"], runtime_profile)
    if llm_enabled:
        if llm_enabled.lower() not in {
            "1",
            "true",
            "yes",
            "on",
            "0",
            "false",
            "no",
            "off",
        }:
            raise ConfigError(
                ErrorCode.CONFIG_INVALID,
                "ATHENA_LLM_ENABLED must be a boolean",
            )
        updated.llm.enabled = llm_enabled.lower() in {"1", "true", "yes", "on"}
    if llm_model:
        updated.llm.model = llm_model
    if execution_mode:
        if execution_mode not in {"legacy_react", "policy_workflow"}:
            raise ConfigError(
                ErrorCode.CONFIG_INVALID,
                "ATHENA_AGENT_EXECUTION_MODE must be legacy_react or policy_workflow",
            )
        updated.agent.execution_mode = cast(
            Literal["legacy_react", "policy_workflow"], execution_mode
        )
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
    # 生产部署常用环境变量注入敏感/环境相关配置
    redis_url = os.getenv("ATHENA_REDIS_URL")
    if redis_url:
        updated.cache.redis_url = redis_url
    database_url = os.getenv("ATHENA_DATABASE_URL")
    if database_url:
        updated.database.url = database_url
    database_echo = os.getenv("ATHENA_DATABASE_ECHO")
    if database_echo:
        updated.database.echo = database_echo.lower() in {"1", "true", "yes", "on"}
    database_auto_migrate = os.getenv("ATHENA_DATABASE_AUTO_MIGRATE")
    if database_auto_migrate:
        updated.database.auto_migrate = database_auto_migrate.lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    queue_stream_name = os.getenv("ATHENA_QUEUE_STREAM_NAME")
    if queue_stream_name:
        updated.queue.stream_name = queue_stream_name
    queue_consumer_group = os.getenv("ATHENA_QUEUE_CONSUMER_GROUP")
    if queue_consumer_group:
        updated.queue.consumer_group = queue_consumer_group
    evidence_root = os.getenv("ATHENA_EVIDENCE_LOCAL_ROOT")
    if evidence_root:
        updated.evidence.local_root = evidence_root
    api_keys = os.getenv("ATHENA_API_KEYS")
    if api_keys:
        # 格式：key1:tenantA,key2:tenantB
        parsed: dict[str, str] = {}
        for item in api_keys.split(","):
            if ":" in item:
                key, tenant = item.split(":", 1)
                if key.strip():
                    parsed[key.strip()] = tenant.strip() or "public"
        if parsed:
            updated.security.api_keys = parsed
            updated.security.require_auth = True
    alert_tokens = os.getenv("ATHENA_ALERT_INTEGRATION_TOKENS")
    if alert_tokens:
        parsed_alert_tokens: dict[str, str] = {}
        for item in alert_tokens.split(","):
            if ":" in item:
                token, tenant = item.split(":", 1)
                if token.strip():
                    parsed_alert_tokens[token.strip()] = tenant.strip() or "public"
        if parsed_alert_tokens:
            updated.security.alert_integration_tokens = parsed_alert_tokens
    secret_master_key = os.getenv("ATHENA_SECRET_MASTER_KEY")
    if secret_master_key:
        updated.security.secret_master_key = secret_master_key
    # 云运维（K8s）常用环境变量注入，便于容器/CI 不改 YAML 切换 mock/real
    ops_mode = os.getenv("ATHENA_OPS_MODE")
    if ops_mode:
        if ops_mode not in {"mock", "real"}:
            raise ConfigError(
                ErrorCode.CONFIG_INVALID,
                "ATHENA_OPS_MODE must be mock or real",
            )
        updated.ops.mode = cast(Literal["mock", "real"], ops_mode)
    k8s_kubeconfig = os.getenv("ATHENA_OPS_K8S_KUBECONFIG")
    if k8s_kubeconfig:
        updated.ops.kubernetes.kubeconfig = k8s_kubeconfig
    k8s_context = os.getenv("ATHENA_OPS_K8S_CONTEXT")
    if k8s_context:
        updated.ops.kubernetes.context = k8s_context
    k8s_allowlist = os.getenv("ATHENA_OPS_K8S_NAMESPACE_ALLOWLIST")
    if k8s_allowlist:
        updated.ops.kubernetes.namespace_allowlist = [
            ns.strip() for ns in k8s_allowlist.split(",") if ns.strip()
        ]  # 💡 学习提示：逗号分隔让一个环境变量能配置多个命名空间白名单。
    k8s_timeout = os.getenv("ATHENA_OPS_K8S_TIMEOUT")
    if k8s_timeout:
        updated.ops.kubernetes.timeout = float(k8s_timeout)
    k8s_fallback_policy = os.getenv("ATHENA_OPS_K8S_FALLBACK_POLICY")
    if k8s_fallback_policy:
        if k8s_fallback_policy not in {"allow_mock", "fail_closed"}:
            raise ConfigError(
                ErrorCode.CONFIG_INVALID,
                "ATHENA_OPS_K8S_FALLBACK_POLICY must be allow_mock or fail_closed",
            )
        updated.ops.kubernetes.fallback_policy = cast(
            Literal["allow_mock", "fail_closed"], k8s_fallback_policy
        )
    prometheus_enabled = os.getenv("ATHENA_OPS_PROMETHEUS_ENABLED")
    if prometheus_enabled:
        updated.ops.prometheus.enabled = prometheus_enabled.lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    prometheus_base_url = os.getenv("ATHENA_OPS_PROMETHEUS_BASE_URL")
    if prometheus_base_url:
        updated.ops.prometheus.base_url = prometheus_base_url
    prometheus_timeout = os.getenv("ATHENA_OPS_PROMETHEUS_TIMEOUT")
    if prometheus_timeout:
        updated.ops.prometheus.timeout_seconds = float(prometheus_timeout)
    prometheus_fallback_policy = os.getenv("ATHENA_OPS_PROMETHEUS_FALLBACK_POLICY")
    if prometheus_fallback_policy:
        if prometheus_fallback_policy not in {"allow_mock", "fail_closed"}:
            raise ConfigError(
                ErrorCode.CONFIG_INVALID,
                "ATHENA_OPS_PROMETHEUS_FALLBACK_POLICY must be allow_mock or fail_closed",
            )
        updated.ops.prometheus.fallback_policy = cast(
            Literal["allow_mock", "fail_closed"], prometheus_fallback_policy
        )
    prod_write_enabled = os.getenv("ATHENA_OPS_PROD_WRITE_ENABLED")
    if prod_write_enabled:
        updated.ops.security.prod_write_enabled = prod_write_enabled.lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    return updated


def production_readiness_issues(settings: AthenaSettings) -> tuple[str, ...]:
    """Return stable, non-secret reasons that make production not ready."""
    issues: list[str] = []
    # LIVE is a fact boundary, not a production-only preference. A demo may use
    # MOCK explicitly, but a configured LIVE environment must never fail over.
    if (
        settings.ops.mode == "real"
        and settings.ops.kubernetes.fallback_policy != "fail_closed"
    ):
        issues.append("LIVE_FALLBACK_MUST_FAIL_CLOSED")
    if settings.runtime.profile != "production":
        return tuple(issues)

    if not settings.security.require_auth:
        issues.append("AUTH_REQUIRED")
    if not settings.security.api_keys and not settings.security.jwt_secret:
        issues.append("AUTH_CREDENTIALS_MISSING")
    if not settings.security.secret_master_key:
        issues.append("SECRET_MASTER_KEY_REQUIRED")
    if not settings.security.alert_integration_tokens:
        issues.append("ALERT_INTEGRATION_TOKEN_REQUIRED")
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
    if settings.ops.mode != "real":
        issues.append("LIVE_ENVIRONMENT_REQUIRED")
    if not settings.ops.kubernetes.namespace_allowlist:
        issues.append("ENV_SCOPE_REQUIRED")
    if (
        settings.ops.mode != "real"
        and settings.ops.kubernetes.fallback_policy != "fail_closed"
    ):
        issues.append("LIVE_FALLBACK_MUST_FAIL_CLOSED")
    if settings.ops.prometheus.enabled:
        if not settings.ops.prometheus.base_url.startswith(("http://", "https://")):
            issues.append("PROMETHEUS_LIVE_ENDPOINT_REQUIRED")
        if settings.ops.prometheus.fallback_policy != "fail_closed":
            issues.append("PROMETHEUS_FALLBACK_MUST_FAIL_CLOSED")
    if not settings.ops.security.default_readonly:
        issues.append("READONLY_DEFAULT_REQUIRED")
    if settings.ops.security.prod_write_enabled:
        issues.append("PRODUCTION_WRITE_PATH_FORBIDDEN")
    return tuple(issues)


"""
🤔 思考题：

1. 如果要支持生产环境和开发环境两套 config.yaml，你会怎么扩展 load_settings？
2. 为什么环境变量覆盖要放在 YAML 校验之后？
3. ATHENA_WEB_CORS_ORIGINS="https://a.com,https://b.com" 会被解析成什么？
4. ⚡ 优化建议：当前 int(web_port) 失败会抛 ValueError，未来可以把它包装成 ConfigError，错误信息更友好。
"""
