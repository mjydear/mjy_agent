"""
📦 模块名称：审计查询 API 路由
📍 架构位置：HTTP 路由层，暴露审计哈希链的只读查询与完整性校验。
🎯 核心作用：让审计员/合规系统查看关键操作留痕并验证链条未被篡改。
🔗 依赖关系：依赖 AthenaWebService 的审计能力与 RBAC scope；被 server.py 挂载。
💡 设计思路：审计接口只读且需 audit:read 权限，避免越权查看敏感操作记录。
📚 学习重点：理解“防篡改审计链”如何通过 verify 端点对外证明完整性。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from athena.api.auth import TenantContext
from athena.api.rbac import require_scope
from athena.api.routes._deps import get_service
from athena.api.services import AthenaWebService

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/events")
async def list_audit_events(
    limit: int = Query(default=50, ge=1, le=500),
    tenant_id: str | None = Query(default=None),
    service: AthenaWebService = Depends(get_service),
    tenant: TenantContext = Depends(require_scope("audit:read")),
) -> dict[str, object]:
    """
    查询最近的审计记录。

    功能说明：按时间倒序返回审计哈希链记录，可用 tenant_id 过滤。
    参数说明：limit 限制条数；tenant_id 可选过滤；tenant 需带 audit:read scope。
    返回值：包含 events 列表的字典。
    设计思路：只读接口，权限收口在 audit:read，避免普通用户看到全量操作留痕。
    使用示例：GET /api/audit/events?limit=20
    """
    events = service.list_audit_events(limit=limit, tenant_id=tenant_id)
    return {"events": events, "count": len(events)}


@router.get("/verify")
async def verify_audit_chain(
    service: AthenaWebService = Depends(get_service),
    tenant: TenantContext = Depends(require_scope("audit:read")),
) -> dict[str, object]:
    """
    校验审计哈希链完整性。

    功能说明：从创世块重算每条记录哈希，检测是否被篡改。
    参数说明：tenant 需带 audit:read scope。
    返回值：{"valid","checked","broken_at","head"}。
    设计思路：把“防篡改”能力做成可验证端点，合规系统可定期巡检。
    使用示例：GET /api/audit/verify
    """
    return service.verify_audit_chain()
