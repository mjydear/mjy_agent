"""
📦 接口鉴权与多租户上下文
📍 架构位置：接口服务层安全边界，作为 FastAPI 依赖注入到需要保护的路由。
🎯 核心作用：校验 X-API-Key，解析出租户身份；未配置任何 Key 时鉴权关闭（本地演示友好）。
🔗 依赖：config.SecuritySettings（从 app.state.settings 读取）；被写接口路由依赖。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request

from athena.api.services import ApiServiceError

API_KEY_HEADER = "X-API-Key"


@dataclass(frozen=True)
class TenantContext:
    """一次请求归属的租户身份。"""

    tenant_id: str
    api_key: str | None


def resolve_tenant(request: Request) -> TenantContext:
    """
    从请求头解析租户身份。

    规则：
      - 未配置 api_keys 且 require_auth=False → 鉴权关闭，归入默认租户。
      - 已开启鉴权 → 必须携带合法 X-API-Key，否则 401。
    """
    settings = getattr(request.app.state, "settings", None)
    security = getattr(settings, "security", None) if settings else None
    if security is None or (not security.require_auth and not security.api_keys):
        tenant = getattr(security, "default_tenant", "public")
        return TenantContext(tenant_id=tenant, api_key=None)

    api_key = request.headers.get(API_KEY_HEADER)
    if not api_key:
        raise ApiServiceError(
            "UNAUTHORIZED", "missing API key", status_code=401
        )
    tenant = security.api_keys.get(api_key)
    if tenant is None:
        raise ApiServiceError(
            "UNAUTHORIZED", "invalid API key", status_code=401
        )
    return TenantContext(tenant_id=tenant, api_key=api_key)


def require_tenant(tenant: TenantContext = Depends(resolve_tenant)) -> TenantContext:
    """路由依赖别名：`tenant: TenantContext = Depends(require_tenant)`。"""
    return tenant
