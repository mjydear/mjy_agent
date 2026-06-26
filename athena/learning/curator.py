"""
📦 模块名称：后台复盘守护进程（Curator Daemon）
📍 架构位置：学习层后台任务（Learning Background Task）：
              [Tracer] → 【CuratorDaemon】 → [Profile / LongTermMemory / SkillLibrary]
🎯 核心作用：在不阻塞用户请求的前提下，周期性复盘轨迹数据，沉淀用户画像、长期记忆和可复用技能。
🔗 依赖关系：
    - 依赖：asyncio、Tracer
    - 被依赖：CLI/TUI 或未来服务端启动流程
💡 设计思路：
    Curator 不是同步插在 Agent.run() 里的逻辑，而是后台 daemon task：
    ① start() 创建 asyncio task，立即返回，不阻塞主循环
    ② stop() 设置停止信号并取消任务，确保资源释放
    ③ job 通过依赖注入传入，方便测试和未来替换成真实复盘逻辑

    面试时可以强调：学习/复盘属于慢路径，不能拖慢用户交互的关键路径。
📚 学习重点：
    1. asyncio.create_task 如何启动非阻塞后台任务
    2. stop_event + cancel 如何保证守护任务可停止
    3. 为什么 job 用依赖注入，而不是写死 ProfileCurator
    4. 定期清理轨迹数据如何避免长期运行内存泄漏
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from athena.learning.tracer import Tracer

CuratorJob = Callable[[Tracer], Awaitable[None]]


class CuratorDaemon:
    """
    周期性复盘执行轨迹的 asyncio 守护任务。

    功能说明：
        默认 job 只做内存清理；生产场景可以注入更复杂的 job，
        例如总结用户偏好、提炼失败案例、写入长期记忆或生成新技能。
    """

    def __init__(
        self,
        tracer: Tracer,
        job: CuratorJob | None = None,
        interval_seconds: float = 60.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.tracer = tracer
        self.job = job or self._default_job
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Start the daemon without blocking the caller."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="athena-curator")

    async def stop(self) -> None:
        """Stop the daemon and release its task."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            await self.job(self.tracer)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.interval_seconds
                )
            except TimeoutError:
                continue

    async def _default_job(self, tracer: Tracer) -> None:
        if len(tracer.events) > tracer.max_events:
            del tracer.events[: len(tracer.events) - tracer.max_events]
