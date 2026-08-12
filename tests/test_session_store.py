"""会话持久化测试：验证落库、跨"重启"重建、TTL 索引清理。"""

from __future__ import annotations

import asyncio

import pytest

from athena.api.auth import TenantContext
from athena.api.services import AthenaWebService
from athena.api.session_store import SessionStore, StoredMessage, StoredSession
from athena.infra.cache import InMemoryCache


class _EchoAgent:
    """最小 Agent 桩：run 直接回显，隔离对 LLM 的依赖。"""

    async def run(self, message: str):
        from athena.agent.base import AgentResponse  # 局部导入避免循环

        return AgentResponse(answer=f"echo:{message}", steps=["step"])


def _make_service(cache: InMemoryCache) -> AthenaWebService:
    return AthenaWebService(
        agent_factory=lambda: _EchoAgent(),
        session_store=SessionStore(cache, ttl_seconds=3600),
    )


def test_store_roundtrip() -> None:
    store = SessionStore(InMemoryCache(), ttl_seconds=3600)
    store.create("s1", "Demo")
    store.save(
        StoredSession(
            session_id="s1",
            title="Demo",
            messages=[StoredMessage(role="user", content="hi")],
        )
    )
    got = store.get("s1")
    assert got is not None
    assert got.messages[0].content == "hi"
    assert [s.session_id for s in store.list()] == ["s1"]
    store.delete("s1")
    assert store.get("s1") is None
    assert store.list() == []


def test_chat_persists_and_survives_restart() -> None:
    cache = InMemoryCache()  # 模拟共享的 Redis
    service = _make_service(cache)
    detail = service.create_session("Chat")
    sid = detail.session_id
    asyncio.run(service.chat(sid, "hello"))

    # 新建一个服务实例，共享同一后端 => 模拟进程重启/另一副本
    restarted = _make_service(cache)
    reloaded = restarted.get_session(sid)
    contents = [m.content for m in reloaded.messages]
    assert "hello" in contents
    assert any(c.startswith("echo:") for c in contents)


def test_ttl_index_pruned_on_list() -> None:
    cache = InMemoryCache()
    store = SessionStore(cache, ttl_seconds=3600)
    store.create("a", "A")
    store.create("b", "B")
    cache.delete("session:a")  # 模拟 a 已过期
    remaining = [s.session_id for s in store.list()]
    assert remaining == ["b"]


def test_store_scopes_identical_session_ids_by_tenant() -> None:
    """Tenant-specific keys and indexes must not collide on the same id."""
    store = SessionStore(InMemoryCache(), ttl_seconds=3600)
    tenant_a = TenantContext(tenant_id="tenant-a", api_key="key-a")
    tenant_b = TenantContext(tenant_id="tenant-b", api_key="key-b")

    store.create("shared", "A session", tenant=tenant_a)
    store.create("shared", "B session", tenant=tenant_b)

    assert store.get("shared", tenant=tenant_a).title == "A session"  # type: ignore[union-attr]
    assert store.get("shared", tenant=tenant_b).title == "B session"  # type: ignore[union-attr]
    assert [item.title for item in store.list(tenant=tenant_a)] == ["A session"]
    assert [item.title for item in store.list(tenant=tenant_b)] == ["B session"]

    store.delete("shared", tenant=tenant_a)
    assert store.get("shared", tenant=tenant_a) is None
    assert store.get("shared", tenant=tenant_b).title == "B session"  # type: ignore[union-attr]
