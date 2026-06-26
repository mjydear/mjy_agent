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
from dataclasses import dataclass, field


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
    内存版运维知识库。

    功能说明：提供记录案例和关键词搜索两个最小能力。
    参数说明：无构造参数；items 保存在当前进程内存中。
    返回值：record_case 返回知识 id；search 返回匹配知识列表。
    设计思路：MVP 先用 dict 实现仓库，后续可替换成 SQLite、Milvus 或企业知识库。
    使用示例：kb = OpsKnowledgeBase(); kb.record_case("alert", "cause", "fix", True)
    """

    def __init__(self) -> None:
        """
        初始化空知识库。

        功能说明：创建 knowledge_id 到 OpsKnowledgeItem 的内存索引。
        参数说明：无。
        返回值：None。
        设计思路：dict 按 id 查找快，也方便测试直接断言内容。
        使用示例：knowledge = OpsKnowledgeBase()
        """
        self.items: dict[str, OpsKnowledgeItem] = (
            {}
        )  # 💡 学习提示：这是进程内存，服务重启后会丢失，生产环境要换持久化存储。

    def record_case(
        self, title: str, root_cause: str, recommendation: str, success: bool
    ) -> str:
        """
        记录一次排障案例。

        功能说明：把告警标题、根因和建议保存成知识项。
        参数说明：title 是案例标题；root_cause 是根因；recommendation 是建议；success 表示是否成功。
        返回值：新知识项的 knowledge_id。
        设计思路：成功案例打 success 标签，失败或待复盘案例打 review 标签，便于后续筛选。
        使用示例：knowledge.record_case("CrashLoop", "env missing", "rollback", True)
        """
        knowledge_id = f"ops-{int(time.time() * 1000)}"
        tags = (
            "cloudops",
            "fault",
            "success" if success else "review",
        )  # 💡 学习提示：标签是最简单的分类方式，后续可以扩展成严重级别、系统名等。
        self.items[knowledge_id] = OpsKnowledgeItem(
            knowledge_id, title, root_cause, recommendation, tags
        )
        return knowledge_id

    def search(self, query: str) -> list[OpsKnowledgeItem]:
        """
        按关键词搜索运维知识。

        功能说明：在标题、根因、建议中做大小写不敏感匹配。
        参数说明：query 是用户输入的检索词。
        返回值：匹配到的 OpsKnowledgeItem 列表。
        设计思路：先用关键词搜索保证可解释和稳定，未来可替换为向量语义检索。
        使用示例：knowledge.search("CrashLoop")

        🎯 面试考点：为什么第一版不用向量数据库？答案：MVP 更重视闭环和可测试性，关键词搜索能先验证知识沉淀价值。
        """
        lowered = query.lower()
        return [
            item
            for item in self.items.values()
            if lowered in item.title.lower()
            or lowered in item.root_cause.lower()
            or lowered in item.recommendation.lower()
        ]


"""
🤔 思考题：

1. 如果 query 为空，当前会返回所有知识，这在真实系统里合理吗？
2. 如果两个案例 title 一样但 root_cause 不同，应该合并还是都保留？
3. 内存知识库服务重启会丢数据，生产环境你会换成什么存储？
4. ⚡ 优化建议：未来可以给 search 增加 top_k 和标签过滤，避免返回结果过多。
"""
