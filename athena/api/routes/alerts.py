"""
📦 模块名称：告警接入 API 路由
📍 架构位置：HTTP 路由层，接收 Prometheus Alertmanager 的 webhook 推送。
🎯 核心作用：把外部告警标准化并写入审计链，作为自愈/降噪工作流入口。
🔗 依赖关系：依赖 AthenaWebService.ingest_alert 与 AlertWebhookParser；被 server.py 挂载。
💡 设计思路：机器对机器入口，不强制携带用户 JWT；生产可在网关加共享密钥保护。
📚 学习重点：理解监控告警如何闭环回到 Agent 触发处置。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from athena.api.auth import TenantContext, require_tenant, resolve_tenant
from athena.api.routes._deps import get_service
from athena.api.services import ApiServiceError, AthenaWebService
from athena.api.tenant_alert_history import TenantAlertHistory
from athena.observability.trace_context import get_traceparent

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _webhook_tenant(request: Request) -> TenantContext:
    """Use an authenticated caller when present, otherwise the integration default."""
    settings = request.app.state.settings
    integration_token = request.headers.get("X-Alert-Token")
    if integration_token:
        tenant_id = settings.security.alert_integration_tokens.get(integration_token)
        if tenant_id is None:
            raise ApiServiceError(
                "ALERT_INTEGRATION_UNAUTHORIZED",
                "invalid alert integration token",
                status_code=401,
            )
        return TenantContext(
            tenant_id=tenant_id,
            api_key=None,
            roles=("alerts:ingest",),
        )
    if request.headers.get("X-API-Key") or request.headers.get("Authorization"):
        return resolve_tenant(request)
    if settings.runtime.profile == "production" or settings.security.require_auth:
        raise ApiServiceError(
            "ALERT_INTEGRATION_UNAUTHORIZED",
            "missing alert integration credential",
            status_code=401,
        )
    return TenantContext(
        tenant_id=settings.security.default_tenant,
        api_key=None,
        roles=("alerts:ingest",),
    )


def _record_alert_history(
    request: Request, tenant_id: str, response: dict[str, object]
) -> None:
    history = getattr(request.app.state, "tenant_alert_history", None)
    if isinstance(history, TenantAlertHistory):
        history.record_response(tenant_id, response)


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
    try:
        payload = await request.json()
    except ValueError as exc:
        raise ApiServiceError(
            "ALERT_PAYLOAD_INVALID", "request body must be valid JSON"
        ) from exc
    tenant = _webhook_tenant(request)
    if not tenant.has_scope("alerts:ingest"):
        raise ApiServiceError(
            "ALERT_INGEST_FORBIDDEN",
            "alert integration principal is missing alerts:ingest",
            status_code=403,
        )
    durable_service = getattr(request.app.state, "durable_alert_service", None)
    if durable_service is not None:
        integration_id = request.headers.get("X-Alert-Integration", "alertmanager")
        try:
            result = await durable_service.ingest(
                payload,
                tenant_id=tenant.tenant_id,
                integration_id=integration_id,
                traceparent=get_traceparent(),
            )
        except ValueError as exc:
            raise ApiServiceError("ALERT_PAYLOAD_INVALID", str(exc)) from exc
        _record_alert_history(request, tenant.tenant_id, result)
        return JSONResponse(status_code=202, content=result)
    result = service.ingest_alert(payload)
    _record_alert_history(request, tenant.tenant_id, result)
    return result


@router.get("/history")
async def alert_history(
    request: Request,
    limit: int = Query(default=20, ge=1, le=50),
    tenant: TenantContext = Depends(require_tenant),
) -> dict[str, object]:
    """返回最近的 Alertmanager 告警处理记录。"""
    history = getattr(request.app.state, "tenant_alert_history", None)
    if not isinstance(history, TenantAlertHistory):
        return {"items": []}
    return {"items": history.list(tenant.tenant_id, limit=limit)}
