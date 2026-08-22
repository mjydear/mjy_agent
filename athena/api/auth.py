"""
📦 接口鉴权与多租户上下文
📍 架构位置：接口服务层安全边界，作为 FastAPI 依赖注入到需要保护的路由。
🎯 核心作用：校验 X-API-Key 或 Bearer JWT，解析出租户身份与 RBAC scope；
             未配置任何凭证时鉴权关闭（本地演示友好）。
🔗 依赖：config.SecuritySettings（从 app.state.settings 读取）；被写接口路由依赖。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fastapi import Depends, Request

from athena.api.errors import ApiServiceError

API_KEY_HEADER = "X-API-Key"
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantContext:
    """一次请求归属的租户身份与授权 scope。"""

    tenant_id: str
    api_key: str | None
    roles: tuple[str, ...] = field(default_factory=tuple)

    def has_scope(self, scope: str) -> bool:
        """是否拥有某个 scope（"*" 表示超级权限，放行全部）。"""
        return "*" in self.roles or scope in self.roles


def _roles_for_tenant(security: object, tenant_id: str) -> tuple[str, ...]:
    """从配置读取租户 scope；未配置的租户默认全部权限（向后兼容）。"""
    roles_map = getattr(security, "roles", None) or {}
    if tenant_id in roles_map:
        return tuple(roles_map[tenant_id])
    return ("*",)


def _resolve_jwt(request: Request, security: object) -> "TenantContext | None":
    """尝试用 Bearer JWT 解析身份；未配置/未携带/PyJWT 缺失时返回 None。"""
    secret = getattr(security, "jwt_secret", None)
    if not secret:
        return None
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[len("Bearer ") :].strip()
    try:
        import jwt  # 惰性导入：未安装 PyJWT 时自动降级（不支持 Bearer）
    except ImportError:
        _logger.warning("PyJWT not installed, Bearer auth disabled")
        return None
    try:
        options = {"verify_aud": bool(getattr(security, "jwt_audience", None))}
        claims = jwt.decode(
            token,
            secret,
            algorithms=[getattr(security, "jwt_algorithm", "HS256")],
            audience=getattr(security, "jwt_audience", None),
            issuer=getattr(security, "jwt_issuer", None),
            options=options,
        )
    except Exception as exc:  # noqa: BLE001 — 任何验签/过期错误都视为未授权
        raise ApiServiceError("UNAUTHORIZED", f"invalid token: {exc}", status_code=401)
    tenant_id = claims.get("tenant") or claims.get("sub") or "public"
    raw_scopes = claims.get("scopes") or claims.get("roles") or []
    if isinstance(raw_scopes, str):
        raw_scopes = raw_scopes.split()
    return TenantContext(
        tenant_id=str(tenant_id), api_key=None, roles=tuple(raw_scopes)
    )


def resolve_tenant(request: Request) -> TenantContext:
    """
    从请求解析租户身份与 scope。

    规则（优先级：Bearer JWT > API Key）：
      - 未配置任何凭证且 require_auth=False → 鉴权关闭，默认租户获全部权限。
      - 配置 jwt_secret 且带 Bearer → 验签取 tenant/scopes。
      - 已开启鉴权 → 必须携带合法 X-API-Key，否则 401。
    """
    settings = getattr(request.app.state, "settings", None)
    security = getattr(settings, "security", None) if settings else None
    if security is None or (
        not security.require_auth
        and not security.api_keys
        and not getattr(security, "jwt_secret", None)
    ):
        tenant = getattr(security, "default_tenant", "public")
        return TenantContext(tenant_id=tenant, api_key=None, roles=("*",))

    jwt_ctx = _resolve_jwt(request, security)
    if jwt_ctx is not None:
        return jwt_ctx

    api_key = request.headers.get(API_KEY_HEADER)
    if not api_key:
        raise ApiServiceError("UNAUTHORIZED", "missing API key", status_code=401)
    tenant = security.api_keys.get(api_key)
    if tenant is None:
        raise ApiServiceError("UNAUTHORIZED", "invalid API key", status_code=401)
    return TenantContext(
        tenant_id=tenant, api_key=api_key, roles=_roles_for_tenant(security, tenant)
    )


def require_tenant(tenant: TenantContext = Depends(resolve_tenant)) -> TenantContext:
    """路由依赖别名：`tenant: TenantContext = Depends(require_tenant)`。"""
    return tenant
