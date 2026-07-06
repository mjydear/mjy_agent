"""嵌入提供者测试：默认降级、启用但缺凭证降级、运行时失败降级。"""

from __future__ import annotations

import asyncio

import pytest

from athena.config import AthenaSettings, EmbeddingSettings
from athena.infra.embeddings import (
    LiteLLMEmbeddingProvider,
    create_embedding_provider,
)
from athena.memory.long_term import HashEmbeddingProvider


def test_default_returns_hash_provider() -> None:
    provider = create_embedding_provider(AthenaSettings())
    assert isinstance(provider, HashEmbeddingProvider)


def test_enabled_without_key_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = AthenaSettings(
        embedding=EmbeddingSettings(enabled=True, model="text-embedding-3-small")
    )
    provider = create_embedding_provider(settings)
    assert isinstance(provider, HashEmbeddingProvider)


def test_runtime_failure_degrades_to_fallback() -> None:
    fallback = HashEmbeddingProvider(dimension=8)
    provider = LiteLLMEmbeddingProvider(
        model="text-embedding-3-small", fallback=fallback, dimension=8
    )

    # 未安装/未配置 litellm 凭证 → _embed_sync 抛错 → 自动降级
    vector = asyncio.run(provider.embed("hello world"))
    assert provider._degraded is True
    assert len(vector) == 8
    # 降级后直接走 fallback，向量与 hash 结果一致
    expected = asyncio.run(fallback.embed("hello world"))
    assert list(vector) == list(expected)
