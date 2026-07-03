"""
📦 两阶段检索器（粗排 + 精排）
📍 架构位置：基础设施层，包裹 VectorStore，兼顾召回率与性能。
🎯 核心作用：先用近似索引快速召回较大候选集（粗排），再用精确余弦相似度重排取 top-K（精排）。
             近似索引负责"快"，精排负责"准"，组合后在性能与召回率之间取得平衡。
🔗 依赖：infra.vector_db 的 VectorStore/MemoryDocument/_cosine_similarity。
"""

from __future__ import annotations

from collections.abc import Sequence

from athena.infra.vector_db import MemoryDocument, VectorStore, _cosine_similarity


class TwoStageRetriever:
    """粗排（快速召回候选）+ 精排（精确重排）两阶段检索。"""

    def __init__(
        self,
        store: VectorStore,
        coarse_multiplier: int = 5,
        min_coarse: int = 50,
    ) -> None:
        if coarse_multiplier < 1:
            raise ValueError("coarse_multiplier must be >= 1")
        self._store = store
        self._coarse_multiplier = coarse_multiplier
        self._min_coarse = min_coarse

    async def search(
        self, embedding: Sequence[float], top_k: int
    ) -> Sequence[MemoryDocument]:
        # 粗排：召回 max(min_coarse, top_k * multiplier) 个候选，覆盖率更高
        coarse_k = max(self._min_coarse, top_k * self._coarse_multiplier)
        candidates = await self._store.search(embedding, coarse_k)
        # 精排：对候选集用精确余弦相似度重排，取最终 top_k
        reranked = sorted(
            candidates,
            key=lambda doc: _cosine_similarity(embedding, doc.embedding),
            reverse=True,
        )
        return reranked[:top_k]
