"""
📦 模块名称：Athena Web Console FastAPI 应用工厂
📍 架构位置：接口服务层入口，位于 CLI 启动命令和 API 路由/服务层之间。
🎯 核心作用：创建 Web 控制台的 FastAPI 应用，统一挂载路由、静态页面、中间件和异常处理。
🔗 依赖关系：依赖 config 配置、cli.build_agent、api.routes、AthenaWebService；被 `athena web` 命令、测试和 uvicorn 启动器依赖。
💡 设计思路：使用“应用工厂 + 依赖注入”模式，把 app 创建过程放进 create_app()，测试时可以注入假 service，生产时创建真实 service。
📚 学习重点：重点看 create_app() 如何把配置、业务服务、路由、静态资源和错误处理拼成一个可运行的 Web 服务。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from athena.api.routes import (
    alerts,
    audit,
    benchmark,
    chat,
    cloud_ops,
    health,
    knowledge,
    metrics,
    prometheus,
    session,
    tasks,
    traces,
    workflow,
)
from athena.api.middleware import (
    install_metrics_middleware,
    install_rate_limit_middleware,
    install_trace_middleware,
)
from athena.api.response import get_trace_id
from athena.api.schemas import ErrorResponse
from athena.api.services import ApiServiceError, AthenaWebService, static_directory
from athena.api.task_manager import AsyncTaskManager
from athena.api.idempotency import IdempotencyManager
from athena.api.session_store import SessionStore
from athena.api.task_store import BenchmarkStore, MetricsStore, TaskStore
from athena.tools.audit_chain import HashChainAuditStore
from athena.infra.cache import create_cache
from athena.infra.embeddings import create_embedding_provider
from athena.infra.resilience import RateLimiter
from athena.infra.vector_db import InMemoryVectorStore, create_vector_store
from athena.memory.knowledge_base import KnowledgeBaseManager
from athena.memory.ops_knowledge import OpsKnowledgeBase
from athena.observability.incident import IncidentManager
from athena.observability.prometheus import PrometheusMetrics
from athena.observability.otel import setup_tracing
from athena.cli.main import build_agent
from athena.config import AthenaSettings, load_settings
from athena.exceptions import AthenaError, ConfigError, ErrorCode
from athena.logging import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> "AsyncIterator[None]":
    """
    应用生命周期：负责优雅关闭（graceful shutdown）。

    功能说明：收到停机信号时，先把 draining 置位让 /readyz 返回 503 停止导流，
    再排空异步任务、关闭缓存连接，最后释放资源。
    设计思路：滚动更新/缩容时先摘流量再退出，避免请求被硬切断。
    """
    # 启动阶段：装配 Skill 自进化守护进程（阶段②-C）——从持久化恢复技能库，再周期性复盘轨迹进化 Skill。
    curator = None
    service = getattr(app.state, "service", None)
    skill_library = getattr(app.state, "skill_library", None)
    if service is not None and skill_library is not None:
        try:
            await skill_library.load()  # 从持久化后端恢复已学 Skill，重启不丢
            from athena.learning import CuratorDaemon, SkillEvolver, SkillValidator
            from athena.tools.sandbox import SecuritySandbox

            evolver = SkillEvolver(
                skill_library,
                SkillValidator(SecuritySandbox()),
                feedback_store=getattr(app.state, "feedback_store", None),
            )
            curator = CuratorDaemon(service.tracer, job=evolver.job, interval_seconds=60.0)
            await curator.start()
            app.state.curator = curator
            logger.info("skill self-evolution curator started")
        except Exception as exc:  # noqa: BLE001 - 进化是增强项，失败不应阻断服务启动
            logger.warning("curator start failed: %s", exc)

    yield  # 运行阶段：state 已由 create_app 装配完成
    # 关闭阶段：优雅下线
    app.state.draining = True
    logger.info("graceful shutdown: draining traffic, closing resources")
    if curator is not None:
        try:
            await curator.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("curator stop error: %s", exc)
    task_manager = getattr(app.state, "task_manager", None)
    if task_manager is not None:
        shutdown = getattr(task_manager, "shutdown", None)
        if callable(shutdown):
            try:
                result = shutdown()
                if hasattr(result, "__await__"):
                    await result  # 排空进行中的异步任务
            except Exception as exc:  # noqa: BLE001
                logger.warning("task manager shutdown error: %s", exc)
    cache = getattr(app.state, "cache", None)
    if cache is not None:
        try:
            cache.close()  # 关闭 Redis 连接池
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache close error: %s", exc)


def _build_workflow_llm(settings: AthenaSettings) -> object | None:
    """
    为工作流规划/执行构建 LLM 客户端，缺凭证时返回 None 触发降级。

    功能说明：复用 LLMClientFactory + ResilientLLMClient 构造韧性 LLM。
    参数说明：settings 提供 provider/model 等 LLM 配置。
    返回值：LLMClient 或 None（无 API Key/构造失败时）。
    设计思路：企业级“真实实现 + 自动降级”——有凭证走真实 LLM，否则规则/占位兜底。
    """
    try:
        from athena.infra.llm import LLMClientFactory
        from athena.infra.resilience import ResilientLLMClient, RetryPolicy, make_breaker

        client = LLMClientFactory.create(
            provider=settings.llm.provider,
            model=settings.llm.model,
            temperature=settings.llm.temperature,
            max_tokens=settings.llm.max_tokens,
        )
        return ResilientLLMClient(
            client, retry_policy=RetryPolicy(), breaker=make_breaker("workflow-llm")
        )
    except Exception as exc:
        logger.info("Workflow LLM unavailable, using rule/placeholder fallback: %s", exc)
        return None


def create_app(
    settings: AthenaSettings | None = None, service: AthenaWebService | None = None
) -> FastAPI:
    """
    创建并配置 Athena Web Console 的 FastAPI 应用。

    功能说明：把配置读取、日志初始化、服务层创建、路由挂载和静态页面挂载集中到一个入口。
    参数说明：
        settings：已经校验过的 AthenaSettings；不传时自动读取 config.yaml。
        service：可选的 AthenaWebService；测试时传假 service，生产时自动创建真实 service。
    返回值：配置完成的 FastAPI app，可以交给 uvicorn 启动。
    设计思路：这是“应用工厂”模式。导入模块时不直接创建全局 app，避免测试和多环境启动时难以替换依赖。
    使用示例：
        app = create_app()
        uvicorn.run(app, host="127.0.0.1", port=8000)

    🎯 面试考点：为什么要允许传入 service？答案：这样 API 测试可以绕过真实 LLM，用假 Agent 验证路由行为。
    """
    resolved_settings = (
        settings or load_settings()
    )  # 💡 学习提示：这里延迟读取配置，方便测试传入自定义 settings。
    # 生产审计持久化硬约束：require_durable_audit=True 时必须配置 Redis，否则审计走内存
    # 后端重启即丢，违反 SRE 可审计要求，启动即快速失败而非静默降级。
    if (
        resolved_settings.ops.require_durable_audit
        and not resolved_settings.cache.redis_url
    ):
        raise ConfigError(
            ErrorCode.CONFIG_INVALID,
            "ops.require_durable_audit is enabled but no Redis is configured "
            "(set cache.redis_url or ATHENA_REDIS_URL); audit chain would not be durable",
        )
    configure_logging(resolved_settings.logging.level)
    app = FastAPI(
        title="Athena Agent Web Console", version="0.1.0", lifespan=_lifespan
    )
    app.state.settings = resolved_settings  # 供鉴权/限流等依赖读取
    app.state.draining = False  # 优雅关闭标志：置位后 /readyz 返回 503 停止导流
    # 企业级基础设施：缓存（Redis 可降级）先建好，供会话持久化等复用
    app.state.cache = create_cache(
        resolved_settings.cache.redis_url, namespace=resolved_settings.cache.namespace
    )
    # Skill 自进化闭环基础设施：技能库（持久化 + 语义召回）与人工反馈存储。
    from athena.memory.feedback import FeedbackStore
    from athena.memory.skill import SkillLibrary

    skill_library = SkillLibrary(cache=app.state.cache)
    feedback_store = FeedbackStore(cache=app.state.cache)
    app.state.skill_library = skill_library
    app.state.feedback_store = feedback_store
    app.state.service = service or AthenaWebService(
        agent_factory=lambda: build_agent(
            None
        ),  # 💡 学习提示：用 lambda 延迟创建 Agent，避免服务启动时就要求 API Key 可用。
        session_ttl_seconds=resolved_settings.web.session_ttl_seconds,
        session_store=SessionStore(
            app.state.cache,
            ttl_seconds=resolved_settings.web.session_ttl_seconds,
        ),  # 会话落 Redis：重启不丢、多副本共享
        ops_knowledge=OpsKnowledgeBase(
            cache=app.state.cache,
            vector_store=create_vector_store(resolved_settings),
            embedding_provider=create_embedding_provider(resolved_settings),
        ),  # 运维知识库：持久化 + 语义召回（向量/嵌入缺失自动降级）
        workflow_llm=_build_workflow_llm(
            resolved_settings
        ),  # 工作流 LLM：有凭证走真实规划/执行，缺失则降级规则/占位
        embedding_provider=create_embedding_provider(
            resolved_settings
        ),  # 评测嵌入：Benchmark 语义评分（嵌入缺失自动降级关键词）
        task_store=TaskStore(
            app.state.cache, ttl_seconds=resolved_settings.web.session_ttl_seconds
        ),  # 任务落 Redis：重启不丢、多副本共享
        metrics_store=MetricsStore(app.state.cache),  # 运行指标落 Redis：多副本聚合
        benchmark_store=BenchmarkStore(app.state.cache),  # 评测报告落 Redis
        audit_store=HashChainAuditStore(app.state.cache),  # 审计哈希链落 Redis：防篡改
        skill_library=skill_library,  # 技能库：Agent 召回 + Curator 进化写入（阶段②-C/D）
        feedback_store=feedback_store,  # 人工反馈：驱动 Skill 修正/降权（阶段②-B）
    )
    # 异步任务管理器、幂等管理器
    app.state.task_manager = AsyncTaskManager(
        max_concurrency=resolved_settings.task.max_concurrency,
        result_ttl_seconds=resolved_settings.task.result_ttl_seconds,
        thread_pool_workers=resolved_settings.task.thread_pool_workers,
    )
    app.state.idempotency = IdempotencyManager(
        app.state.cache,
        ttl_seconds=resolved_settings.cache.idempotency_ttl_seconds,
    )
    app.state.rate_limiter = RateLimiter(
        app.state.cache,
        global_per_minute=resolved_settings.rate_limit.global_per_minute,
        per_tenant_per_minute=resolved_settings.rate_limit.per_tenant_per_minute,
    )
    app.state.incident_manager = IncidentManager()
    # 知识库管理后台：向量库按配置选 Milvus/内存 + 嵌入模型（缺凭证降级哈希）
    app.state.knowledge_base = KnowledgeBaseManager(
        create_vector_store(resolved_settings),
        embedding_provider=create_embedding_provider(resolved_settings),
    )
    # 可观测性：Prometheus 指标 + 故障事件联动（L3+ 计入指标并告警）
    app.state.prometheus = PrometheusMetrics()

    def _alert_sink(inc: object) -> None:
        app.state.prometheus.observe_incident(inc.severity.name)
        logger.error(
            "ALERT severity=%s strategy=%s code=%s message=%s",
            inc.severity.name,
            inc.strategy,
            inc.error_code,
            inc.message,
        )

    app.state.incident_manager._alert_sink = _alert_sink  # L3+ 触发上报
    _configure_middlewares(app, resolved_settings)
    if resolved_settings.observability.metrics_enabled:
        install_metrics_middleware(app, app.state.prometheus)
    if resolved_settings.rate_limit.enabled:
        install_rate_limit_middleware(app, app.state.rate_limiter)
    install_trace_middleware(app)  # 链路 ID + 结构化访问日志（最外层，最先执行）
    _configure_exception_handlers(app)
    _mount_routes(app)
    _mount_static(app, static_directory())
    if resolved_settings.observability.tracing_enabled:
        setup_tracing(
            app,
            service_name=resolved_settings.observability.service_name,
            otlp_endpoint=resolved_settings.observability.otlp_endpoint,
        )
    return app


def _configure_middlewares(app: FastAPI, settings: AthenaSettings) -> None:
    """
    配置跨域和请求日志中间件。

    功能说明：给 Web 前端开放允许的跨域来源，并记录每个 HTTP 请求的耗时。
    参数说明：
        app：需要被配置的 FastAPI 应用。
        settings：包含 web.cors_origins 的全局配置。
    返回值：None，直接修改 app。
    设计思路：中间件像“服务门口的检查员”，请求进入路由前后都能统一处理横切逻辑。
    使用示例：_configure_middlewares(app, settings)
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.web.cors_origins,  # 💡 学习提示：CORS 放配置里，是为了本地演示和部署环境可以使用不同前端域名。
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 注：链路 ID 与访问日志由 install_trace_middleware 统一处理，避免重复记录。


