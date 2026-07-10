"""
📦 模块名称：技能库（Skill Library）
📍 架构位置：记忆层 / 学习层交界（Memory & Learning Boundary）：
              [Curator / Developer] → Skill → 【SkillLibrary】 → [LongTermMemory]
🎯 核心作用：存储、检索和匹配可复用技能，让 Agent 能把重复解决问题的经验沉淀成可召回能力。
🔗 依赖关系：
    - 依赖：LongTermMemory（向量检索）、InMemoryVectorStore（默认本地实现）
    - 被依赖：未来的 Agent 技能选择器、后台 Curator、提示词组装器
💡 设计思路：
    技能本身是结构化对象，但匹配依赖向量检索：
    ① Skill 保存 name/description/content/tags 等人类可读元数据
    ② add_skill() 将技能拼成 searchable 文本写入 LongTermMemory
    ③ match() 根据用户 query 召回相关技能，再回到 skills 字典取完整对象

📚 学习重点：
    1. 为什么技能库复用长期记忆：语义匹配逻辑不重复造轮子
    2. 为什么 content 和 description 都参与索引：描述利于粗匹配，正文利于细粒度匹配
    3. 为什么 Skill 是 frozen dataclass：技能被写入后应尽量作为不可变版本看待
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from athena.infra.cache import CacheBackend, cache_get_json, cache_set_json
from athena.infra.vector_db import InMemoryVectorStore
from athena.memory.long_term import (
    HashEmbeddingProvider,
    LongTermMemory,
    LongTermMemoryRecord,
)

_INDEX_KEY = "skill:index"


@dataclass(frozen=True)
class Skill:
    """
    可复用技能的元数据模型。

    字段说明：
        name:        技能唯一名称
        description: 技能适用场景说明，影响检索匹配质量
        content:     技能正文，可包含步骤、约束、示例
        tags:        技能标签，辅助筛选和解释匹配原因
        version:     版本号，每次“修正/优化已有规则”自增（阶段②-C）
        success_count/failure_count: 复用成功/失败计数，供进化时评估与降权
        created_at:   创建时间
    """

    name: str
    description: str
    content: str
    tags: tuple[str, ...] = field(default_factory=tuple)
    version: int = 1
    success_count: int = 0
    failure_count: int = 0
    created_at: float = field(default_factory=time.time)


class SkillLibrary:
    """
    可向量检索、可持久化的技能库。

    设计思路：
        内部同时维护 dict 和 LongTermMemory：
        - dict 用于按 name 快速取完整 Skill 对象
        - LongTermMemory 用于按语义搜索最相关技能
        注入 cache 后，Skill 会持久化到 CacheBackend（Redis 可降级内存），重启不丢（阶段②-C）。
    """

    def __init__(
        self,
        memory: LongTermMemory | None = None,
        cache: CacheBackend | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self.memory = memory or LongTermMemory(
            InMemoryVectorStore(), HashEmbeddingProvider()
        )
        self.skills: dict[str, Skill] = {}
        self._cache = cache
        self._ttl = ttl_seconds

    async def load(self) -> None:
        """从持久化后端恢复所有 Skill 到内存并重建向量索引（可选，注入 cache 时使用）。"""
        if self._cache is None:
            return
        for name in cache_get_json(self._cache, _INDEX_KEY) or []:
            raw = cache_get_json(self._cache, f"skill:{name}")
            if raw is None:
                continue
            skill = Skill(
                name=raw["name"],
                description=raw["description"],
                content=raw["content"],
                tags=tuple(raw.get("tags", ())),
                version=raw.get("version", 1),
                success_count=raw.get("success_count", 0),
                failure_count=raw.get("failure_count", 0),
                created_at=raw.get("created_at", time.time()),
            )
            await self.add_skill(skill, persist=False)

    def _persist(self, skill: Skill) -> None:
        """把单条 Skill 与索引写回持久化后端。"""
        if self._cache is None:
            return
        cache_set_json(
            self._cache,
            f"skill:{skill.name}",
            {
                "name": skill.name,
                "description": skill.description,
                "content": skill.content,
                "tags": list(skill.tags),
                "version": skill.version,
                "success_count": skill.success_count,
                "failure_count": skill.failure_count,
                "created_at": skill.created_at,
            },
            ttl_seconds=self._ttl,
        )
        names = cache_get_json(self._cache, _INDEX_KEY) or []
        if skill.name not in names:
            names.append(skill.name)
            cache_set_json(self._cache, _INDEX_KEY, names)

    def _delete_persisted(self, name: str) -> None:
        """从持久化后端删除一条 Skill（否决/降权时使用）。"""
        if self._cache is None:
            return
        names = [n for n in (cache_get_json(self._cache, _INDEX_KEY) or []) if n != name]
        cache_set_json(self._cache, _INDEX_KEY, names)

    async def add_skill(self, skill: Skill, *, persist: bool = True) -> None:
        """Store a skill and index it for later matching.

        persist=False 用于从持久化后端 load 回放时，避免重复写盘。
        """
        self._validate_skill(skill)
        self.skills[skill.name] = skill
        searchable = f"{skill.name}\n{skill.description}\n{' '.join(skill.tags)}\n{skill.content}"
        await self.memory.add(
            skill.name,
            searchable,
            importance=2.0,
            metadata={"type": "skill", "tags": ",".join(skill.tags)},
        )
        if persist:
            self._persist(skill)

    def remove_skill(self, name: str) -> None:
        """从内存与持久化后端移除一条 Skill（否决反馈驱动，阶段②-C）。"""
        self.skills.pop(name, None)
        self._delete_persisted(name)

    async def match(self, query: str, top_k: int = 3) -> list[Skill]:
        """Return the best matching skills for a query."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        records = await self.memory.search(query, top_k=top_k)
        return [
            self.skills[record.doc_id]
            for record in records
            if record.doc_id in self.skills
        ]

    def explain_match(self, record: LongTermMemoryRecord) -> str:
        """Return a compact explanation for a matched skill record."""
        return f"{record.doc_id}: score={record.score:.3f}"

    def _validate_skill(self, skill: Skill) -> None:
        if not isinstance(skill, Skill):
            raise ValueError("skill must be a Skill instance")
        if not skill.name.strip():
            raise ValueError("skill.name must be non-empty")
        if not skill.description.strip():
            raise ValueError("skill.description must be non-empty")
        if not skill.content.strip():
            raise ValueError("skill.content must be non-empty")
