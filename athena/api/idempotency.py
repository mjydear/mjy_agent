"""
📦 幂等性管理器
📍 架构位置：接口服务层，写接口（任务提交等）的重复请求防护。
🎯 核心作用：基于请求携带的 Idempotency-Key，把首次执行结果缓存；重复请求直接返回缓存结果，
             避免重复提交导致的数据错乱（如重复创建任务、重复执行工具）。
🔗 依赖：infra.cache.CacheBackend；被 tasks 路由使用。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from typing import Any

from fastapi import Request

from athena.infra.cache import CacheBackend, cache_get_json, cache_set_json

IDEMPOTENCY_HEADER = "Idempotency-Key"


class IdempotencyConflictError(RuntimeError):
    """A key was reused for a different request payload."""


class IdempotencyManager:
    """把 (租户, Idempotency-Key) → 首次响应结果 缓存起来。"""

    def __init__(self, cache: CacheBackend, ttl_seconds: int = 86400) -> None:
        self._cache = cache
        self._ttl = ttl_seconds
        self._lock = threading.RLock()

    def _key(self, tenant_id: str, idem_key: str, operation: str) -> str:
        operation_hash = hashlib.sha256(operation.encode("utf-8")).hexdigest()[:16]
        return f"idem:{tenant_id}:{operation_hash}:{idem_key}"

    def lookup(
        self,
        tenant_id: str,
        idem_key: str | None,
        *,
        operation: str = "default",
        request_hash: str | None = None,
    ) -> Any | None:
        """返回该幂等键已缓存的结果，不存在返回 None。"""
        if not idem_key:
            return None
        cached = cache_get_json(
            self._cache, self._key(tenant_id, idem_key, operation)
        )
        if cached is None:
            return None
        if isinstance(cached, dict) and cached.get("_idempotency_record") is True:
            cached_hash = cached.get("request_hash")
            if request_hash is not None and cached_hash != request_hash:
                raise IdempotencyConflictError(
                    "idempotency key was reused with a different request"
                )
            return cached.get("result")
        return cached

    def remember(
        self,
        tenant_id: str,
        idem_key: str | None,
        result: Any,
        *,
        operation: str = "default",
        request_hash: str | None = None,
    ) -> None:
        """记录该幂等键的执行结果，供后续重复请求复用。"""
        if not idem_key:
            return
        cache_set_json(
            self._cache,
            self._key(tenant_id, idem_key, operation),
            {
                "_idempotency_record": True,
                "request_hash": request_hash,
                "result": result,
            },
            ttl_seconds=self._ttl,
        )

    def execute_once(
        self,
        tenant_id: str,
        idem_key: str,
        *,
        operation: str,
        request_hash: str,
        factory: Callable[[], Any],
    ) -> tuple[Any, bool]:
        """Atomically replay or execute a process-local command once."""
        with self._lock:
            cached = self.lookup(
                tenant_id,
                idem_key,
                operation=operation,
                request_hash=request_hash,
            )
            if cached is not None:
                return cached, True
            result = factory()
            self.remember(
                tenant_id,
                idem_key,
                result,
                operation=operation,
                request_hash=request_hash,
            )
            return result, False

    @staticmethod
    def request_hash(payload: object) -> str:
        serialized = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_idempotency_key(request: Request, *, required: bool = False) -> str | None:
    """从请求头提取 Idempotency-Key（可选）。"""
    value = request.headers.get(IDEMPOTENCY_HEADER)
    if value is not None:
        value = value.strip()
    if required and not value:
        raise ValueError("IDEMPOTENCY_KEY_REQUIRED")
    return value or None
