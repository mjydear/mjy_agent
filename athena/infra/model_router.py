"""
多模型复杂度路由：按查询复杂度把请求分发到不同规格的模型。

简单查询走轻量模型（省成本、低延迟），复杂查询走强模型（保质量）。
复杂度用可解释的启发式打分（长度/代码块/多步关键词/消息轮数），不引入额外模型调用。
满足 LLMClient 协议（有 complete 方法），可无缝替换单一客户端。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from athena.infra.llm import LLMClient, LLMMessage, LLMResponse

logger = logging.getLogger(__name__)

# 暗示任务偏复杂的中英文关键词（多步推理/分析/设计等）
_COMPLEX_MARKERS = (
    "分析",
    "设计",
    "架构",
    "推导",
    "优化",
    "为什么",
    "对比",
    "调试",
    "排查",
    "step by step",
    "analyze",
    "design",
    "compare",
    "debug",
    "refactor",
    "explain why",
)


@dataclass(frozen=True)
class ComplexityWeights:
    """复杂度打分权重（各项归一化后加权求和，范围约 0..1）。"""

    length: float = 0.4
    code: float = 0.25
    markers: float = 0.2
    turns: float = 0.15


def estimate_prompt_complexity(
    messages: Sequence[LLMMessage], weights: ComplexityWeights | None = None
) -> float:
    """
    对一次对话估算复杂度分数（0..1）。

    综合：最新用户消息长度、是否含代码块、复杂关键词命中数、对话轮数。
    纯启发式、零额外调用，作为路由的快速前置判断。
    """
    weights = weights or ComplexityWeights()
    user_msgs = [m.content for m in messages if m.role == "user"]
    latest = user_msgs[-1] if user_msgs else ""
    text = latest.lower()

    length_score = min(len(latest) / 600.0, 1.0)  # 600 字符视为长
    code_score = 1.0 if ("```" in latest or re.search(r"\bdef |\bclass |;\n", latest)) else 0.0
    marker_hits = sum(1 for kw in _COMPLEX_MARKERS if kw in text)
    marker_score = min(marker_hits / 3.0, 1.0)
    turns_score = min(len(messages) / 10.0, 1.0)

    return (
        weights.length * length_score
        + weights.code * code_score
        + weights.markers * marker_score
        + weights.turns * turns_score
    )


class ModelRouter:
    """
    复杂度感知的模型路由器。

    根据 estimate_prompt_complexity 的分数选择 light / heavy 客户端：
    分数 >= threshold 走 heavy，否则走 light。
    """

    def __init__(
        self,
        light: LLMClient,
        heavy: LLMClient,
        threshold: float = 0.5,
        weights: ComplexityWeights | None = None,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be in [0, 1]")
        self._light = light
        self._heavy = heavy
        self._threshold = threshold
        self._weights = weights

    def route(
        self, messages: Sequence[LLMMessage], *, preference: str = "adaptive"
    ) -> tuple[LLMClient, str, float]:
        """返回选中的客户端、档位和复杂度，支持 Runtime 的预算偏好。"""
        score = estimate_prompt_complexity(messages, self._weights)
        if preference == "economy":
            return self._light, "light", score
        if preference == "quality":
            return self._heavy, "heavy", score
        if score >= self._threshold:
            return self._heavy, "heavy", score
        return self._light, "light", score

    async def complete(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        client, tier, score = self.route(messages)
        logger.info("model route tier=%s complexity=%.2f", tier, score)
        return await client.complete(messages)
