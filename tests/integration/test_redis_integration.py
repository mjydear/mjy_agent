"""集成测试：针对真实 Redis 的存储往返（缺 Redis 自动跳过）。

运行：本机/CI 有 Redis 时执行；否则本文件全部跳过。
    pytest -m integration
CI 可用 testcontainers 或 docker-compose 起 Redis 后运行。
"""

from __future__ import annotations

import pytest

from athena.infra.cache import RedisCache, create_cache

pytestmark = pytest.mark.integration

_REDIS_URL = "redis://127.0.0.1:6379/0"


def _real_cache() -> RedisCache:
    cache = create_cache(_REDIS_URL, namespace="athena-itest")
    if not isinstance(cache, RedisCache):
        pytest.skip("Redis 不可用，跳过集成测试")
    return cache


def test_redis_set_get_incr_roundtrip() -> None:
    cache = _real_cache()
    key = "itest:kv"
    cache.delete(key)
    cache.set(key, "hello")
    assert cache.get(key) == "hello"
    counter = "itest:counter"
    cache.delete(counter)
    assert cache.incr(counter) == 1
    assert cache.incr(counter) == 2
    cache.delete(key)
    cache.delete(counter)


def test_redis_session_store_roundtrip() -> None:
    cache = _real_cache()
    from athena.api.session_store import SessionStore

    store = SessionStore(cache, ttl_seconds=60)
    stored = store.create("itest-session", "集成会话")
    fetched = store.get("itest-session")
    assert fetched is not None
    assert fetched.session_id == stored.session_id
    store.delete("itest-session")


def test_redis_audit_chain_verifies_on_real_backend() -> None:
    cache = _real_cache()
    from athena.tools.audit_chain import HashChainAuditStore

    # 清理可能的历史键，保证链从干净状态开始
    for key in ("audit:seq", "audit:head", "audit:index"):
        cache.delete(key)
    store = HashChainAuditStore(cache)
    store.append(actor="itest", action="x.run", resource="r1", success=True)
    store.append(actor="itest", action="y.run", resource="r2", success=True)
    result = store.verify_chain()
    assert result["valid"] is True
    assert result["checked"] >= 2
