"""
任务 / 指标 / 评测报告持久化存储：把原本进程内的运行状态落到缓存后端（Redis/内存）。

企业级诉求：服务重启不丢任务与报告、多副本共享运行指标。Redis 可用时天然跨副本，
降级内存时行为一致（仅单进程）。与 SessionStore 同款模式：真实实现 + 自动降级。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from athena.infra.cache import CacheBackend, cache_get_json, cache_set_json

if TYPE_CHECKING:  # 避免运行时循环依赖：schemas 仅用于类型标注与序列化
    from athena.api.schemas import BenchmarkRunResponse, StepTrace
    from athena.api.services import TaskRecord

_ERROR_INDEX_KEY = "metrics:error:index"
_TOKENS_KEY = "metrics:tokens"


class TaskStore:
    """
    基于缓存后端的任务仓库。

    键结构：task:{id} 存单任务 JSON（含步骤轨迹）。
    仅按 id 读取，无需全量列举，因此不维护索引，交由 TTL 回收。
    """

    def __init__(self, cache: CacheBackend, ttl_seconds: int = 3600) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._cache = cache
        self._ttl = ttl_seconds

    def _key(self, task_id: str) -> str:
        return f"task:{task_id}"

    def save(self, record: "TaskRecord") -> None:
        """写入/更新一条任务记录（每次状态变更都应调用）。"""
        cache_set_json(
            self._cache, self._key(record.task_id), self._to_dict(record),
            ttl_seconds=self._ttl,
        )

    def get(self, task_id: str) -> "TaskRecord | None":
        raw = cache_get_json(self._cache, self._key(task_id))
        if raw is None:
            return None
        return self._from_dict(raw)

    @staticmethod
    def _to_dict(record: "TaskRecord") -> dict:
        return {
            "task_id": record.task_id,
            "status": record.status,
            "answer": record.answer,
            "steps": [step.model_dump() for step in record.steps],
            "error": record.error,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    @staticmethod
    def _from_dict(raw: dict) -> "TaskRecord":
        from athena.api.schemas import StepTrace
        from athena.api.services import TaskRecord

        steps: list[StepTrace] = [
            StepTrace.model_validate(item) for item in raw.get("steps", [])
        ]
        return TaskRecord(
            task_id=raw["task_id"],
            status=raw["status"],
            answer=raw.get("answer"),
            steps=steps,
            error=raw.get("error"),
            created_at=raw.get("created_at", time.time()),
            updated_at=raw.get("updated_at", time.time()),
        )


class MetricsStore:
    """
    基于缓存后端的运行指标仓库：错误分布 + Token 累计。

    错误分布用 incr 计数 + 名称索引；Token 用读改写累加。多副本共享同一 Redis 时天然聚合。
    """

    def __init__(self, cache: CacheBackend) -> None:
        self._cache = cache

    def incr_error(self, error_name: str) -> None:
        """错误计数 +1，并把错误名登记进索引以便枚举分布。"""
        self._cache.incr(f"metrics:error:{error_name}")
        names = cache_get_json(self._cache, _ERROR_INDEX_KEY) or []
        if error_name not in names:
            names.append(error_name)
            cache_set_json(self._cache, _ERROR_INDEX_KEY, names)

    def error_distribution(self) -> dict[str, int]:
        """读取全部错误名及其计数。"""
        names = cache_get_json(self._cache, _ERROR_INDEX_KEY) or []
        distribution: dict[str, int] = {}
        for name in names:
            raw = self._cache.get(f"metrics:error:{name}")
            distribution[name] = int(raw) if raw is not None else 0
        return distribution

    def add_tokens(self, amount: int) -> None:
        """Token 累计（读改写，支持任意增量）。"""
        if amount <= 0:
            return
        current = self.token_usage()
        cache_set_json(self._cache, _TOKENS_KEY, current + amount)

    def token_usage(self) -> int:
        return int(cache_get_json(self._cache, _TOKENS_KEY) or 0)


class BenchmarkStore:
    """基于缓存后端的评测报告仓库：key benchmark:{run_id}。"""

    def __init__(self, cache: CacheBackend, ttl_seconds: int = 86400) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._cache = cache
        self._ttl = ttl_seconds

    def _key(self, run_id: str) -> str:
        return f"benchmark:{run_id}"

    def save(self, report: "BenchmarkRunResponse") -> None:
        cache_set_json(
            self._cache, self._key(report.run_id), report.model_dump(),
            ttl_seconds=self._ttl,
        )

    def get(self, run_id: str) -> "BenchmarkRunResponse | None":
        raw = cache_get_json(self._cache, self._key(run_id))
        if raw is None:
            return None
        from athena.api.schemas import BenchmarkRunResponse

        return BenchmarkRunResponse.model_validate(raw)