def _configure_exception_handlers(app: FastAPI) -> None:
    """
    注册统一 JSON 异常处理器。

    功能说明：把业务错误、Athena 核心错误和未知异常都转换成稳定的 ErrorResponse。
    参数说明：app 是需要注册处理器的 FastAPI 应用。
    返回值：None。
    设计思路：统一异常出口可以避免把 Python 堆栈直接暴露给前端，也让前端错误处理更简单。
    使用示例：service 抛 ApiServiceError 时，前端会收到 {error_code, message}。

    🔍 原理讲解：
    FastAPI 发现路由抛异常后，会按异常类型寻找对应 handler。
    举个例子：
    service 抛 ApiServiceError → handle_api_error() 捕获 → 返回 400 JSON。
    """

    @app.exception_handler(ApiServiceError)
    async def handle_api_error(request: Request, exc: ApiServiceError) -> JSONResponse:
        """把服务层可预期错误转换成语义化 HTTP 响应（默认 400，可为 401/409 等）。"""
        logger.warning(
            "api service error path=%s code=%s message=%s trace_id=%s",
            request.url.path,
            exc.error_code,
            exc.message,
            get_trace_id(),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error_code=exc.error_code, message=exc.message, trace_id=get_trace_id()
            ).model_dump(),
        )

    @app.exception_handler(AthenaError)
    async def handle_athena_error(request: Request, exc: AthenaError) -> JSONResponse:
        """把 Athena 核心异常转换成标准 JSON 响应。"""
        logger.warning(
            "athena error path=%s code=%s message=%s trace_id=%s",
            request.url.path,
            exc.code,
            exc.message,
            get_trace_id(),
        )
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error_code=str(exc.code), message=exc.message, trace_id=get_trace_id()
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """兜底处理未知异常，避免内部堆栈泄露到浏览器。"""
        logger.exception("unexpected api error path=%s trace_id=%s", request.url.path, get_trace_id())
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error_code="INTERNAL_ERROR",
                message="Internal server error",
                trace_id=get_trace_id(),
            ).model_dump(),
        )


