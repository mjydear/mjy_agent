"""
📦 模块名称：CloudOps 运维知识库
📍 架构位置：记忆层的运维经验存储，位于故障工作流和知识检索 API 之间。
🎯 核心作用：把成功排障案例保存为可搜索、可复用的运维知识。
🔗 依赖关系：依赖 dataclass/time；被 FaultDiagnoseWorkflow 和 AthenaWebService 依赖。
💡 设计思路：使用轻量内存仓库模式，先跑通“记录案例 → 检索复用”的闭环。
📚 学习重点：关注 record_case 和 search 如何把一次排障经验变成后续可检索资产。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from athena.infra.cache import CacheBackend, cache_get_json, cache_set_json

_INDEX_KEY = "ops:index"


@dataclass(frozen=True)
class OpsKnowledgeItem:
    """
    一条可复用的运维知识。

    功能说明：保存故障标题、根因、建议、标签和创建时间。
    参数说明：knowledge_id 是唯一 id；title 通常是告警名；root_cause/recommendation 是复盘核心。
    返回值：数据容器，不主动执行逻辑。
    设计思路：知识项保持结构化，未来可以直接写入向量库或数据库。
    使用示例：OpsKnowledgeItem("ops-1", "CrashLoop", "process exits", "rollback", ("cloudops",))
    """

    knowledge_id: str
    title: str
    root_cause: str
    recommendation: str
    tags: tuple[str, ...]
    created_at: float = field(
        default_factory=time.time
    )  # 💡 学习提示：用 default_factory 确保每条知识创建时才取当前时间。


class OpsKnowledgeBase:
    """
    运维知识库：Redis/内存持久化 + 关键词检索，可选向量语义召回。

    功能说明：记录排障案例并支持检索。持久化后服务重启不丢；注入向量库+嵌入器时
        可用语义召回，缺失时降级关键词匹配。
    参数说明：
        cache：持久化后端，缺省内存缓存（不跨进程）。
        vector_store/embedding_provider：可选，二者齐备才启用语义召回。
    设计思路：兼容旧的同步 record_case/search 接口，新增异步 semantic_search 做语义检索。
    使用示例：kb = OpsKnowledgeBase(cache); kb.record_case("alert", "cause", "fix", True)
    """

    def __init__(
        self,
        cache: CacheBackend | None = None,
        vector_store: "object | None" = None,
        embedding_provider: "object | None" = None,
        ttl_seconds: int | None = None,
    ) -> None:
        """
        初始化知识库并从持久化后端恢复历史知识。

        功能说明：创建 knowledge_id 到 OpsKnowledgeItem 的索引，并载入已持久化的知识。
        参数说明：cache 为空时用内存缓存；vector_store/embedding_provider 用于语义召回。
        返回值：None。
        设计思路：dict 按 id 查找快；持久化保证重启不丢；向量能力可选注入。
        使用示例：knowledge = OpsKnowledgeBase(cache)
        """
        if cache is None:
            from athena.infra.cache import InMemoryCache

            cache = InMemoryCache(namespace="athena")
        self._cache = cache
        self._ttl = ttl_seconds
        self._vector_store = vector_store
        self._embedder = embedding_provider
        self._indexed: set[str] = set()  # 已写入向量库的 id，避免重复嵌入
        self.items: dict[str, OpsKnowledgeItem] = {}
        self._load()

    def _load(self) -> None:
        """从持久化后端恢复所有知识项到内存索引。"""
        for kid in cache_get_json(self._cache, _INDEX_KEY) or []:
            raw = cache_get_json(self._cache, f"ops:{kid}")
            if raw is None:
                continue
            self.items[kid] = OpsKnowledgeItem(
                knowledge_id=raw["knowledge_id"],
                title=raw["title"],
                root_cause=raw["root_cause"],
                recommendation=raw["recommendation"],
                tags=tuple(raw.get("tags", ())),
                created_at=raw.get("created_at", time.time()),
            )

    def _persist(self, item: OpsKnowledgeItem) -> None:
        """把单条知识与索引写回持久化后端。"""
        cache_set_json(
            self._cache,
            f"ops:{item.knowledge_id}",
            {
                "knowledge_id": item.knowledge_id,
                "title": item.title,
                "root_cause": item.root_cause,
                "recommendation": item.recommendation,
                "tags": list(item.tags),
                "created_at": item.created_at,
            },
            ttl_seconds=self._ttl,
        )
        ids = cache_get_json(self._cache, _INDEX_KEY) or []
        if item.knowledge_id not in ids:
            ids.append(item.knowledge_id)
            cache_set_json(self._cache, _INDEX_KEY, ids)

    def record_case(
        self, title: str, root_cause: str, recommendation: str, success: bool
    ) -> str:
        """
        记录一次排障案例并持久化。

        功能说明：把告警标题、根因和建议保存成知识项并写入持久化后端。
        参数说明：title 是案例标题；root_cause 是根因；recommendation 是建议；success 表示是否成功。
        返回值：新知识项的 knowledge_id。
        设计思路：成功案例打 success 标签，失败或待复盘案例打 review 标签，便于后续筛选。
        使用示例：knowledge.record_case("CrashLoop", "env missing", "rollback", True)
        """
        knowledge_id = f"ops-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        tags = (
            "cloudops",
            "fault",
            "success" if success else "review",
        )  # 💡 学习提示：标签是最简单的分类方式，后续可以扩展成严重级别、系统名等。
        item = OpsKnowledgeItem(
            knowledge_id, title, root_cause, recommendation, tags
        )
        self.items[knowledge_id] = item
        self._persist(item)
        return knowledge_id

    def search(self, query: str) -> list[OpsKnowledgeItem]:
        """
        按关键词搜索运维知识。

        功能说明：在标题、根因、建议中做大小写不敏感匹配。
        参数说明：query 是用户输入的检索词。
        返回值：匹配到的 OpsKnowledgeItem 列表。
        设计思路：关键词搜索稳定可解释，作为语义召回不可用时的降级路径。
        使用示例：knowledge.search("CrashLoop")

        🎯 面试考点：为什么保留关键词搜索？答案：作为向量库/嵌入缺失时的确定性兜底。
        """
        lowered = query.lower()
        return [
            item
            for item in self.items.values()
            if lowered in item.title.lower()
            or lowered in item.root_cause.lower()
            or lowered in item.recommendation.lower()
        ]

    async def semantic_search(
        self, query: str, top_k: int = 5
    ) -> list[OpsKnowledgeItem]:
        """
        语义召回运维知识，向量能力缺失时降级关键词搜索。

        功能说明：把知识项惰性写入向量库并按查询向量检索最相关的 top_k 条。
        参数说明：query 检索词；top_k 返回条数上限。
        返回值：按语义相关度排序的 OpsKnowledgeItem 列表。
        设计思路：index-on-read 惰性建索引，避免同步 record_case 中做异步嵌入。
        """
        if self._vector_store is None or self._embedder is None:
            return self.search(query)[:top_k]
        try:
            from athena.infra.vector_db import MemoryDocument

            for item in self.items.values():
                if item.knowledge_id in self._indexed:
                    continue
                text = f"{item.title}\n{item.root_cause}\n{item.recommendation}"
                vector = await self._embedder.embed(text)
                await self._vector_store.add(
                    MemoryDocument(
                        doc_id=item.knowledge_id,
                        content=text,
                        embedding=list(vector),
                        metadata={"title": item.title},
                    )
                )
                self._indexed.add(item.knowledge_id)

            query_vec = await self._embedder.embed(query)
            hits = await self._vector_store.search(query_vec, top_k=top_k)
            results = [
                self.items[doc.doc_id]
                for doc in hits
                if doc.doc_id in self.items
            ]
            return results or self.search(query)[:top_k]
        except Exception:  # 向量后端异常 → 关键词兜底
            return self.search(query)[:top_k]


"""
🤔 思考题：

1. 如果 query 为空，当前会返回所有知识，这在真实系统里合理吗？
2. 如果两个案例 title 一样但 root_cause 不同，应该合并还是都保留？
3. 内存知识库服务重启会丢数据，生产环境你会换成什么存储？
4. ⚡ 优化建议：未来可以给 search 增加 top_k 和标签过滤，避免返回结果过多。
"""
