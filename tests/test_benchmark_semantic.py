"""Benchmark 语义评分 + 关键词降级测试。"""

from __future__ import annotations

import pytest

from athena.agent.base import AgentResponse
from athena.evaluation.benchmark import BenchmarkCase, BenchmarkEngine


class _KeywordEmbedder:
    """把文本嵌成简单词袋向量，语义相近的文本余弦更高。"""

    def __init__(self, vocab: tuple[str, ...]) -> None:
        self._vocab = vocab

    async def embed(self, text: str) -> list[float]:
        lowered = text.lower()
        return [1.0 if word in lowered else 0.0 for word in self._vocab]


class _BrokenEmbedder:
    async def embed(self, text: str) -> list[float]:
        raise RuntimeError("embed down")


def _runner_factory(answer: str):
    async def runner(query: str) -> AgentResponse:
        return AgentResponse(answer=answer, steps=["plan"])

    return runner


@pytest.mark.asyncio
async def test_semantic_success_when_keywords_miss() -> None:
    # 答案不含关键词 "重启"，但语义（服务/异常）与参考接近
    embedder = _KeywordEmbedder(("服务", "异常", "重启", "内存"))
    engine = BenchmarkEngine(
        _runner_factory("服务出现异常"),
        embedding_provider=embedder,
        semantic_threshold=0.5,
    )
    case = BenchmarkCase(
        name="c1",
        query="q",
        expected_keywords=("重启",),
        golden_answer="服务异常",
    )
    results = await engine.run_cases((case,))
    assert results[0].success is True


@pytest.mark.asyncio
async def test_keyword_fallback_without_embedder() -> None:
    engine = BenchmarkEngine(_runner_factory("diagnosis ok"))
    case = BenchmarkCase(name="c2", query="q", expected_keywords=("diagnosis",))
    results = await engine.run_cases((case,))
    assert results[0].success is True


@pytest.mark.asyncio
async def test_semantic_failure_falls_back_to_keyword() -> None:
    engine = BenchmarkEngine(
        _runner_factory("完全无关的输出"),
        embedding_provider=_BrokenEmbedder(),
    )
    case = BenchmarkCase(name="c3", query="q", expected_keywords=("diagnosis",))
    results = await engine.run_cases((case,))
    # embedder 抛异常 + 关键词未命中 → 失败
    assert results[0].success is False
