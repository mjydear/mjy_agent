"""
📦 模块名称：向量数据库抽象层（Vector DB Gateway）
📍 架构位置：基础设施层（Infrastructure Layer）—— 与 llm.py 平级，同属底层基础设施
🎯 核心作用：给 Agent 提供"长期记忆"能力——把知识存成向量、按语义相似度检索
🔗 依赖关系：
   - 依赖：pymilvus（可选，生产环境向量数据库）、athena.exceptions（统一错误处理）
   - 被依赖：athena/memory/working.py（工作记忆模块通过此处读写长期记忆）
💡 设计思路：
   采用"两套实现 + Protocol 接口"模式，与 llm.py 风格一致：
   ① InMemoryVectorStore  → 纯内存实现，零依赖，用于本地开发和单元测试
   ② MilvusVectorStore    → 生产级向量数据库，支持亿级向量高速检索
   ③ VectorStore Protocol → 统一接口，上层代码无感知切换两种实现
   
   设计的核心思想：让 Agent 开发/测试阶段不依赖任何外部服务，
   切换到生产环境只需改配置，不改业务逻辑代码。
📚 学习重点：
   1. 什么是"向量嵌入"（Embedding）？为什么要把文字变成数字？
   2. 余弦相似度（Cosine Similarity）是如何衡量"意思相近"的？
   3. 懒加载（Lazy Loading）模式：_client() 方法的设计意图
   4. 幂等初始化（Idempotent Setup）：_ensure_collection() 的防重复创建逻辑
"""

from __future__ import annotations  # 💡 学习提示：支持类型注解中的前向引用，与 llm.py 保持一致

import logging
import asyncio
from collections.abc import Sequence
from typing import Protocol, cast

from pydantic import BaseModel, Field

from athena.exceptions import ErrorCode, VectorStoreError

logger = logging.getLogger(__name__)  # 💡 学习提示：模块级 logger，日志会显示 "athena.infra.vector_db"，便于定位问题


# ============================================================
# 📌 数据模型层：定义"存进去的数据长什么样"
# ============================================================


class MemoryDocument(BaseModel):
    """
    向量记忆库中存储的一条"记忆文档"。

    功能说明：
        Agent 的每条记忆都以这个格式存储——既有原始文字（content），
        也有文字对应的数字向量（embedding），还有额外的标签信息（metadata）。

    参数说明：
        doc_id:    文档的唯一标识符，类似数据库主键。用于去重和精确查找。
                   示例："mem_20260623_001"、"user_preference_color"
        content:   原始文字内容，就是人类能读懂的那部分。
                   示例："用户喜欢简洁的回答风格"
        embedding: 文字内容对应的向量表示（一串浮点数）。
                   这是文字被嵌入模型处理后的"数字指纹"，用于计算语义相似度。
                   维度通常是 1536（OpenAI text-embedding-3-small）或 768（其他模型）。
                   示例：[0.021, -0.034, 0.156, ...]（实际有上千个数字）
        metadata:  附加的键值对标签，用于过滤或分类记忆。
                   示例：{"source": "user_input", "session": "20260623", "type": "preference"}

    设计思路：
        同时存 content 和 embedding 是为了两个不同的用途：
        - embedding 用来做快速相似度搜索（找"语义最近的"记忆）
        - content 用来把搜索结果拼回提示词（让 AI 读懂原文）

    """
    """
    🔍 原理讲解：什么是"向量嵌入"（Embedding）？

    大模型无法直接比较两段文字是否"意思相近"，但可以比较数字。
    嵌入模型（Embedding Model）就是把文字翻译成一串数字向量的工具。

    举个例子：
    "苹果很甜"   → [0.12, 0.45, -0.03, ...]（1536 个数字）
    "水果味道好" → [0.11, 0.44, -0.02, ...]（1536 个数字，很接近！）
    "今天下雨了" → [0.78, -0.23, 0.67, ...]（1536 个数字，差异很大）

    两个向量越接近 → 原始文字语义越相似
    这就是为什么向量数据库能做"语义搜索"，而普通数据库只能做"关键词搜索"。
    """

    doc_id: str
    content: str
    embedding: Sequence[float]
    # 💡 学习提示：同 llm.py 中的 usage 字段，用 default_factory=dict 避免所有实例共享同一个字典
    metadata: dict[str, str] = Field(default_factory=dict)


# ============================================================
# 📌 接口层：定义"向量库能做什么"
# ============================================================

