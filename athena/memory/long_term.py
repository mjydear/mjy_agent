"""
📦 模块名称：长期记忆（Long-Term Memory）
📍 架构位置：记忆层（Memory Layer）—— 位于 Agent 执行器和向量数据库之间：
              [ReActAgent] → 提炼重要信息 → 【LongTermMemory】 → [VectorStore / Milvus]
🎯 核心作用：把短期对话中值得保留的事实、偏好、任务经验写入向量库，
              并在后续任务中通过“语义相似度 + 时间衰减 + 重要性”混合评分召回。
🔗 依赖关系：
    - 依赖：athena.infra.vector_db.VectorStore（向量存储协议）
    - 被依赖：未来的 Curator、Agent 执行器、SkillLibrary 都可以复用这里的检索能力
💡 设计思路：
    本模块采用“接口注入 + 可配置混合检索”设计：
    ① EmbeddingProvider Protocol → 上层只依赖“能把文本转向量”的接口
    ② HashEmbeddingProvider       → 离线和测试场景的确定性嵌入，不依赖外部 API
    ③ HybridRetrievalWeights      → 检索权重集中配置，避免把 0.6/0.2/0.2 写死在业务逻辑中
    ④ LongTermMemory              → 把向量库召回结果重新排序，补上时间和重要性维度

    面试时可以重点讲：向量数据库只解决“语义相似”，但 Agent 记忆还要考虑“新不新、重不重要”。
    所以这里没有直接信任 VectorStore.search() 的顺序，而是在业务层做二次排序。
📚 学习重点：
    1. Protocol 如何让 Milvus、本地内存向量库、未来的 Qdrant 实现可替换
    2. 混合检索公式：semantic * 0.6 + recency * 0.2 + importance * 0.2
    3. 时间衰减为什么用指数函数，而不是简单按天数线性扣分
    4. 为什么测试嵌入用 HashEmbeddingProvider，而不是 mock 一大堆向量
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from athena.infra.vector_db import MemoryDocument, VectorStore


class EmbeddingProvider(Protocol):
    """
    文本嵌入提供者接口。

    功能说明：
        LongTermMemory 不关心底层用 OpenAI、本地模型还是哈希嵌入，
        只要求外部传入的对象实现 embed(text) 方法。

    # 🎯 面试考点：为什么这里用 Protocol 而不是抽象基类 ABC？
    # 答：Protocol 支持结构化子类型，只要方法签名对就能用，减少继承耦合。
    """

    async def embed(self, text: str) -> Sequence[float]:
        """Return an embedding vector for text."""


class HashEmbeddingProvider:
    """
    确定性的本地哈希嵌入实现。

    功能说明：
        将 token 哈希到固定维度的向量槽位里，再做 L2 归一化。
        它不是生产级语义模型，但有两个优点：零外部依赖、结果稳定，特别适合单元测试。

    设计思路：
        测试长期记忆时，我们关注的是混合检索排序逻辑，而不是真实 embedding 质量。
        用哈希向量可以让测试不依赖网络、不依赖 API Key，也不会因为模型版本变化而抖动。
    """

    def __init__(self, dimension: int = 128) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive")
        self.dimension = dimension

    async def embed(self, text: str) -> Sequence[float]:
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        vector = [0.0] * self.dimension
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            vector[index] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


@dataclass(frozen=True)
class HybridRetrievalWeights:
    """
    混合检索权重配置。

    字段说明：
        semantic:   语义相似度权重，默认 0.6，是主要排序依据
        recency:    时间新鲜度权重，默认 0.2，防止旧记忆永久霸榜
        importance: 重要性权重，默认 0.2，让用户偏好、关键决策更容易被召回

    # 🎯 面试考点：为什么权重要做成配置对象？
    # 答：不同场景权重不同。知识库问答更重语义，个人助理更重偏好和最近上下文。
    #     配置对象让策略可调，不需要改检索算法源码。
    """

    semantic: float = 0.6
    recency: float = 0.2
    importance: float = 0.2

    def __post_init__(self) -> None:
        values = (self.semantic, self.recency, self.importance)
        if any(value < 0 for value in values):
            raise ValueError("retrieval weights must be non-negative")
        if sum(values) <= 0:
            raise ValueError("at least one retrieval weight must be positive")


@dataclass(frozen=True)
class LongTermMemoryRecord:
    """长期记忆检索结果，包含文档内容、最终混合分数和元数据。"""

    doc_id: str
    content: str
    score: float
    metadata: dict[str, str] = field(default_factory=dict)


class LongTermMemory:
    """
    向量库驱动的长期记忆管理器。

    功能说明：
        add() 负责把文本写入向量库，并保存 created_at / importance 等排序元数据；
        search() 负责先按向量召回候选，再用混合公式进行业务排序。

    设计思路：
        把“存储召回”和“记忆排序策略”拆开：
        - VectorStore 管存取和相似度粗召回
        - LongTermMemory 管 Agent 语义下的记忆价值判断
        这样未来即使从 Milvus 换成别的向量库，混合检索策略也不用重写。
    """

    def __init__(
        self,
        vector_store: VectorStore,
        embedding_provider: EmbeddingProvider | None = None,
        weights: HybridRetrievalWeights | None = None,
        half_life_seconds: float = 7 * 24 * 60 * 60,
    ) -> None:
        if half_life_seconds <= 0:
            raise ValueError("half_life_seconds must be positive")
        self.vector_store = vector_store
        self.embedding_provider = embedding_provider or HashEmbeddingProvider()
        self.weights = weights or HybridRetrievalWeights()
        self.half_life_seconds = half_life_seconds

    async def add(
        self,
        doc_id: str,
        content: str,
        importance: float = 1.0,
        metadata: dict[str, str] | None = None,
    ) -> None:
        """
        写入一条长期记忆。

        参数说明：
            doc_id:     记忆唯一 ID，用于去重、解释召回来源
            content:    原始记忆内容，会被 embedding_provider 转为向量
            importance: 业务重要性分数，越高越容易在混合检索中排前
            metadata:   调用方附加标签，如 source/session/type
        """
        doc_id = self._require_text(doc_id, "doc_id")
        content = self._require_text(content, "content")
        if not math.isfinite(importance) or importance < 0:
            raise ValueError("importance must be a non-negative finite number")
        embedding = await self.embedding_provider.embed(content)
        document_metadata = dict(metadata or {})
        document_metadata.update(
            {"created_at": str(time.time()), "importance": str(importance)}
        )
        await self.vector_store.add(
            MemoryDocument(
                doc_id=doc_id,
                content=content,
                embedding=embedding,
                metadata=document_metadata,
            )
        )

    async def search(
        self, query: str, top_k: int = 5, candidate_k: int | None = None
    ) -> Sequence[LongTermMemoryRecord]:
        """
        使用混合检索搜索长期记忆。

        功能说明：
            先把 query 转成向量，从 VectorStore 中取 candidate_k 个候选；
            再计算 semantic、recency、importance 三个分量，得到最终 score。
        """
        query = self._require_text(query, "query")
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        query_embedding = await self.embedding_provider.embed(query)
        candidates = await self.vector_store.search(
            query_embedding, candidate_k or max(top_k * 4, top_k)
        )
        ranked = [
            self._score_document(query_embedding, document) for document in candidates
        ]
        ranked.sort(key=lambda record: record.score, reverse=True)
        return ranked[:top_k]

    def _score_document(
        self, query_embedding: Sequence[float], document: MemoryDocument
    ) -> LongTermMemoryRecord:
        semantic = self._cosine_similarity(query_embedding, document.embedding)
        created_at = float(document.metadata.get("created_at", "0") or 0)
        age = (
            max(0.0, time.time() - created_at)
            if created_at > 0
            else self.half_life_seconds
        )
        recency = math.exp(-age / self.half_life_seconds)
        importance = (
            min(float(document.metadata.get("importance", "1.0") or 1.0), 5.0) / 5.0
        )
        weight_total = (
            self.weights.semantic + self.weights.recency + self.weights.importance
        )
        score = (
            semantic * self.weights.semantic
            + recency * self.weights.recency
            + importance * self.weights.importance
        ) / weight_total
        return LongTermMemoryRecord(
            doc_id=document.doc_id,
            content=document.content,
            score=score,
            metadata=dict(document.metadata),
        )

    def _cosine_similarity(
        self, left: Sequence[float], right: Sequence[float]
    ) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return dot / (left_norm * right_norm)

    def _require_text(self, value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value.strip()
