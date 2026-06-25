"""
📦 模块名称：可观测性基础设施（Observability）
📍 架构位置：基础设施层（Infrastructure Layer）—— 为上层关键路径提供指标和性能埋点：
              [Agent/Tools/Memory] → 【MetricsRegistry / performance_span】 → [Logs/Metrics]
🎯 核心作用：提供轻量级计数器、耗时统计和 span 上下文管理器，让关键路径可度量、可排查。
🔗 依赖关系：
    - 依赖：logging、time、contextlib
    - 被依赖：工具执行器、记忆检索、Agent 主循环等关键路径
💡 设计思路：
    MVP 阶段不直接引入 Prometheus/OpenTelemetry，而是先定义最小可用指标抽象：
    ① counters 记录发生次数
    ② timings 记录耗时样本
    ③ performance_span 用 context manager 包住关键路径，确保成功失败都能记录耗时

📚 学习重点：
    1. 为什么先做进程内 MetricsRegistry：低依赖、易测试、后续可适配外部指标系统
    2. 为什么 span 用 finally 记录耗时：即使抛异常也要留下性能数据
    3. 为什么参数要校验：指标名为空会让后续聚合结果不可用
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MetricsRegistry:
    """
    进程内指标注册表。

    功能说明：
        counters 适合记录调用次数、失败次数；timings 适合记录关键路径耗时。
        snapshot() 返回普通 dict，方便测试断言或未来暴露成 HTTP metrics endpoint。
    """

    counters: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    timings: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    def increment(self, name: str, value: int = 1) -> None:
        if not name.strip():
            raise ValueError("metric name must be non-empty")
        if value < 0:
            raise ValueError("counter increment must be non-negative")
        self.counters[name] += value

    def observe(self, name: str, duration_seconds: float) -> None:
        if not name.strip():
            raise ValueError("metric name must be non-empty")
        if duration_seconds < 0:
            raise ValueError("duration_seconds must be non-negative")
        self.timings[name].append(duration_seconds)

    def snapshot(self) -> dict[str, object]:
        return {"counters": dict(self.counters), "timings": {key: list(values) for key, values in self.timings.items()}}


@contextlib.contextmanager
def performance_span(metrics: MetricsRegistry, name: str) -> Iterator[None]:
    """
    记录同步关键路径的耗时和成功/失败计数。

    使用示例：
        with performance_span(metrics, "memory.search"):
            ...
    """
    if not isinstance(metrics, MetricsRegistry):
        raise ValueError("metrics must be a MetricsRegistry")
    if not name.strip():
        raise ValueError("span name must be non-empty")
    started_at = time.perf_counter()
    metrics.increment(f"{name}.started")
    try:
        yield
    except Exception:
        metrics.increment(f"{name}.failed")
        logger.exception("span failed: %s", name)
        raise
    finally:
        duration = time.perf_counter() - started_at
        metrics.observe(f"{name}.duration", duration)
        metrics.increment(f"{name}.finished")