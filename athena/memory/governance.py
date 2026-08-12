"""
📦 模块名称：记忆治理（Memory Governance）
📍 架构位置：记忆层的质量管理组件，位于 LongTermMemory/SkillMemory 外侧。
🎯 核心作用：对长期记忆做遗忘、冲突检测、合并和质量审计，防止记忆库越用越乱。
🔗 依赖关系：只依赖 Python 标准库；可被 CuratorDaemon、长期记忆层或后台治理任务调用。
💡 设计思路：先用确定性规则实现可解释治理，再为未来 embedding/LLM 冲突检测预留接口。
📚 学习重点：理解“记忆不是越多越好”，Agent 长期运行时必须有清理、合并和审计机制。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass
class GovernedMemory:
    """
    可治理的记忆条目。

    功能说明：给普通记忆增加 importance、access_count、updated_at 等治理字段。
    参数说明：
        memory_id：记忆唯一标识。
        content：记忆内容。
        importance：重要性，越高越不容易被遗忘。
        access_count：访问次数，常用记忆会被保留。
        updated_at：更新时间戳。
    返回值：数据容器。
    设计思路：治理不直接绑定某个数据库格式，先用通用对象表达记忆质量。
    使用示例：GovernedMemory(memory_id="m1", content="db: use replica", importance=0.8)
    """

    memory_id: str
    content: str
    importance: float = 1.0
    access_count: int = 0
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.memory_id.strip():
            raise ValueError("memory_id must be non-empty")
        if not self.content.strip():
            raise ValueError("content must be non-empty")
        if self.importance < 0:
            raise ValueError("importance must be non-negative")


@dataclass(frozen=True)
class MemoryAuditReport:
    """
    记忆质量审计报告。

    功能说明：汇总低质量记忆和疑似冲突记忆组。
    参数说明：
        total：参与审计的记忆总数。
        low_quality_ids：低质量记忆 id 列表。
        conflict_groups：疑似冲突的记忆 id 分组。
    返回值：数据容器。
    设计思路：审计报告不直接删除数据，而是给上层策略决定，这样更安全。
    使用示例：report = governance.audit(memories)
    """

    total: int
    low_quality_ids: tuple[str, ...]
    conflict_groups: tuple[tuple[str, ...], ...]


class MemoryGovernance:
    """
    记忆治理：遗忘、冲突检测、质量审计。

    功能说明：提供长期记忆清理和质量维护能力。
    参数说明：
        decay_rate：每次治理时重要性衰减比例。
        low_quality_threshold：低于该阈值的记忆会被视为低质量。
    返回值：构造函数无返回；各方法返回治理后的列表或报告。
    设计思路：用“策略类”集中管理治理规则，避免把遗忘逻辑散在存储层里。
    使用示例：cleaned = MemoryGovernance().apply_forgetting(memories)
    """

    def __init__(
        self, decay_rate: float = 0.05, low_quality_threshold: float = 0.2
    ) -> None:
        if decay_rate < 0 or decay_rate > 1:
            raise ValueError("decay_rate must be in range 0..1")
        if low_quality_threshold < 0:
            raise ValueError("low_quality_threshold must be non-negative")
        self.decay_rate = decay_rate
        self.low_quality_threshold = low_quality_threshold

    def apply_forgetting(
        self, memories: Sequence[GovernedMemory]
    ) -> list[GovernedMemory]:
        """
        基于访问频率和重要性衰减过滤低价值记忆。

        功能说明：降低长期不用记忆的重要性，同时奖励经常被访问的记忆。
        参数说明：memories 是待治理的记忆列表。
        返回值：过滤后的 GovernedMemory 列表。
        设计思路：模拟人的记忆：不常用会淡忘，经常用会加深。
        使用示例：active_memories = governance.apply_forgetting(memories)

        🔍 原理讲解：
        输入 importance=0.5、access_count=3 的记忆。
        处理过程：先按 decay_rate 衰减，再按访问次数加一点补偿。
        输出：如果新 importance 高于阈值，就保留；否则遗忘。
        """
        governed: list[GovernedMemory] = []
        for memory in memories:
            # 💡 学习提示：访问次数最多只奖励到 10 次，避免一个旧记忆因为历史访问太多而永远无法被淘汰。
            decayed = (
                memory.importance * (1 - self.decay_rate)
                + min(memory.access_count, 10) * 0.02
            )
            if decayed >= self.low_quality_threshold:
                memory.importance = decayed
                governed.append(memory)
        return governed

    def detect_conflicts(
        self, memories: Sequence[GovernedMemory]
    ) -> tuple[tuple[str, ...], ...]:
        """
        根据简单 subject 前缀检测冲突记忆。

        功能说明：把形如 "subject: fact" 的记忆按 subject 分组，找出同一主题下的多条记忆。
        参数说明：memories 是待检测的记忆列表。
        返回值：冲突分组，每组包含多个 memory_id。
        设计思路：这是第一版便宜可解释的冲突检测，不依赖向量库或 LLM。
        使用示例：conflicts = governance.detect_conflicts(memories)
        """
        buckets: dict[str, list[str]] = {}
        for memory in memories:
            # 💡 学习提示：用冒号前缀当 subject 是一个简化约定，真实系统可替换为实体抽取。
            subject = memory.content.split(":", 1)[0].strip().lower()
            if subject:
                buckets.setdefault(subject, []).append(memory.memory_id)
        return tuple(tuple(ids) for ids in buckets.values() if len(ids) > 1)

    def merge_conflicts(
        self, memories: Sequence[GovernedMemory]
    ) -> list[GovernedMemory]:
        """
        保留同一 subject 下重要性最高的记忆。

        功能说明：对同一主题的多条记忆，只保留当前最重要的一条。
        参数说明：memories 是待合并的记忆列表。
        返回值：合并后的记忆列表。
        设计思路：冲突合并先做保守策略，不自动拼接内容，减少错误合并风险。
        使用示例：merged = governance.merge_conflicts(memories)
        """
        best: dict[str, GovernedMemory] = {}
        for memory in memories:
            subject = (
                memory.content.split(":", 1)[0].strip().lower() or memory.memory_id
            )
            current = best.get(subject)
            if current is None or memory.importance >= current.importance:
                best[subject] = memory
        return list(best.values())

    def audit(self, memories: Sequence[GovernedMemory]) -> MemoryAuditReport:
        """
        生成记忆质量审计报告。

        功能说明：列出低质量记忆和疑似冲突记忆，供后台任务或人类审查。
        参数说明：memories 是待审计的记忆列表。
        返回值：MemoryAuditReport。
        设计思路：审计只报告问题，不直接修改数据，符合企业系统的安全习惯。
        使用示例：report = governance.audit(memories)
        """
        low_quality = tuple(
            memory.memory_id
            for memory in memories
            if memory.importance < self.low_quality_threshold
        )
        return MemoryAuditReport(
            total=len(memories),
            low_quality_ids=low_quality,
            conflict_groups=self.detect_conflicts(memories),
        )


"""
🤔 思考题：

1. 如果两条记忆主题相同但内容互补，直接保留最高 importance 会不会丢信息？
2. 这里为什么审计不直接删除低质量记忆？
3. 如果 content 没有冒号，当前冲突检测会怎么处理？
4. ⚡ 优化建议：未来可以用 embedding 相似度检测“语义重复”，比当前 subject 前缀更准确。
"""
