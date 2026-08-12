"""
📦 向量检索缓存层
📍 架构位置：基础设施层，包裹任意 VectorStore，为热点查询加一层结果缓存。
🎯 核心作用：把"查询向量→检索结果"缓存到 Redis（可降级内存），命中即跳过向量计算，
             显著降低热点问题的响应耗时；同时缓存 文本→嵌入 结果，减少重复嵌入调用。
🔗 依赖：infra.cache.CacheBackend、infra.vector_db.VectorStore/MemoryDocument。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from athena.infra.cache import CacheBackend
from athena.infra.vector_db import MemoryDocument, VectorStore


def _embedding_key(embedding: Sequence[float], top_k: int) -> str:
    """把查询向量四舍五入后哈希成稳定缓存键（抵抗浮点微小噪声）。"""
    rounded = ",".join(f"{x:.5f}" for x in embedding)
    digest = hashlib.sha256(rounded.encode("utf-8")).hexdigest()[:24]
    return f"vec:search:{digest}:{top_k}"


class CachedVectorStore:
    """给任意 VectorStore 套上检索结果缓存；接口与 VectorStore 完全一致，可无感替换。"""

    def __init__(
        self, inner: VectorStore, cache: CacheBackend, ttl_seconds: int = 300
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._ttl = ttl_seconds

    async def add(self, document: MemoryDocument) -> None:
        # 写入会让旧缓存变脏；MVP 采用 TTL 自然过期（热点读多写少，命中率高）
        await self._inner.add(document)

    async def search(
        self, embedding: Sequence[float], top_k: int
    ) -> Sequence[MemoryDocument]:
        key = _embedding_key(embedding, top_k)
        cached = self._cache.get(key)
        if cached is not None:
            return [MemoryDocument(**item) for item in json.loads(cached)]
        results = await self._inner.search(embedding, top_k)
        payload = json.dumps(
            [doc.model_dump() for doc in results], ensure_ascii=False
        )
        self._cache.set(key, payload, ttl_seconds=self._ttl)
        return results

    def cache_stats(self) -> dict[str, int]:
        return self._cache.stats()


class EmbeddingCache:
    """文本→嵌入向量 缓存，避免对相同文本重复调用嵌入模型。"""

    def __init__(self, cache: CacheBackend, ttl_seconds: int = 3600) -> None:
        self._cache = cache
        self._ttl = ttl_seconds

    def _key(self, text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        return f"vec:embed:{digest}"

    def get(self, text: str) -> list[float] | None:
        raw = self._cache.get(self._key(text))
        return json.loads(raw) if raw is not None else None

    def set(self, text: str, embedding: Sequence[float]) -> None:
        self._cache.set(
            self._key(text),
            json.dumps(list(embedding)),
            ttl_seconds=self._ttl,
        )
