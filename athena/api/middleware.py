"""
📦 HTTP 中间件集合
📍 架构位置：接口服务层横切面，位于 CORS 之后、路由之前。
🎯 核心作用：为每个请求分配/透传链路 ID（trace_id），写入响应头并记录结构化访问日志。
🔗 依赖：api.response 的 trace 上下文；被 server.create_app 安装。
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from athena.api.response import get_trace_id, new_trace_id, set_trace_id
from athena.api.schemas import ErrorResponse
from athena.infra.resilience import (
    HierarchicalRateLimiter,
    RateLimiter,
    RateLimitExceeded,
)
from athena.observability.prometheus import PrometheusMetrics
from athena.observability.trace_context import (
    TRACEPARENT_HEADER,
    resolve_traceparent,
    set_traceparent,
)

logger = logging.getLogger("athena.access")

TRACE_HEADER = "X-Trace-Id"
API_KEY_HEADER = "X-API-Key"


def install_metrics_middleware(app: FastAPI, metrics: PrometheusMetrics) -> None:
    """安装 Prometheus 指标采集中间件：记录每个请求的量、耗时与错误。"""

    @app.middleware("http")
    async def collect_metrics(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started_at = time.perf_counter()
        # 用路由模板而非真实路径，避免带 ID 的路径造成指标基数爆炸
        route = request.scope.get("route")
        path = getattr(route, "path", None) or request.url.path
        try:
            response = await call_next(request)
        except Exception:
            duration = time.perf_counter() - started_at
            metrics.observe_request(request.method, path, 500, duration)
            raise
        duration = time.perf_counter() - started_at
        metrics.observe_request(request.method, path, response.status_code, duration)
        return response


def install_trace_middleware(app: FastAPI) -> None:
    """安装链路 ID + 访问日志中间件。"""

    @app.middleware("http")
    async def trace_and_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # 优先复用上游传入的 trace_id（网关/前端），否则新建，保证跨服务链路可拼接
        trace_id = request.headers.get(TRACE_HEADER) or new_trace_id()
        set_trace_id(trace_id)
        traceparent = resolve_traceparent(request.headers.get(TRACEPARENT_HEADER))
        set_traceparent(traceparent)
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
        response.headers[TRACEPARENT_HEADER] = traceparent
        logger.info(
            "request method=%s path=%s status=%s trace_id=%s duration_ms=%.2f",
            request.method,
            request.url.path,
            response.status_code,
            trace_id,
            duration_ms,
        )
        return response


def install_rate_limit_middleware(
    app: FastAPI, limiter: RateLimiter | HierarchicalRateLimiter
) -> None:
    """安装网关层限流中间件：全局 + 单租户固定窗口限流，超限返回 429。"""

    @app.middleware("http")
    async def rate_limit(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # 仅对业务 API 限流，放行静态资源/文档/健康检查
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        api_key = request.headers.get(API_KEY_HEADER)
        tenant = (
            getattr(request.app.state.settings.security, "api_keys", {}).get(api_key)
            if api_key
            else None
        ) or request.app.state.settings.security.default_tenant
        try:
            result = (
                limiter.check(tenant, request.url.path)
                if isinstance(limiter, HierarchicalRateLimiter)
                else limiter.check(tenant)
            )
            if inspect.isawaitable(result):
                await result
        except RateLimitExceeded as exc:
            return JSONResponse(
                status_code=429,
                content=ErrorResponse(
                    error_code="RATE_LIMITED",
                    message=str(exc),
                    trace_id=get_trace_id(),
                ).model_dump(),
                headers={"Retry-After": str(exc.retry_after_seconds)},
            )
        return await call_next(request)
