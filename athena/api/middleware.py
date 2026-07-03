"""
📦 HTTP 中间件集合
📍 架构位置：接口服务层横切面，位于 CORS 之后、路由之前。
🎯 核心作用：为每个请求分配/透传链路 ID（trace_id），写入响应头并记录结构化访问日志。
🔗 依赖：api.response 的 trace 上下文；被 server.create_app 安装。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from starlette.responses import Response

from athena.api.response import new_trace_id, set_trace_id

logger = logging.getLogger("athena.access")

TRACE_HEADER = "X-Trace-Id"


def install_trace_middleware(app: FastAPI) -> None:
    """安装链路 ID + 访问日志中间件。"""

    @app.middleware("http")
    async def trace_and_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # 优先复用上游传入的 trace_id（网关/前端），否则新建，保证跨服务链路可拼接
        trace_id = request.headers.get(TRACE_HEADER) or new_trace_id()
        set_trace_id(trace_id)
        started_at = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started_at) * 1000
            logger.exception(
                "request_failed method=%s path=%s trace_id=%s duration_ms=%.2f",
                request.method,
                request.url.path,
                trace_id,
                duration_ms,
            )
            raise
        duration_ms = (time.perf_counter() - started_at) * 1000
        response.headers[TRACE_HEADER] = trace_id
        logger.info(
            "request method=%s path=%s status=%s trace_id=%s duration_ms=%.2f",
            request.method,
            request.url.path,
            response.status_code,
            trace_id,
            duration_ms,
        )
        return response
