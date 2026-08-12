"""阶段2 检索优化测试：结果缓存命中、嵌入缓存、两阶段检索正确性。"""

from __future__ import annotations

import pytest

from athena.infra.cache import InMemoryCache
from athena.infra.retrieval import TwoStageRetriever
from athena.infra.vector_cache import CachedVectorStore, EmbeddingCache
from athena.infra.vector_db import InMemoryVectorStore, MemoryDocument


def _doc(doc_id: str, emb: list[float]) -> MemoryDocument:
    return MemoryDocument(doc_id=doc_id, content=f"c-{doc_id}", embedding=emb)


@pytest.mark.asyncio
async def test_cached_vector_store_hits_on_repeat_query() -> None:
    inner = InMemoryVectorStore()
    await inner.add(_doc("a", [1.0, 0.0]))
    await inner.add(_doc("b", [0.0, 1.0]))
    cache = InMemoryCache()
    store = CachedVectorStore(inner, cache, ttl_seconds=60)

    first = await store.search([1.0, 0.0], top_k=1)
    second = await store.search([1.0, 0.0], top_k=1)  # 命中缓存
    assert first[0].doc_id == second[0].doc_id == "a"
    stats = store.cache_stats()
    assert stats["hits"] >= 1


@pytest.mark.asyncio
async def test_cached_store_returns_same_as_inner() -> None:
    inner = InMemoryVectorStore()
    await inner.add(_doc("a", [1.0, 0.0]))
    await inner.add(_doc("b", [0.9, 0.1]))
    cache = InMemoryCache()
    store = CachedVectorStore(inner, cache)
    direct = await inner.search([1.0, 0.0], top_k=2)
    cached = await store.search([1.0, 0.0], top_k=2)
    assert [d.doc_id for d in direct] == [d.doc_id for d in cached]


def test_embedding_cache_roundtrip() -> None:
    cache = InMemoryCache()
    ec = EmbeddingCache(cache)
    assert ec.get("hello") is None
    ec.set("hello", [0.1, 0.2, 0.3])
    assert ec.get("hello") == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_two_stage_retriever_matches_exact_topk() -> None:
    inner = InMemoryVectorStore()
    for i in range(100):
        await inner.add(_doc(str(i), [i / 100.0, 1 - i / 100.0]))
    retriever = TwoStageRetriever(inner, coarse_multiplier=5, min_coarse=20)
    query = [0.99, 0.01]
    result = await retriever.search(query, top_k=3)
    exact = await inner.search(query, top_k=3)
    assert [d.doc_id for d in result] == [d.doc_id for d in exact]
