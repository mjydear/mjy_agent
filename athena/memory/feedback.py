"""
📦 模块名称：人工修正反馈存储（Feedback Store）
📍 架构位置：记忆层，位于 CloudOps API 与 Skill 自进化闭环之间。
🎯 核心作用：采集用户对 Agent 诊断结论的“采纳 / 修正 / 否决”反馈，作为 Skill 进化的输入。
🔗 依赖关系：依赖 infra.cache 的 CacheBackend 做持久化；被 AthenaWebService 与 SkillEvolver 使用。
💡 设计思路：仿 ops_knowledge 的 _load/_persist 范式，持久化到 CacheBackend（Redis 可降级内存）。
📚 学习重点：人工反馈是自进化闭环里“最高质量的监督信号”，必须持久化且可按 task 检索。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from athena.infra.cache import CacheBackend, cache_get_json, cache_set_json

_INDEX_KEY = "feedback:index"

# 允许的反馈裁决：采纳 / 修正 / 否决
_VALID_VERDICTS = ("adopt", "correct", "reject")


@dataclass(frozen=True)
class FeedbackItem:
    """
    一条人工反馈记录。

    字段说明：
        feedback_id：唯一 id。
        task_id：被反馈的 CloudOps 任务 id（= Agent run_id / trace run_id）。
        verdict：adopt（采纳）| correct（修正）| reject（否决）。
        correction_text：verdict=correct 时的人工修正内容，可为空。
        created_at：创建时间。
    """

    feedback_id: str
    task_id: str
    verdict: str
    correction_text: str = ""
    created_at: float = field(default_factory=time.time)


class FeedbackStore:
    """
    人工反馈持久化存储（CacheBackend 后端，Redis 可降级内存）。

    功能说明：记录并检索用户对诊断结论的反馈；供 Skill 进化 job 消费。
    参数说明：cache 为空时用内存缓存；ttl_seconds 可选过期时间。
    设计思路：兼容同步 record/list 接口，持久化保证重启不丢、多副本可见。
    使用示例：store.record("cloud-k8s-x", "correct", "根因其实是镜像 tag 错误")
    """

    def __init__(
        self, cache: CacheBackend | None = None, ttl_seconds: int | None = None
    ) -> None:
        if cache is None:
            from athena.infra.cache import InMemoryCache

            cache = InMemoryCache(namespace="athena")
        self._cache = cache
        self._ttl = ttl_seconds
        self.items: dict[str, FeedbackItem] = {}
        self._load()

    def _load(self) -> None:
        """从持久化后端恢复所有反馈项到内存索引。"""
        for fid in cache_get_json(self._cache, _INDEX_KEY) or []:
            raw = cache_get_json(self._cache, f"feedback:{fid}")
            if raw is None:
                continue
            self.items[fid] = FeedbackItem(
                feedback_id=raw["feedback_id"],
                task_id=raw["task_id"],
                verdict=raw["verdict"],
                correction_text=raw.get("correction_text", ""),
                created_at=raw.get("created_at", time.time()),
            )

    def _persist(self, item: FeedbackItem) -> None:
        """把单条反馈与索引写回持久化后端。"""
        cache_set_json(
            self._cache,
            f"feedback:{item.feedback_id}",
            {
                "feedback_id": item.feedback_id,
                "task_id": item.task_id,
                "verdict": item.verdict,
                "correction_text": item.correction_text,
                "created_at": item.created_at,
            },
            ttl_seconds=self._ttl,
        )
        ids = cache_get_json(self._cache, _INDEX_KEY) or []
        if item.feedback_id not in ids:
            ids.append(item.feedback_id)
            cache_set_json(self._cache, _INDEX_KEY, ids)

    def record(
        self, task_id: str, verdict: str, correction_text: str = ""
    ) -> FeedbackItem:
        """
        记录一条人工反馈并持久化。

        功能说明：校验 verdict 合法后生成反馈项、入内存索引并落库。
        参数说明：task_id 被反馈任务；verdict adopt|correct|reject；correction_text 修正内容。
        返回值：新建的 FeedbackItem。
        使用示例：store.record("cloud-k8s-x", "reject")
        """
        normalized = (verdict or "").strip().lower()
        if normalized not in _VALID_VERDICTS:
            raise ValueError(f"verdict must be one of {_VALID_VERDICTS}, got {verdict!r}")
        if not task_id or not task_id.strip():
            raise ValueError("task_id must be non-empty")
        import uuid

        feedback_id = f"fb-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        item = FeedbackItem(
            feedback_id=feedback_id,
            task_id=task_id.strip(),
            verdict=normalized,
            correction_text=correction_text or "",
        )
        self.items[feedback_id] = item
        self._persist(item)
        return item

    def list(self, limit: int = 50) -> list[FeedbackItem]:
        """返回最近的反馈项（按创建时间倒序）。"""
        ordered = sorted(
            self.items.values(), key=lambda i: i.created_at, reverse=True
        )
        return ordered[: max(1, limit)]

    def for_task(self, task_id: str) -> list[FeedbackItem]:
        """返回某个任务的所有反馈项。"""
        return [item for item in self.items.values() if item.task_id == task_id]

    def unprocessed(self, processed_ids: set[str]) -> list[FeedbackItem]:
        """返回尚未被 Skill 进化 job 处理过的反馈项。"""
        return [
            item for fid, item in self.items.items() if fid not in processed_ids
        ]
