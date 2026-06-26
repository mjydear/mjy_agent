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
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from athena.api.routes import (
    benchmark,
    chat,
    cloud_ops,
    metrics,
    session,
    traces,
    workflow,
)
from athena.api.schemas import ErrorResponse
from athena.api.services import ApiServiceError, AthenaWebService, static_directory
from athena.cli.main import build_agent
from athena.config import AthenaSettings, load_settings
from athena.exceptions import AthenaError
from athena.logging import configure_logging

logger = logging.getLogger(__name__)


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
    configure_logging(resolved_settings.logging.level)
    app = FastAPI(title="Athena Agent Web Console", version="0.1.0")
    app.state.service = service or AthenaWebService(
        agent_factory=lambda: build_agent(
            None
        ),  # 💡 学习提示：用 lambda 延迟创建 Agent，避免服务启动时就要求 API Key 可用。
        session_ttl_seconds=resolved_settings.web.session_ttl_seconds,
    )
    _configure_middlewares(app, resolved_settings)
    _configure_exception_handlers(app)
    _mount_routes(app)
    _mount_static(app, static_directory())
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

    @app.middleware("http")
    async def log_requests(
        request: Request, call_next: Callable[[Request], Awaitable[object]]
    ) -> object:
        """
        记录每个请求的基础访问日志。

        功能说明：统计请求处理耗时并写入日志。
        参数说明：
            request：当前 HTTP 请求对象。
            call_next：FastAPI 提供的“继续交给下一个处理环节”的函数。
        返回值：下游路由生成的响应对象。
        设计思路：使用中间件统一记录日志，避免每个路由都重复写计时代码。
        使用示例：浏览器请求 /api/metrics 时，这个函数会自动被 FastAPI 调用。
        """
        started_at = (
            time.perf_counter()
        )  # 💡 学习提示：perf_counter 适合测耗时，比 time.time 更稳定。
        response = await call_next(
            request
        )  # 💡 学习提示：这里必须 await，否则请求不会真正进入后面的路由。
        duration_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "api request method=%s path=%s duration_ms=%.2f",
            request.method,
            request.url.path,
            duration_ms,
        )
        return response


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
        """把服务层可预期错误转换成 400 响应。"""
        logger.warning(
            "api service error path=%s code=%s message=%s",
            request.url.path,
            exc.error_code,
            exc.message,
        )
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error_code=exc.error_code, message=exc.message
            ).model_dump(),
        )

    @app.exception_handler(AthenaError)
    async def handle_athena_error(request: Request, exc: AthenaError) -> JSONResponse:
        """把 Athena 核心异常转换成标准 JSON 响应。"""
        logger.warning(
            "athena error path=%s code=%s message=%s",
            request.url.path,
            exc.code,
            exc.message,
        )
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error_code=str(exc.code), message=exc.message
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """兜底处理未知异常，避免内部堆栈泄露到浏览器。"""
        logger.exception("unexpected api error path=%s", request.url.path)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error_code="INTERNAL_ERROR", message="Internal server error"
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
    app.include_router(workflow.router)
    app.include_router(cloud_ops.router)
    app.include_router(traces.router)
    app.include_router(metrics.router)
    app.include_router(benchmark.router)


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
