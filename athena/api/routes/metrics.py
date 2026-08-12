"""
📦 模块名称：运行指标 API 路由
📍 架构位置：HTTP 路由层，连接 Web 控制台指标面板和 RuntimeMetrics。
🎯 核心作用：提供 GET /api/metrics，让前端展示任务数、成功率、平均耗时等指标。
🔗 依赖关系：依赖 MetricsResponse 和 AthenaWebService；被 server.py 挂载。
💡 设计思路：指标查询是只读接口，路由不修改业务状态，只读取服务层聚合结果。
📚 学习重点：看可观测性数据如何从执行过程沉淀到前端仪表盘。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from athena.api.routes._deps import get_service
from athena.api.schemas import MetricsResponse
from athena.api.services import AthenaWebService

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("", response_model=MetricsResponse)
async def get_metrics(
    service: AthenaWebService = Depends(get_service),
) -> MetricsResponse:
    """
    获取全局运行指标。

    功能说明：返回 Web Console 指标 Tab 需要的统计数据。
    参数说明：service 是注入的服务层对象。
    返回值：MetricsResponse。
    设计思路：把指标计算放在 service.get_metrics()，路由保持只读转发。
    使用示例：GET /api/metrics
    """
    return service.get_metrics()


"""
🤔 思考题：

1. 如果指标很多，是否应该增加时间窗口，例如最近 5 分钟？
2. 当前接口返回全局指标，如果要按 session 统计，需要新增什么参数？
3. 为什么指标接口最好保持只读？
"""
