"""
📦 模块名称：告警接入 API 路由
📍 架构位置：HTTP 路由层，接收 Prometheus Alertmanager 的 webhook 推送。
🎯 核心作用：把外部告警标准化并写入审计链，作为自愈/降噪工作流入口。
🔗 依赖关系：依赖 AthenaWebService.ingest_alert 与 AlertWebhookParser；被 server.py 挂载。
💡 设计思路：机器对机器入口，不走用户 JWT/租户；用共享密钥（header）做机器边界鉴权。
📚 学习重点：理解监控告警如何闭环回到 Agent 触发处置，以及机器入口的密钥校验。
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Query, Request

from athena.api.routes._deps import get_service
from athena.api.services import ApiServiceError, AthenaWebService

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def require_webhook_secret(request: Request) -> None:
    """
    校验 Alertmanager webhook 共享密钥。

    功能说明：从 ops.security.webhook_secret 读取密钥；为空则跳过校验（兼容演示/CI），
        否则请求必须携带 webhook_secret_header 且用 hmac.compare_digest 常量时间比对通过。
    参数说明：request 是当前 FastAPI 请求（从 app.state.settings 读配置与 header）。
    返回值：None（通过校验）；不通过抛 401。
    设计思路：webhook 是机器对机器入口，无租户身份，因此不复用 require_scope，
        而用独立的共享密钥边界；比对用 compare_digest 防时序攻击。

    🎯 面试考点：为什么不复用 require_scope？答案：Alertmanager 不携带 X-API-Key/JWT，
        没有租户上下文，机器入口用共享密钥更贴合，也避免污染租户授权模型。
    """
    settings = getattr(request.app.state, "settings", None)
    security = getattr(getattr(settings, "ops", None), "security", None)
    secret = getattr(security, "webhook_secret", None)
    if not secret:
        return  # 未配置密钥：保持现状不强制（本地演示/CI 兼容）
    header_name = getattr(security, "webhook_secret_header", "X-Alert-Secret")
    provided = request.headers.get(header_name, "")
    if not provided or not hmac.compare_digest(provided, secret):
        raise ApiServiceError(
            "UNAUTHORIZED", "invalid or missing webhook secret", status_code=401
        )


@router.post("/webhook")
async def receive_alert(
    request: Request,
    service: AthenaWebService = Depends(get_service),
    _: None = Depends(require_webhook_secret),
) -> dict[str, object]:
    """
    接收 Alertmanager webhook。

    功能说明：校验共享密钥后解析告警 payload，标准化并写入审计链，返回受理结果。
    参数说明：request 携带 Alertmanager JSON body；service 是注入的服务层。
    返回值：{"status","alert_name","severity"}。
    设计思路：路由只做边界鉴权与解析，处置逻辑放在服务层，方便未来接自愈工作流。
    使用示例：POST /api/alerts/webhook {"alerts":[{"labels":{"alertname":"X"}}]}
    """
    payload = await request.json()
    return await service.ingest_alert(payload)


@router.get("/history")
async def alert_history(
    limit: int = Query(default=20, ge=1, le=50),
    service: AthenaWebService = Depends(get_service),
) -> dict[str, object]:
    """返回最近的 Alertmanager 告警处理记录。"""
    return {"items": service.list_alert_history(limit)}