class VectorStore(Protocol):
    """
    向量存储的"接口契约"（Protocol）。

    功能说明：
        定义了所有向量存储必须提供的两个操作：存（add）和查（search）。
        任何实现了这两个方法的类都自动满足此协议。

    设计思路：
        与 llm.py 中的 LLMClient Protocol 相同的设计理念。
        上层的记忆模块（memory/working.py）只依赖这个接口，
        不管底层是内存存储还是 Milvus，调用代码完全一样。

    # 🎯 面试考点：这里为什么不直接在 InMemoryVectorStore 里加方法，而要单独定义 Protocol？
    # 答：Protocol 让你可以在不修改已有类的情况下声明"这个类满足某个接口"。
    # 未来接入 Qdrant、Weaviate 等其他向量库时，只要方法签名对，直接就能用，
    # 不需要改这些第三方库的代码，也不需要再写包装类。这是"开放-封闭原则"的体现。
    """

    async def add(self, document: MemoryDocument) -> None:
        """Persist one memory document."""

    async def search(self, embedding: Sequence[float], top_k: int) -> Sequence[MemoryDocument]:
        """Return the most relevant memory documents."""


# ============================================================
# 📌 实现层 ①：内存版（用于测试和本地开发）
# ============================================================

class InMemoryVectorStore(BaseModel):
    """
    基于内存的向量存储——零依赖的轻量级实现。

    功能说明：
        把所有文档存在内存的 Python 列表里。搜索时用纯 Python 计算余弦相似度，
        找出最相似的 top_k 个文档返回。
        程序重启后所有数据丢失（非持久化），适合开发调试和单元测试。

    设计思路：
        "测试替身"（Test Double）模式——在没有真实 Milvus 服务的情况下，
        用最简单的实现代替，让上层代码的测试不依赖任何外部基础设施。
        就像测试汽车驾驶程序，可以先用模拟器而不是真车。

    使用示例：
        store = InMemoryVectorStore()
        await store.add(MemoryDocument(
            doc_id="1", content="苹果很甜",
            embedding=[0.1, 0.2, 0.3]
        ))
        results = await store.search(embedding=[0.1, 0.2, 0.3], top_k=1)
        print(results[0].content)  # "苹果很甜"
    """

    # 💡 学习提示：documents 是可变列表，必须用 default_factory=list 而不是 default=[]
    # 原因同 metadata 字段：避免所有实例共享同一个列表对象。
    documents: list[MemoryDocument] = Field(default_factory=list)

    async def add(self, document: MemoryDocument) -> None:
        """
        将文档追加到内存列表中。

        功能说明：
            直接 append 到 self.documents，操作是瞬时的，没有 I/O。
            虽然方法是 async 的，但实际上没有任何异步操作——
            这样做是为了让接口签名与 MilvusVectorStore 保持一致，实现可互换。

        参数说明：
            document: 要存储的记忆文档

        返回值：
            无（None）

        # 🎯 面试考点：为什么一个完全同步的操作要声明为 async？
        # 答：接口统一性。VectorStore Protocol 要求 add() 是 async 方法。
        # 如果 InMemoryVectorStore 把 add() 声明为同步，就不满足 Protocol，
        # 类型检查会报错，也无法在 async 代码中 await 它。
        # 这是"里氏替换原则"的体现：子类（实现类）必须可以替换父类（Protocol）使用。
        """
        self.documents.append(document)

    async def search(self, embedding: Sequence[float], top_k: int) -> Sequence[MemoryDocument]:
        """
        通过余弦相似度在内存中搜索最相关的文档。

        功能说明：
            对所有存储的文档计算余弦相似度，按相似度从高到低排序，返回前 top_k 个。
            这是最简单直接的"暴力搜索"——每次都遍历全部文档。

        参数说明：
            embedding: 查询向量（通常是用户问题经过嵌入模型处理后的结果）
            top_k:     返回最相似的前几个文档（如 top_k=3 表示返回最相关的3条记忆）

        返回值：
            按相似度降序排列的 MemoryDocument 列表，最多 top_k 个

        设计思路：
            使用 Python 内置的 sorted() + lambda 实现排序，代码简洁可读。
            代价是性能：O(n) 遍历 + O(n log n) 排序，文档量大时会变慢。
            但对于 MVP 和测试场景，这已经足够了。

        # ⚡ 优化建议：如果文档量超过几千条，sorted() 暴力搜索会很慢。
        # 可以考虑：① 改用 numpy 做向量化批量计算（快 100x+）
        #           ② 接入真实的向量数据库（Milvus/FAISS）
        """
        ranked = sorted(
            self.documents,
            # 💡 学习提示：lambda 这里是一个"临时函数"，只在 sorted 的 key 参数里用一次。
            # 等价于：def get_similarity(document): return _cosine_similarity(embedding, document.embedding)
            # 用 lambda 可以把相关逻辑写在一起，不用在别处定义命名函数。
            key=lambda document: _cosine_similarity(embedding, document.embedding),
            reverse=True,  # 💡 学习提示：reverse=True 表示降序排列，相似度最高的排在最前面
        )
        return ranked[:top_k]  # 💡 学习提示：Python 切片，取前 top_k 个，如果文档总数不够则返回全部


