"""
文本嵌入提供者：真实向量模型（litellm.embedding）+ 哈希嵌入降级。

企业级诉求：有可用 embedding 凭证时用真实语义向量；缺凭证或调用失败时自动退回
本地哈希嵌入，保证服务不因外部依赖缺失而崩溃。为避免向量空间维度漂移，
降级哈希向量与真实模型使用同一维度。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from athena.memory.long_term import EmbeddingProvider, HashEmbeddingProvider

logger = logging.getLogger(__name__)

# 常见 embedding 模型 → 需要的 API Key 环境变量
_MODEL_KEY_ENV = {
    "text-embedding-3-small": "OPENAI_API_KEY",
    "text-embedding-3-large": "OPENAI_API_KEY",
    "text-embedding-ada-002": "OPENAI_API_KEY",
}


def _required_key_env(model: str) -> str | None:
    """推断某 embedding 模型所需的 API Key 环境变量名。"""
    if model in _MODEL_KEY_ENV:
        return _MODEL_KEY_ENV[model]
    lowered = model.lower()
    if lowered.startswith(("text-embedding", "openai/")):
        return "OPENAI_API_KEY"
    if lowered.startswith(("cohere", "embed-")):
        return "COHERE_API_KEY"
    if lowered.startswith("voyage"):
        return "VOYAGE_API_KEY"
    return None  # 本地/自托管模型无需 Key


class LiteLLMEmbeddingProvider:
    """
    基于 litellm 的真实嵌入实现，失败时自动降级到哈希嵌入。

    维度与传入的 fallback 一致，保证真实向量与降级向量在同一空间可比。
    """

    def __init__(
        self,
        model: str,
        fallback: EmbeddingProvider,
        dimension: int,
    ) -> None:
        self.model = model
        self._fallback = fallback
        self.dimension = dimension
        self._degraded = False  # 一旦触发降级则记忆，避免反复打日志

    async def embed(self, text: str) -> Sequence[float]:
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        if self._degraded:
            return await self._fallback.embed(text)
        try:
            return await asyncio.to_thread(self._embed_sync, text)
        except Exception as exc:  # 网络/凭证/额度等任何异常都降级
            self._degraded = True
            logger.warning(
                "embedding model %s unavailable, falling back to hash embedding: %s",
                self.model,
                exc,
            )
            return await self._fallback.embed(text)

    def _embed_sync(self, text: str) -> Sequence[float]:
        from litellm import embedding  # 延迟导入，未装 litellm 不影响其它功能

        response = embedding(model=self.model, input=[text])
        vector = response["data"][0]["embedding"]
        if not vector:
            raise ValueError("embedding response is empty")
        return [float(v) for v in vector]


def create_embedding_provider(settings: object | None = None) -> EmbeddingProvider:
    """
    根据配置构建嵌入提供者：可用则真实模型，否则哈希降级。

    settings 期望具有 embedding.enabled/model/dimension；缺省或缺凭证时返回 HashEmbeddingProvider。
    """
    import os

    enabled = False
    model = "text-embedding-3-small"
    dimension = 1536
    emb = getattr(settings, "embedding", None) if settings is not None else None
    if emb is not None:
        enabled = getattr(emb, "enabled", False)
        model = getattr(emb, "model", model)
        dimension = getattr(emb, "dimension", dimension)

    fallback = HashEmbeddingProvider(dimension=dimension)
    if not enabled:
        return fallback

    key_env = _required_key_env(model)
    if key_env and not os.getenv(key_env):
        logger.warning(
            "embedding enabled but %s not set; using hash embedding fallback", key_env
        )
        return fallback

    return LiteLLMEmbeddingProvider(model=model, fallback=fallback, dimension=dimension)
