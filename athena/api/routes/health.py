"""
📦 模块名称：健康探针 API 路由
📍 架构位置：HTTP 路由层，面向 K8s liveness/readiness 探针与负载均衡健康检查。
🎯 核心作用：提供 GET /healthz（存活）与 GET /readyz（就绪，探测依赖连通与优雅下线状态）。
🔗 依赖关系：读取 app.state.cache（连通性）与 app.state.draining（优雅关闭标志）。
💡 设计思路：liveness 只反映进程存活；readiness 反映“能否接流量”，下线中或依赖不可用返回 503。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from athena.config import production_readiness_issues
from athena.exceptions import AthenaError

router = APIRouter(tags=["health"])


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """存活探针：进程能响应即视为存活，不探测下游依赖。"""
    return {"status": "alive"}


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    """
    就绪探针：优雅下线中或关键依赖不可用时返回 503，让 K8s 停止导流。

    检查项：draining 标志（优雅关闭中）+ 缓存后端连通性。
    """
    state = request.app.state
    if getattr(state, "draining", False):
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "ready": False,
                "reason_code": "DRAINING",
                "components": [],
            },
        )

    settings = state.settings
    config_issues = list(production_readiness_issues(settings))
    components: list[dict[str, object]] = [
        {
            "component": "configuration",
            "configured_backend": settings.runtime.profile,
            "active_backend": "valid" if not config_issues else "invalid",
            "status": "healthy" if not config_issues else "unavailable",
            "reason_code": config_issues[0] if config_issues else None,
        }
    ]

    cache = getattr(state, "cache", None)
    cache_component = dict(
        getattr(
            state,
            "cache_component",
            {
                "component": "cache",
                "configured_backend": "unknown",
                "active_backend": "unknown",
                "status": "unavailable",
                "reason_code": "CACHE_UNAVAILABLE",
            },
        )
    )
    if cache is not None:
        try:
            cache.get("__readyz__")  # 轻量连通性探测：Redis 不可用会抛异常
        except Exception:  # noqa: BLE001
            cache_component.update(
                status="unavailable", reason_code="CACHE_CONNECTION_FAILED"
            )
    components.append(cache_component)

    database = getattr(state, "database", None)
    if database is None:
        components.append(
            {
                "component": "database",
                "configured_backend": "disabled",
                "active_backend": "disabled",
                "status": "healthy" if settings.runtime.profile != "production" else "unavailable",
                "reason_code": (
                    None
                    if settings.runtime.profile != "production"
                    else "DATABASE_BACKEND_REQUIRED"
                ),
            }
        )
    else:
        database_component: dict[str, object] = {
            "component": "database",
            "configured_backend": "postgresql",
            "active_backend": "postgresql",
            "status": "healthy",
            "reason_code": None,
        }
        try:
            async with database.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - readiness must not expose connection details
            database_component.update(
                active_backend="unavailable",
                status="unavailable",
                reason_code="DATABASE_CONNECTION_FAILED",
            )
        components.append(database_component)

    k8s_client = getattr(state, "ops_k8s_client", None)
    if k8s_client is not None:
        configured_k8s = "live" if settings.ops.mode == "real" else "mock"
        if settings.ops.mode == "real" and settings.ops.kubernetes.namespace_allowlist:
            try:
                k8s_client.list_pods(settings.ops.kubernetes.namespace_allowlist[0])
            except AthenaError:
                pass
        active_k8s = k8s_client.last_data_origin
        if configured_k8s == active_k8s:
            k8s_status = "healthy"
        elif active_k8s in {"unknown", "unavailable"}:
            k8s_status = "unavailable"
        else:
            k8s_status = "degraded"
        components.append(
            {
                "component": "kubernetes",
                "configured_backend": configured_k8s,
                "active_backend": active_k8s,
                "status": k8s_status,
                "reason_code": k8s_client.last_error_code,
            }
        )

    prometheus_client = getattr(state, "ops_prometheus_client", None)
    if prometheus_client is not None:
        if prometheus_client.enabled and settings.ops.mode == "real":
            prometheus_client.query("readiness", "up")
        configured_prometheus = "live" if prometheus_client.enabled else "disabled"
        active_prometheus = (
            prometheus_client.last_data_origin
            if prometheus_client.enabled
            else "disabled"
        )
        components.append(
            {
                "component": "prometheus",
                "configured_backend": configured_prometheus,
                "active_backend": active_prometheus,
                "status": (
                    "healthy"
                    if configured_prometheus == active_prometheus
                    else "degraded"
                ),
                "reason_code": prometheus_client.last_error_code,
            }
        )

    production = settings.runtime.profile == "production"
    critical_degradation = (
        production and cache_component.get("status") != "healthy"
    ) or (
        production
        and any(
            item["component"] == "database" and item["status"] != "healthy"
            for item in components
        )
    ) or (
        settings.ops.mode == "real"
        and any(
            item["component"] == "kubernetes" and item["status"] != "healthy"
            for item in components
        )
    )
    ready = not config_issues and not critical_degradation
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "ready": ready,
            "profile": settings.runtime.profile,
            "reason_code": (
                config_issues[0]
                if config_issues
                else ("CRITICAL_DEPENDENCY_DEGRADED" if not ready else None)
            ),
            "components": components,
        },
    )
