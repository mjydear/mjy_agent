"""Prometheus 指标导出路由：GET /api/metrics/prometheus 供 Prometheus 抓取。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("/prometheus", include_in_schema=True)
async def prometheus_metrics(request: Request) -> Response:
    """导出 Prometheus 文本格式指标（含缓存命中率的即时快照）。"""
    metrics = request.app.state.prometheus
    cache = getattr(request.app.state, "cache", None)
    # 抓取时刷新缓存命中率，反映最新状态
    if cache is not None and hasattr(cache, "stats"):
        ratio = cache.stats().get("hit_rate_pct", 0) / 100.0
        metrics.set_cache_hit_ratio(getattr(cache, "_namespace", "default"), ratio)
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)
