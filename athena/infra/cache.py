"""
📦 缓存后端抽象层：InMemory（可降级）+ Redis（生产）
📍 架构位置：基础设施层，被幂等中间件、向量检索缓存、限流器共享。
🎯 核心作用：提供统一的 KV 缓存接口，Redis 不可用时自动降级到进程内内存缓存，保证服务始终可启动。
🔗 依赖：可选 redis 客户端；被 api.idempotency / infra.vector_db / 限流器使用。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CacheBackend(Protocol):
    """缓存后端协议：所有实现暴露相同的读写与统计接口。"""

    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None: ...
    def delete(self, key: str) -> None: ...
    def incr(self, key: str, ttl_seconds: int | None = None) -> int: ...
    def stats(self) -> dict[str, int]: ...
    def close(self) -> None: ...


class _StatsMixin:
    """记录命中/未命中，用于计算缓存命中率指标。"""

    def __init__(self) -> None:
        self._hits = 0
        self._misses = 0

    def _hit(self) -> None:
        self._hits += 1

    def _miss(self) -> None:
        self._misses += 1

    def stats(self) -> dict[str, int]:
        total = self._hits + self._misses
        rate = int(self._hits / total * 100) if total else 0
        return {"hits": self._hits, "misses": self._misses, "hit_rate_pct": rate}


class InMemoryCache(_StatsMixin):
    """线程安全的进程内缓存，带 TTL 过期。Redis 缺失时的兜底实现。"""

    def __init__(self, namespace: str = "athena") -> None:
        super().__init__()
        self._namespace = namespace
        self._store: dict[str, tuple[str, float | None]] = {}
        self._lock = threading.RLock()

    def _k(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    def _expired(self, expires_at: float | None) -> bool:
        return expires_at is not None and time.time() >= expires_at

    def get(self, key: str) -> str | None:
        k = self._k(key)
        with self._lock:
            entry = self._store.get(k)
            if entry is None or self._expired(entry[1]):
                if entry is not None:
                    self._store.pop(k, None)
                self._miss()
                return None
            self._hit()
            return entry[0]

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        expires_at = time.time() + ttl_seconds if ttl_seconds else None
        with self._lock:
            self._store[self._k(key)] = (value, expires_at)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(self._k(key), None)

    def incr(self, key: str, ttl_seconds: int | None = None) -> int:
        k = self._k(key)
        with self._lock:
            entry = self._store.get(k)
            current = 0 if entry is None or self._expired(entry[1]) else int(entry[0])
            current += 1
            expires_at = entry[1] if entry and not self._expired(entry[1]) else None
            if expires_at is None and ttl_seconds:
                expires_at = time.time() + ttl_seconds
            self._store[k] = (str(current), expires_at)
            return current

    def close(self) -> None:
        with self._lock:
            self._store.clear()


class RedisCache(_StatsMixin):
    """基于 redis-py 的缓存后端，命中率统计在本进程侧累计。"""

    def __init__(self, client: Any, namespace: str = "athena") -> None:
        super().__init__()
        self._client = client
        self._namespace = namespace

    def _k(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    def get(self, key: str) -> str | None:
        value = self._client.get(self._k(key))
        if value is None:
            self._miss()
            return None
        self._hit()
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        if ttl_seconds:
            self._client.set(self._k(key), value, ex=ttl_seconds)
        else:
            self._client.set(self._k(key), value)

    def delete(self, key: str) -> None:
        self._client.delete(self._k(key))

    def incr(self, key: str, ttl_seconds: int | None = None) -> int:
        k = self._k(key)
        value = int(self._client.incr(k))
        if value == 1 and ttl_seconds:
            self._client.expire(k, ttl_seconds)
        return value

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


def create_cache(redis_url: str | None = None, namespace: str = "athena") -> CacheBackend:
    """
    创建缓存后端：优先连接 Redis，连不上则降级到内存缓存。

    这样保证无论部署环境是否提供 Redis，服务都能启动（高可用的第一步：不因外部依赖缺失而崩溃）。
    """
    if redis_url:
        try:
            import redis  # 延迟导入，未安装 redis 时不影响其它功能

            client = redis.Redis.from_url(redis_url, socket_connect_timeout=1)
            client.ping()
            return RedisCache(client, namespace=namespace)
        except Exception:
            # 连接失败：降级为内存缓存，记录降级事实由调用方决定是否告警
            return InMemoryCache(namespace=namespace)
    return InMemoryCache(namespace=namespace)


# JSON 便捷读写：业务层多数缓存的是结构化对象
def cache_get_json(cache: CacheBackend, key: str) -> Any | None:
    raw = cache.get(key)
    return json.loads(raw) if raw is not None else None


def cache_set_json(
    cache: CacheBackend, key: str, value: Any, ttl_seconds: int | None = None
) -> None:
    cache.set(key, json.dumps(value, ensure_ascii=False), ttl_seconds=ttl_seconds)
