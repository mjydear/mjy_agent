"""阶段5 业务深化测试：知识库管理后台 + 多模型复杂度路由。"""

from __future__ import annotations

import pytest

from athena.infra.llm import LLMMessage, LLMResponse
from athena.infra.model_router import (
    ModelRouter,
    estimate_prompt_complexity,
)
from athena.infra.vector_db import InMemoryVectorStore
from athena.memory.knowledge_base import (
    KnowledgeBaseManager,
    RecallRule,
    chunk_text,
)


def test_chunk_text_overlap() -> None:
    text = "a" * 1000
    chunks = chunk_text(text, chunk_size=400, overlap=50)
    assert len(chunks) >= 2
    assert all(len(c) <= 400 for c in chunks)


@pytest.mark.asyncio
async def test_knowledge_ingest_and_recall() -> None:
    kb = KnowledgeBaseManager(InMemoryVectorStore(), chunk_size=100, overlap=20)
    doc = await kb.ingest(
        "Redis 排障",
        "Redis 连接超时 通常是 maxclients 打满 或 网络分区 导致 需要检查连接数",
        tags=["redis", "ops"],
    )
    assert doc.chunk_count >= 1
    assert len(kb.list_documents()) == 1

    hits = await kb.recall("Redis 连接超时", RecallRule(top_k=3))
    assert hits
    assert hits[0].title == "Redis 排障"


@pytest.mark.asyncio
async def test_knowledge_tag_filter() -> None:
    kb = KnowledgeBaseManager(InMemoryVectorStore(), chunk_size=100, overlap=20)
    await kb.ingest("A 文档", "kubernetes pod crashloop 排查", tags=["k8s"])
    await kb.ingest("B 文档", "kubernetes pod crashloop 排查", tags=["redis"])
    hits = await kb.recall(
        "kubernetes crashloop", RecallRule(top_k=5, tags=("k8s",))
    )
    assert hits
    assert all("k8s" in h.tags for h in hits)


def test_knowledge_delete() -> None:
    import asyncio

    kb = KnowledgeBaseManager(InMemoryVectorStore())
    doc = asyncio.run(kb.ingest("t", "some content here", tags=[]))
    assert kb.delete_document(doc.doc_id) is True
    assert kb.delete_document(doc.doc_id) is False


def test_complexity_estimate_ranks_hard_higher() -> None:
    simple = [LLMMessage(role="user", content="你好")]
    hard = [
        LLMMessage(
            role="user",
            content="请分析并设计一个高可用架构，对比不同方案的优缺点并推导性能瓶颈 " * 5,
        )
    ]
    assert estimate_prompt_complexity(hard) > estimate_prompt_complexity(simple)


class _StubLLM:
    def __init__(self, name: str) -> None:
        self.name = name

    async def complete(self, messages):
        return LLMResponse(content=self.name, model=self.name)


@pytest.mark.asyncio
async def test_model_router_selects_by_complexity() -> None:
    router = ModelRouter(_StubLLM("light"), _StubLLM("heavy"), threshold=0.3)
    simple = [LLMMessage(role="user", content="hi")]
    hard = [
        LLMMessage(
            role="user",
            content="分析 设计 架构 优化 对比 调试 " * 10,
        )
    ]
    assert (await router.complete(simple)).model == "light"
    assert (await router.complete(hard)).model == "heavy"
