"""
知识库管理后台：文档上传 → 自动分块 → 向量入库 → 语义召回。

面向运维/业务知识沉淀：支持按标签过滤、最小相似度阈值等召回规则，
底层复用 VectorStore（内存/Milvus）与 EmbeddingProvider（哈希/真实模型）。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from athena.infra.vector_db import MemoryDocument, VectorStore
from athena.memory.long_term import EmbeddingProvider, HashEmbeddingProvider


def chunk_text(text: str, chunk_size: int = 400, overlap: int = 50) -> list[str]:
    """
    按字符数滑动窗口分块，块间保留 overlap 重叠以避免语义割裂。

    对中文按字符切分足够实用；overlap 让跨块的句子仍能被召回。
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be in [0, chunk_size)")
    cleaned = text.strip()
    if not cleaned:
        return []
    step = chunk_size - overlap
    chunks: list[str] = []
    for start in range(0, len(cleaned), step):
        piece = cleaned[start : start + chunk_size]
        if piece.strip():
            chunks.append(piece)
        if start + chunk_size >= len(cleaned):
            break
    return chunks


@dataclass
class KnowledgeDocument:
    """一篇知识文档的元数据（正文以分块形式存入向量库）。"""

    doc_id: str
    title: str
    tags: tuple[str, ...]
    chunk_count: int
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class RecallRule:
    """召回规则：top_k、最小相似度阈值、标签过滤。"""

    top_k: int = 5
    min_score: float = 0.0
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecallResult:
    """召回结果：命中的分块内容、来源文档与相似度分数。"""

    doc_id: str
    title: str
    chunk: str
    score: float
    tags: tuple[str, ...]


class KnowledgeBaseManager:
    """
    知识库管理器：负责文档的增删查与语义召回。

    每篇文档被切成多个 chunk 存入向量库，chunk 的 metadata 记录来源 doc_id/title/tags，
    召回时按向量相似度粗排后再套用 RecallRule（阈值 + 标签）精筛。
    """

    def __init__(
        self,
        vector_store: VectorStore,
        embedding_provider: EmbeddingProvider | None = None,
        chunk_size: int = 400,
        overlap: int = 50,
    ) -> None:
        self._store = vector_store
        self._embedder = embedding_provider or HashEmbeddingProvider()
        self._chunk_size = chunk_size
        self._overlap = overlap
        self._docs: dict[str, KnowledgeDocument] = {}

    async def ingest(
        self, title: str, content: str, tags: Sequence[str] | None = None
    ) -> KnowledgeDocument:
        """上传并入库一篇文档：分块 → 逐块嵌入 → 写入向量库。"""
        if not title.strip():
            raise ValueError("title must be non-empty")
        chunks = chunk_text(content, self._chunk_size, self._overlap)
        if not chunks:
            raise ValueError("content produced no chunks")
        doc_id = f"kb-{uuid.uuid4().hex[:12]}"
        tag_tuple = tuple(tags or ())
        for idx, chunk in enumerate(chunks):
            embedding = await self._embedder.embed(chunk)
            await self._store.add(
                MemoryDocument(
                    doc_id=f"{doc_id}#{idx}",
                    content=chunk,
                    embedding=list(embedding),
                    metadata={
                        "kb_doc_id": doc_id,
                        "title": title,
                        "tags": ",".join(tag_tuple),
                    },
                )
            )
        doc = KnowledgeDocument(
            doc_id=doc_id, title=title, tags=tag_tuple, chunk_count=len(chunks)
        )
        self._docs[doc_id] = doc
        return doc

    def list_documents(self) -> list[KnowledgeDocument]:
        """列出所有已入库文档（按创建时间倒序）。"""
        return sorted(self._docs.values(), key=lambda d: d.created_at, reverse=True)

    def get_document(self, doc_id: str) -> KnowledgeDocument | None:
        return self._docs.get(doc_id)

    def delete_document(self, doc_id: str) -> bool:
        """删除文档元数据（向量分块由后端 TTL/重建清理）。返回是否存在。"""
        return self._docs.pop(doc_id, None) is not None

    async def recall(self, query: str, rule: RecallRule | None = None) -> list[RecallResult]:
        """
        语义召回：向量粗排 → 相似度阈值 + 标签过滤精筛。

        为保证阈值筛选后仍有足够结果，粗排阶段多取候选（top_k * 4）。
        """
        rule = rule or RecallRule()
        if not query.strip():
            raise ValueError("query must be non-empty")
        embedding = await self._embedder.embed(query)
        from athena.infra.vector_db import _cosine_similarity  # 复用余弦实现

        candidates = await self._store.search(embedding, top_k=max(rule.top_k * 4, rule.top_k))
        results: list[RecallResult] = []
        for doc in candidates:
            score = _cosine_similarity(embedding, doc.embedding)
            if score < rule.min_score:
                continue
            doc_tags = tuple(
                t for t in str(doc.metadata.get("tags", "")).split(",") if t
            )
            if rule.tags and not set(rule.tags).issubset(set(doc_tags)):
                continue
            results.append(
                RecallResult(
                    doc_id=str(doc.metadata.get("kb_doc_id", "")),
                    title=str(doc.metadata.get("title", "")),
                    chunk=doc.content,
                    score=score,
                    tags=doc_tags,
                )
            )
            if len(results) >= rule.top_k:
                break
        return results
