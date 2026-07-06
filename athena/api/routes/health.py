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
            status_code=503, content={"status": "draining", "ready": False}
        )
    cache = getattr(state, "cache", None)
    if cache is not None:
        try:
            cache.get("__readyz__")  # 轻量连通性探测：Redis 不可用会抛异常
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=503,
                content={"status": "cache_unavailable", "ready": False, "detail": str(exc)},
            )
    return JSONResponse(status_code=200, content={"status": "ready", "ready": True})