def _mount_routes(app: FastAPI) -> None:
    """
    挂载所有领域路由模块。

    功能说明：把 sessions/chat/workflow/traces/metrics/benchmark 等路由注册到同一个 app。
    参数说明：app 是 FastAPI 应用。
    返回值：None。
    设计思路：路由按领域拆文件，入口集中挂载，既清晰又方便以后增删模块。
    使用示例：_mount_routes(app)
    """
    app.include_router(session.router)
    app.include_router(chat.router)
    app.include_router(tasks.router)
    app.include_router(workflow.router)
    app.include_router(cloud_ops.router)
    app.include_router(traces.router)
    app.include_router(metrics.router)
    app.include_router(prometheus.router)
    app.include_router(knowledge.router)
    app.include_router(benchmark.router)
    app.include_router(audit.router)  # 审计哈希链：/api/audit/events、/api/audit/verify
    app.include_router(alerts.router)  # 告警接入：/api/alerts/webhook
    app.include_router(health.router)  # 健康探针：/healthz 存活、/readyz 就绪


def _mount_static(app: FastAPI, directory: Path) -> None:
    """
    挂载 Web Console 静态资源和首页。

    功能说明：让浏览器访问 `/` 时返回 index.html，访问 `/static/app.js` 时返回前端脚本。
    参数说明：
        app：FastAPI 应用。
        directory：静态文件目录，一般是 athena/web/static。
    返回值：None。
    设计思路：前端不使用构建工具，所以直接由 FastAPI 托管静态文件，部署和演示都更简单。
    使用示例：_mount_static(app, Path("athena/web/static"))
    """
    if directory.exists():
        app.mount(
            "/static", StaticFiles(directory=directory), name="static"
        )  # 💡 学习提示：静态目录不存在时不挂载，方便纯 API 测试环境运行。

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            """
            返回 Web Console 的 HTML 外壳。

            功能说明：浏览器打开根路径时加载控制台页面。
            参数说明：无，FastAPI 自动处理 HTTP 请求。
            返回值：index.html 文件响应。
            设计思路：首页不进入 OpenAPI 文档，所以设置 include_in_schema=False。
            使用示例：浏览器访问 http://127.0.0.1:8000/。
            """
            return FileResponse(directory / "index.html")


"""
🤔 思考题：

1. 如果要把 Web Console 部署到公网，CORS 和 host 应该怎么配置才更安全？
2. 这里为什么把业务逻辑放在 AthenaWebService，而不是直接写在 server.py？
3. 如果前端变成 React/Vue 构建产物，_mount_static() 需要怎么调整？
4. ⚡ 优化建议：call_next 的类型可以进一步改成 Awaitable[Response]，这样静态类型检查会更精确。
"""
