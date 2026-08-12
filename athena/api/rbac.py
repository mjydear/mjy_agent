"""
📦 RBAC 授权依赖
📍 架构位置：接口安全层，位于鉴权（auth.resolve_tenant）之后，路由处理之前。
🎯 核心作用：提供 require_scope(scope) 依赖工厂，校验当前租户是否拥有指定 scope。
🔗 依赖：auth.TenantContext / require_tenant；被需要授权的写接口依赖。
💡 设计思路：鉴权解决“你是谁”，授权解决“你能做什么”，两者分层，scope 细粒度可扩展。
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends

from athena.api.auth import TenantContext, require_tenant
from athena.api.services import ApiServiceError


def require_scope(scope: str) -> Callable[[TenantContext], TenantContext]:
    """
    构造一个校验指定 scope 的 FastAPI 依赖。

    功能说明：租户缺少该 scope（且非 "*" 超级权限）时抛 403。
    参数说明：scope 形如 "workflow:run"、"cloud:execute"、"audit:read"。
    返回值：可用于 Depends(...) 的依赖函数。
    使用示例：tenant: TenantContext = Depends(require_scope("workflow:run"))
    """

    def _dependency(
        tenant: TenantContext = Depends(require_tenant),
    ) -> TenantContext:
        if not tenant.has_scope(scope):
            raise ApiServiceError(
                "FORBIDDEN",
                f"missing required scope: {scope}",
                status_code=403,
            )
        return tenant

    return _dependency
