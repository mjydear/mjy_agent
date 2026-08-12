"""
📦 异步任务管理器
📍 架构位置：接口服务层，支撑"提交即返回任务ID + 轮询查询结果"的异步接口模式。
🎯 核心作用：把耗时的排障/工作流任务放到后台执行，避免长请求占满连接池；
             用 asyncio.Semaphore 限制并发（I/O 密集型 Agent 任务），另配 ThreadPoolExecutor 承接同步 CPU 任务。
🔗 依赖：标准库 asyncio / concurrent.futures；被 api/routes/tasks.py 使用，实例挂在 app.state.task_manager。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

# 任务体：返回一个可 await 的协程，产出可序列化的结果 dict
TaskCoroFactory = Callable[[], Awaitable[dict[str, Any]]]


@dataclass
class BackgroundTask:
    """一个后台任务的完整状态记录。"""

    task_id: str
    tenant_id: str
    kind: str
    status: str = "pending"  # pending -> running -> success/failed
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AsyncTaskManager:
    """后台任务调度器：并发受控、状态可轮询、结果带 TTL。"""

    def __init__(
        self,
        max_concurrency: int = 8,
        result_ttl_seconds: int = 3600,
        thread_pool_workers: int = 8,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._result_ttl = result_ttl_seconds
        self._tasks: dict[str, BackgroundTask] = {}
        self._pending_refs: set[asyncio.Task[Any]] = set()
        self._pool = ThreadPoolExecutor(
            max_workers=thread_pool_workers, thread_name_prefix="athena-task"
        )
        self._submitted = 0
        self._completed = 0
        self._failed = 0

    def submit(self, factory: TaskCoroFactory, tenant_id: str, kind: str) -> str:
        """
        提交一个后台任务，立即返回 task_id（不阻塞）。

        必须在有事件循环的异步上下文中调用（FastAPI 路由天然满足）。
        """
        self._cleanup_expired()
        task_id = f"{kind}-{uuid.uuid4().hex[:12]}"
        self._tasks[task_id] = BackgroundTask(
            task_id=task_id, tenant_id=tenant_id, kind=kind
        )
        self._submitted += 1
        ref = asyncio.create_task(self._run(task_id, factory))
        self._pending_refs.add(ref)
        ref.add_done_callback(self._pending_refs.discard)
        return task_id

    async def _run(self, task_id: str, factory: TaskCoroFactory) -> None:
        # 并发闸门：超过上限的任务在此排队，保护下游 LLM/工具不被打爆
        async with self._semaphore:
            record = self._tasks.get(task_id)
            if record is None:
                return
            record.status = "running"
            record.updated_at = time.time()
            try:
                record.result = await factory()
                record.status = "success"
                self._completed += 1
            except Exception as exc:  # 后台任务失败不能冒泡，只能落到状态里
                record.status = "failed"
                record.error = str(exc)
                self._failed += 1
            finally:
                record.updated_at = time.time()

    async def run_in_thread(self, fn: Callable[..., Any], *args: Any) -> Any:
        """把同步/CPU 密集函数丢到线程池执行，避免阻塞事件循环。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, fn, *args)

    def get(self, task_id: str, tenant_id: str | None = None) -> BackgroundTask | None:
        """按 task_id 查询任务；传入 tenant_id 时做租户越权校验。"""
        record = self._tasks.get(task_id)
        if record is None:
            return None
        if tenant_id is not None and record.tenant_id != tenant_id:
            return None  # 租户隔离：不返回别的租户的任务
        return record

    def _cleanup_expired(self) -> None:
        now = time.time()
        expired = [
            tid
            for tid, rec in self._tasks.items()
            if rec.status in {"success", "failed"}
            and now - rec.updated_at > self._result_ttl
        ]
        for tid in expired:
            self._tasks.pop(tid, None)

    def stats(self) -> dict[str, int]:
        return {
            "submitted": self._submitted,
            "completed": self._completed,
            "failed": self._failed,
            "in_flight": self._submitted - self._completed - self._failed,
        }

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)