# ============================================================
# 📌 实现层 ②：Milvus 生产版（用于真实部署）
# ============================================================

class MilvusVectorStore(BaseModel):
    """
    基于 Milvus 向量数据库的生产级实现。

    功能说明：
        Milvus 是专门为大规模向量相似度搜索设计的数据库，支持亿级向量毫秒级检索。
        这个类是 Milvus 的"适配器"——把 Milvus 的 API 包装成 Athena 的统一接口。

    参数说明：
        uri:             Milvus 服务地址，默认本地开发环境端口
        collection_name: Milvus 中的"表名"，相当于关系型数据库里的 Table
        dimension:       向量维度，必须和你用的嵌入模型匹配：
                         - OpenAI text-embedding-3-small → 1536
                         - text-embedding-ada-002        → 1536
                         - BGE-M3 (本地模型)             → 1024
        metric_type:     相似度计算方式，"COSINE" 表示余弦相似度（最常用）

    设计思路：
        MVP 阶段把 Milvus 隔离在这一层（而不是散落在业务代码里），
        好处是：测试 Agent 逻辑时不需要启动 Milvus 服务，改用 InMemoryVectorStore 即可。
        这是"依赖隔离"（Dependency Isolation）原则的实践。

    使用示例：
        store = MilvusVectorStore(uri="http://localhost:19530", dimension=1536)
        await store.add(MemoryDocument(doc_id="1", content="xxx", embedding=[...]))
        results = await store.search(embedding=[...], top_k=5)
    """

    uri: str = "http://localhost:19530"
    collection_name: str = "athena_memory"
    dimension: int = 1536
    metric_type: str = "COSINE"

    async def add(self, document: MemoryDocument) -> None:
        """
        异步地将一条记忆文档写入 Milvus。

        功能说明：
            把同步的 Milvus 写入操作放到线程池执行，避免阻塞 asyncio 事件循环。
            和 llm.py 中的 complete() 方法使用完全相同的模式。

        参数说明：
            document: 要持久化存储的记忆文档（包含向量和原文）

        返回值：
            无（None）

        异常：
            VectorStoreError: Milvus 连接失败、写入超时等情况都会抛出此异常

        设计思路：
            异常转译——把底层的 pymilvus 异常统一转换成 VectorStoreError，
            上层代码只需处理 Athena 自己的异常体系，不需要了解 pymilvus 的细节。
        """
        try:
            # 💡 学习提示：与 llm.py 完全相同的模式——同步阻塞代码放线程池
            # Milvus 的 Python SDK（pymilvus）是同步的，直接调用会阻塞事件循环
            await asyncio.to_thread(self._add_sync, document)
        except Exception as exc:
            # 💡 学习提示：logger.exception() 会自动附加完整的异常堆栈信息到日志，
            # 比 logger.error() 更适合在 except 块里使用，调试时信息更完整
            logger.exception("Milvus add failed")
            raise VectorStoreError(ErrorCode.VECTOR_STORE_FAILED, str(exc)) from exc

    async def search(self, embedding: Sequence[float], top_k: int) -> Sequence[MemoryDocument]:
        """
        异步地在 Milvus 中搜索语义相似的记忆文档。

        功能说明：
            把同步的 Milvus 向量搜索操作放到线程池执行，返回最相关的 top_k 条记忆。

        参数说明：
            embedding: 查询向量（通常是用户当前问题的嵌入表示）
            top_k:     返回最相似的前几条（Milvus 内部用 HNSW 索引做近似搜索，非常快）

        返回值：
            按相似度降序排列的 MemoryDocument 列表

        异常：
            VectorStoreError: 搜索失败时抛出
        """
        try:
            return await asyncio.to_thread(self._search_sync, embedding, top_k)
        except Exception as exc:
            logger.exception("Milvus search failed")
            raise VectorStoreError(ErrorCode.VECTOR_STORE_FAILED, str(exc)) from exc

    def _add_sync(self, document: MemoryDocument) -> None:
        """
        在工作线程中执行的同步 Milvus 写入逻辑（内部私有方法）。

        功能说明：
            创建 Milvus 客户端 → 确保 Collection 存在 → 插入数据。
            每次调用都创建新的客户端连接，适合无状态的 MVP 场景。

        设计思路：
            把数据结构从 Pydantic 对象转成 Milvus 需要的字典列表格式，
            字段名与 _ensure_collection() 中定义的 Schema 一一对应。
        """
        client = self._client()
        self._ensure_collection(client)
        client.insert(
            collection_name=self.collection_name,
            data=[
                {
                    "id": document.doc_id,
                    "vector": list(document.embedding),  # 💡 学习提示：Sequence 转 list，pymilvus 需要标准列表类型
                    "content": document.content,
                    "metadata": document.metadata,
                }
            ],
        )

    def _search_sync(self, embedding: Sequence[float], top_k: int) -> Sequence[MemoryDocument]:
        """
        在工作线程中执行的同步 Milvus 搜索逻辑（内部私有方法）。

        功能说明：
            向 Milvus 发送向量查询，然后把返回的原始结果解析成 MemoryDocument 列表。
            这里有不少字典解包的操作，是因为 pymilvus 返回的结构比较嵌套。
        """
        """
        🔍 原理讲解：Milvus 搜索结果的数据结构

        Milvus 的 search() 返回一个"分页列表的列表"：
        raw_results = [
            [  ← 第一个查询向量的结果（因为可以批量查，这里只查了1个）
                {
                    "id": "doc_001",
                    "distance": 0.95,        ← 相似度分数
                    "entity": {
                        "content": "苹果很甜",
                        "metadata": {"source": "user"}
                    }
                },
                { ... },  ← 第二个结果
            ]
        ]

        所以下面要先取 raw_results[0]（第一页），
        再遍历每个 item，从 entity 字典里拿 content 和 metadata。
        """
        client = self._client()
        self._ensure_collection(client)
        raw_results = client.search(
            collection_name=self.collection_name,
            data=[list(embedding)],  # 💡 学习提示：data 是"向量列表的列表"，支持批量查询，这里只查 1 个所以包一层 []
            limit=top_k,
            output_fields=["content", "metadata"],  # 💡 学习提示：指定要返回的字段，不写默认只返回 id 和距离，content 就拿不到了
        )
        # 💡 学习提示：raw_results[0] 取第一个查询的结果，因为我们只查了一个向量
        first_page = cast(Sequence[object], raw_results[0] if raw_results else [])
        documents: list[MemoryDocument] = []
        for item in first_page:
            # 💡 学习提示：cast() 是给类型检查器的"提示"，运行时不做任何转换
            # pymilvus 返回的是动态类型，用 cast 帮助 IDE 和 mypy 理解结构
            item_map = cast(dict[str, object], item)
            entity = cast(dict[str, object], item_map.get("entity", {}))
            metadata_value = entity.get("metadata", {})
            # 💡 学习提示：防御性检查——metadata 应该是 dict，但 Milvus 在某些异常情况
            # 可能返回其他类型，这里做了类型保护，避免后续代码崩溃
            metadata = metadata_value if isinstance(metadata_value, dict) else {}
            documents.append(
                MemoryDocument(
                    doc_id=str(item_map.get("id", "")),
                    content=str(entity.get("content", "")),
                    # 💡 学习提示：搜索结果里没有原始 embedding，用查询向量填充占位
                    # 这是个权衡：存储完整 embedding 会增加内存，但这里只是用于展示结果
                    embedding=list(embedding),
                    # 💡 学习提示：强制转 str，确保 metadata 的 key/value 都是字符串，
                    # 与 MemoryDocument 的类型声明 dict[str, str] 保持一致
                    metadata={str(key): str(value) for key, value in metadata.items()},
                )
            )
        return documents

    def _client(self) -> object:
        """
        懒加载（Lazy Loading）方式创建 Milvus 客户端。

        功能说明：
            每次需要客户端时才创建，而不是在类初始化时创建。

        返回值：
            MilvusClient 实例（返回 object 类型是为了避免在没安装 pymilvus 时导入报错）

        设计思路：
            "懒加载"模式——推迟资源的创建到真正需要时。
            好处：没安装 pymilvus 的环境（如只用 InMemoryVectorStore 的测试机）
            仍然可以正常导入整个模块，只有真正调用 add/search 时才会触发导入错误。

            这与 llm.py 的 _complete_sync 里延迟导入 litellm 是完全相同的模式。

        # ⚡ 优化建议：MVP 中每次操作都创建新连接，生产环境应该使用连接池或缓存客户端实例。
        # 可以用 @cached_property 或把 client 存为实例字段，避免重复建立 TCP 连接。
        """
        from pymilvus import MilvusClient  # 💡 学习提示：延迟导入——只在真正调用时才加载 pymilvus

        return MilvusClient(uri=self.uri)

    def _ensure_collection(self, client: object) -> None:
        """
        幂等地确保 Milvus Collection 存在（不存在就创建，已存在则跳过）。

        功能说明：
            在第一次写入或搜索之前，检查目标 Collection 是否存在，不存在则自动创建。
            多次调用是安全的——不会重复创建，也不会报错。

        参数说明：
            client: MilvusClient 实例（用 object 类型是为了不在模块顶部导入 pymilvus）

        设计思路：
            "幂等性"（Idempotency）——无论调用多少次，结果都和调用一次相同。
            这样代码更健壮：不需要在外部维护"有没有初始化"的状态标记，
            每次操作前都自检一下就好，类似"进门先确认灯是否开着，没开就开"。

        # 🎯 面试考点：这里为什么不把 create_collection 放在 __init__ 里？
        # 答：① __init__ 是同步的，而 Milvus 操作需要在线程里运行
        #     ② 把初始化放在首次使用时，让单元测试可以跳过整个 Milvus 连接
        #     ③ 避免对象创建时就产生网络连接，降低启动延迟
        """
        milvus_client = cast("MilvusClientProtocol", client)
        if milvus_client.has_collection(self.collection_name):
            return  # 💡 学习提示：已存在则提前返回，不重复创建（幂等）
        milvus_client.create_collection(
            collection_name=self.collection_name,
            dimension=self.dimension,
            primary_field_name="id",
            vector_field_name="vector",
            metric_type=self.metric_type,
            auto_id=False,  # 💡 学习提示：auto_id=False 表示由应用层自己管理 doc_id，不让 Milvus 自动分配。好处是我们能用有意义的 ID，便于后续精确查找和去重
        )


