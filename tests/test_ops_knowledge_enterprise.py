"""运维知识库测试：持久化恢复、语义召回、无向量降级关键词。"""

from __future__ import annotations

import asyncio

from athena.infra.cache import InMemoryCache
from athena.infra.vector_db import InMemoryVectorStore
from athena.memory.long_term import HashEmbeddingProvider
from athena.memory.ops_knowledge import OpsKnowledgeBase


def test_persist_and_restore_across_restart() -> None:
    cache = InMemoryCache()
    kb = OpsKnowledgeBase(cache=cache)
    kid = kb.record_case("CrashLoop", "env missing", "rollback", True)

    # 新实例共享同一 cache => 模拟重启
    restarted = OpsKnowledgeBase(cache=cache)
    assert kid in restarted.items
    assert restarted.items[kid].title == "CrashLoop"


def test_keyword_search_still_works() -> None:
    kb = OpsKnowledgeBase(cache=InMemoryCache())
    kb.record_case("DiskFull", "log grew", "clean logs", True)
    assert kb.search("disk")[0].title == "DiskFull"


def test_semantic_search_returns_relevant_case() -> None:
    kb = OpsKnowledgeBase(
        cache=InMemoryCache(),
        vector_store=InMemoryVectorStore(),
        embedding_provider=HashEmbeddingProvider(dimension=64),
    )
    kb.record_case("PodCrashLoopBackOff", "container exits", "fix image", True)
    kb.record_case("HighLatency", "slow db query", "add index", True)

    hits = asyncio.run(kb.semantic_search("container exits", top_k=2))
    assert any("CrashLoop" in h.title for h in hits)


def test_semantic_search_degrades_to_keyword_without_vector() -> None:
    kb = OpsKnowledgeBase(cache=InMemoryCache())
    kb.record_case("OOMKilled", "memory limit", "raise limit", True)
    hits = asyncio.run(kb.semantic_search("memory", top_k=5))
    assert hits and hits[0].title == "OOMKilled"
