"""
📦 模块名称：告警接入 API 路由
📍 架构位置：HTTP 路由层，接收 Prometheus Alertmanager 的 webhook 推送。
🎯 核心作用：把外部告警标准化并写入审计链，作为自愈/降噪工作流入口。
🔗 依赖关系：依赖 AthenaWebService.ingest_alert 与 AlertWebhookParser；被 server.py 挂载。
💡 设计思路：机器对机器入口，不强制携带用户 JWT；生产可在网关加共享密钥保护。
📚 学习重点：理解监控告警如何闭环回到 Agent 触发处置。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from athena.api.routes._deps import get_service
from athena.api.services import AthenaWebService

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.post("/webhook")
async def receive_alert(
    request: Request, service: AthenaWebService = Depends(get_service)
) -> dict[str, object]:
    """
    接收 Alertmanager webhook。

    功能说明：解析告警 payload，标准化并写入审计链，返回受理结果。
    参数说明：request 携带 Alertmanager JSON body；service 是注入的服务层。
    返回值：{"status","alert_name","severity"}。
    设计思路：路由只做边界解析，处置逻辑放在服务层，方便未来接自愈工作流。
    使用示例：POST /api/alerts/webhook {"alerts":[{"labels":{"alertname":"X"}}]}
    """
    payload = await request.json()
    return service.ingest_alert(payload)