# ============================================================
# 📌 辅助协议层：给类型检查器用的 Milvus 接口描述
# ============================================================

class MilvusClientProtocol(Protocol):
    """
    PyMilvus MilvusClient 的轻量级 Protocol 描述。

    功能说明：
        Athena 只用到了 MilvusClient 的四个方法。
        这个 Protocol 精确声明了这四个方法的签名，
        让 _ensure_collection() 和 _add_sync() 里的 cast() 能获得正确的类型提示。

    设计思路：
        "接口最小化原则"（Interface Segregation Principle, ISP）——
        只声明你真正需要的那几个方法，而不是导入整个 MilvusClient 类。
        好处：
        ① 不需要安装 pymilvus 就能通过类型检查
        ② 测试时可以用任何满足这 4 个方法的 mock 对象替代真实 Milvus 客户端
        ③ 代码意图更清晰：明确标出了 Athena 依赖 Milvus 的哪些功能点

    # 🎯 面试考点：为什么不直接 from pymilvus import MilvusClient 然后用真实类？
    # 答：① pymilvus 是可选依赖，部分环境没有安装
    #     ② 测试时需要 mock 这个对象，Protocol 让 mock 更简单
    #     ③ 这个 Protocol 充当了"文档"的作用，一眼就知道用了哪些 API
    """

    def has_collection(self, collection_name: str) -> bool:
        """Return whether a collection exists."""

    def create_collection(
        self,
        collection_name: str,
        dimension: int,
        primary_field_name: str,
        vector_field_name: str,
        metric_type: str,
        auto_id: bool,
    ) -> None:
        """Create a Milvus collection."""

    def insert(self, collection_name: str, data: Sequence[dict[str, object]]) -> object:
        """Insert entities into a collection."""

    def search(
        self,
        collection_name: str,
        data: Sequence[Sequence[float]],
        limit: int,
        output_fields: Sequence[str],
    ) -> Sequence[Sequence[object]]:
        """Search a collection by vector."""


