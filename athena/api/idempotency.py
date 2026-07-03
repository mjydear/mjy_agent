"""
📦 幂等性管理器
📍 架构位置：接口服务层，写接口（任务提交等）的重复请求防护。
🎯 核心作用：基于请求携带的 Idempotency-Key，把首次执行结果缓存；重复请求直接返回缓存结果，
             避免重复提交导致的数据错乱（如重复创建任务、重复执行工具）。
🔗 依赖：infra.cache.CacheBackend；被 tasks 路由使用。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from athena.infra.cache import CacheBackend, cache_get_json, cache_set_json

IDEMPOTENCY_HEADER = "Idempotency-Key"


class IdempotencyManager:
    """把 (租户, Idempotency-Key) → 首次响应结果 缓存起来。"""

    def __init__(self, cache: CacheBackend, ttl_seconds: int = 86400) -> None:
        self._cache = cache
        self._ttl = ttl_seconds

    def _key(self, tenant_id: str, idem_key: str) -> str:
        return f"idem:{tenant_id}:{idem_key}"

    def lookup(self, tenant_id: str, idem_key: str | None) -> Any | None:
        """返回该幂等键已缓存的结果，不存在返回 None。"""
        if not idem_key:
            return None
        return cache_get_json(self._cache, self._key(tenant_id, idem_key))

    def remember(self, tenant_id: str, idem_key: str | None, result: Any) -> None:
        """记录该幂等键的执行结果，供后续重复请求复用。"""
        if not idem_key:
            return
        cache_set_json(
            self._cache, self._key(tenant_id, idem_key), result, ttl_seconds=self._ttl
        )


def get_idempotency_key(request: Request) -> str | None:
    """从请求头提取 Idempotency-Key（可选）。"""
    return request.headers.get(IDEMPOTENCY_HEADER)
