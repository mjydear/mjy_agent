"""
📦 模块名称：执行轨迹记录器（Execution Tracer）
📍 架构位置：学习层 / 可观测性边界（Learning & Observability Layer）：
              [Agent / ToolExecutor / Curator] → TraceEvent → 【EventBus / Tracer】 → [Memory / Logs]
🎯 核心作用：记录 Agent 执行过程中的关键事件，为调试、复盘、自我进化提供结构化数据。
🔗 依赖关系：
    - 依赖：标准库 dataclass/json/time
    - 被依赖：CuratorDaemon、未来的 Agent 执行循环、指标系统
💡 设计思路：
    使用观察者模式把“业务执行”和“轨迹记录”解耦：
    ① TraceEvent 只描述发生了什么
    ② EventBus 负责把事件广播给订阅者
    ③ Tracer 只是一个订阅者，负责保存和落盘

    这样 ReActAgent 未来只需要 publish 事件，不需要知道日志写到哪里、是否进入长期记忆。
📚 学习重点：
    1. 观察者模式如何降低主执行循环和可观测性系统的耦合
    2. max_events 如何防止后台复盘长期运行导致内存泄漏
    3. sink_path 如何把内存轨迹扩展为可持久化 JSONL 日志
    4. by_run / clear_run 如何支持按一次任务维度复盘和清理
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class TraceEvent:
    """
    单条执行轨迹事件。

    字段说明：
        name:      事件名，如 agent.step、tool.call、memory.compress
        run_id:    一次用户任务的关联 ID，用于把多个事件串成一条链路
        timestamp: 事件发生时间
        payload:   结构化扩展信息，统一用字符串值便于 JSONL 落盘和检索
    """

    name: str
    run_id: str
    timestamp: float = field(default_factory=time.time)
    payload: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("event name must be non-empty")
        if not self.run_id.strip():
            raise ValueError("run_id must be non-empty")


class TraceObserver(Protocol):
    """轨迹观察者接口，任何实现 on_event() 的对象都能订阅 EventBus。"""

    async def on_event(self, event: TraceEvent) -> None:
        """Handle one trace event."""


class EventBus:
    """
    最小异步事件总线。

    设计思路：
        这里不引入复杂消息队列，先用进程内观察者列表满足 MVP。
        未来如果要接 Kafka、Redis Stream，也可以把 EventBus 换成适配器。
    """

    def __init__(self) -> None:
        self._observers: list[TraceObserver] = []

    def subscribe(self, observer: TraceObserver) -> None:
        if observer in self._observers:
            return
        self._observers.append(observer)

    def unsubscribe(self, observer: TraceObserver) -> None:
        if observer in self._observers:
            self._observers.remove(observer)

    async def publish(self, event: TraceEvent) -> None:
        for observer in tuple(self._observers):
            await observer.on_event(event)


class Tracer:
    """
    可订阅事件总线的轨迹记录器。

    功能说明：
        record() 把事件保存在内存中；如果配置 sink_path，则同步写入 JSONL。
        超过 max_events 后自动删除最旧事件，避免守护进程跑久后内存无限增长。
    """

    def __init__(self, max_events: int = 10000, sink_path: Path | None = None) -> None:
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        self.max_events = max_events
        self.sink_path = sink_path
        self.events: list[TraceEvent] = []

    async def on_event(self, event: TraceEvent) -> None:
        self.record(event)

    def record(self, event: TraceEvent) -> None:
        if not isinstance(event, TraceEvent):
            raise ValueError("event must be a TraceEvent")
        self.events.append(event)
        if len(self.events) > self.max_events:
            del self.events[: len(self.events) - self.max_events]
        if self.sink_path is not None:
            self.sink_path.parent.mkdir(parents=True, exist_ok=True)
            with self.sink_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(self._serialize(event), ensure_ascii=False) + "\n"
                )

    def by_run(self, run_id: str) -> Sequence[TraceEvent]:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        return tuple(event for event in self.events if event.run_id == run_id)

    def clear_run(self, run_id: str) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        self.events = [event for event in self.events if event.run_id != run_id]

    def _serialize(self, event: TraceEvent) -> dict[str, object]:
        return {
            "name": event.name,
            "run_id": event.run_id,
            "timestamp": event.timestamp,
            "payload": event.payload,
        }