# ============================================================
# 📌 算法层：余弦相似度计算
# ============================================================

def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """
    计算两个向量之间的余弦相似度。

    功能说明：
        衡量两个向量在"方向"上有多相似。结果越接近 1.0，说明两个文本语义越相近；
        结果越接近 0.0，说明越不相关；负数表示语义相反（embedding 通常不会出现负结果）。

    参数说明：
        left:  第一个向量（如查询文本的 embedding）
        right: 第二个向量（如待比较文档的 embedding）

    返回值：
        float，范围 [-1.0, 1.0]，实际使用中通常在 [0.0, 1.0] 之间：
        - 1.0  → 完全相同方向（语义高度相似）
        - 0.5  → 有一定相关性
        - 0.0  → 完全不相关，或其中一个向量是零向量

    设计思路：
        纯 Python 实现，零依赖，直接对应数学公式，便于理解。
        代价是性能低于 numpy（后者用 C 实现了向量化运算）。
        对于 InMemoryVectorStore 的测试/MVP 场景完全够用。
    """
    """
    🔍 原理讲解：余弦相似度是什么？

    数学公式：
        cosine_similarity(A, B) = (A · B) / (|A| × |B|)

    其中：
        A · B  = 点积 = sum(a_i × b_i)    ← 两个向量"重叠"的程度
        |A|    = A 的模长 = sqrt(sum(a_i²)) ← 向量的"长度"
        |B|    = B 的模长

    为什么用余弦而不用欧氏距离？
    因为余弦关注的是"方向"（语义），而不是"长度"（文本长短）。
    一篇 100 字的文章和 1000 字的文章讲同一件事，
    余弦相似度会认为它们很接近，欧氏距离则不一定。

    举个例子：
    向量 A = [1, 0]（指向右）
    向量 B = [2, 0]（也指向右，但更长）
    → 余弦相似度 = 1.0（完全一样的方向）
    → 欧氏距离  = 1.0（长度不同，有距离）

    向量 A = [1, 0]（指向右）
    向量 C = [0, 1]（指向上）
    → 余弦相似度 = 0.0（方向完全不同）
    """
    if len(left) != len(right) or not left or not right:
        # 💡 学习提示：防御性检查——维度不匹配的两个向量无法计算相似度。
        # 返回 0.0 而不是抛异常，是因为这个函数通常在 sorted() 的 key 里调用，
        # 如果抛异常会中断整个排序过程。返回 0 让不匹配的文档排到最后，静默降级。
        return 0.0
    dot_product = sum(left_value * right_value for left_value, right_value in zip(left, right))
    # 💡 学习提示：** 0.5 等价于 math.sqrt()，这里用幂运算避免再 import math
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if left_norm == 0.0 or right_norm == 0.0:
        # 💡 学习提示：零向量（全是 0 的向量）无法计算余弦相似度（分母为 0 会除零错误）。
        # 实际中这种情况很罕见，但防御性处理是好习惯。
        return 0.0
    return dot_product / (left_norm * right_norm)
    # ⚡ 优化建议：当文档量超过几千条时，这个纯 Python 实现会很慢（逐元素循环）。
    # 可以改成：import numpy as np; return float(np.dot(left, right) / (np.linalg.norm(left) * np.linalg.norm(right)))
    # numpy 底层是 C，同样操作速度快 100x 以上。


"""
🤔 思考题（结合这个文件深入思考）：

1. InMemoryVectorStore 的性能边界：
   假设 Agent 积累了 10 万条记忆，每次 search() 都要遍历全部 10 万条计算相似度。
   你觉得这会有多慢？有什么办法可以在不接入 Milvus 的情况下提速？
   提示：想想 FAISS 这个库，或者"提前建索引"的思路。

2. 连接池问题：
   _client() 每次操作都创建一个新的 MilvusClient（新的 TCP 连接）。
   在高并发场景下（同时 100 个请求），这会有什么问题？
   你会怎么改造 MilvusVectorStore 来支持连接复用？

3. embedding 字段的权衡：
   搜索结果里的 MemoryDocument.embedding 被填充为"查询向量"而不是"原始存储的向量"。
   这样做有什么影响？你能想到什么场景下这会导致 bug？

4. 幂等性的局限：
   _ensure_collection() 是幂等的，但在多进程/多实例部署时会有竞争条件
   （两个进程同时检测到 collection 不存在，都去创建，其中一个会报错）。
   你会如何解决这个分布式初始化问题？

5. （选做）扩展新的向量库：
   现在要接入 Qdrant（另一个向量数据库），你需要：
   ① 创建 QdrantVectorStore 类（实现 VectorStore Protocol）
   ② 类似 MilvusClientProtocol 创建 QdrantClientProtocol
   ③ 上层代码需要改动吗？
   这个架构是否真的达到了"对扩展开放，对修改关闭"的目标？
"""
