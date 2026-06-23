"""
📦 模块名称：Agent 核心抽象层（Agent Base Abstractions）
📍 架构位置：Agent 层 —— 整个架构的"大脑中枢"，位于基础设施层（llm/vector_db）之上，
           是 CLI 层调用的直接对象。在分层架构中处于中间位置：
           [CLI 层] → [Agent 层（本文件）] → [基础设施层（LLM/向量库）]
🎯 核心作用：用最少的代码定义"Agent 是什么"——接收一个问题、返回一个答案
🔗 依赖关系：
   - 依赖：pydantic（数据校验）
   - 被依赖：
     * athena/agent/executor.py（ReActAgent 是这里 Agent Protocol 的具体实现）
     * athena/cli/main.py（CLI 层通过这里的 AgentResponse 处理输出）
💡 设计思路：
   "接口与实现分离"原则：
   ① base.py   → 只定义"接口契约"（Agent 能做什么、返回什么格式）
   ② executor.py → 实现具体的 ReAct 推理循环（怎么做到的）
   
   这个文件故意保持极简——只有数据模型和协议接口，不含任何业务逻辑。
   好处：接口稳定，实现可以随时替换（从 ReAct 换成 CoT、换成 Plan-Execute 等）。
📚 学习重点：
   1. Protocol 在 Agent 框架中的作用（与 llm.py 和 vector_db.py 同样的设计理念）
   2. AgentResponse 中 steps 字段的用途——它记录了 Agent 的"推理链"
   3. 为什么 Agent 接口只有一个 run() 方法？（最小化接口设计）
   4. 这个文件和 executor.py 的分工是怎么划定的？
"""

from __future__ import annotations  # 💡 学习提示：支持类型注解中的前向引用，全项目统一风格

from typing import Protocol

from pydantic import BaseModel, Field


# ============================================================
# 📌 数据模型层：定义"Agent 的返回格式长什么样"
# ============================================================


class AgentResponse(BaseModel):
    """
    Agent 执行完毕后返回给调用方的标准化结果。

    功能说明：
        封装了两部分信息：最终答案（answer）和推理过程记录（steps）。
        answer 是用户最终看到的回复；steps 是 Agent 每一步的思考记录，
        用于调试、日志、以及让用户理解"AI 是怎么想的"。

    参数说明：
        answer: Agent 给出的最终回答文字。
                对应 ReAct 循环中的 "Final Answer"。
                示例："北京今天的天气是晴天，气温 28°C。"

        steps:  Agent 执行过程中每个推理步骤的文字记录列表（思考链）。
                在 ReAct 框架里，每步包含 "Thought: ..." 和 "Observation: ..."。
                示例：[
                    "Thought: 用户想知道北京天气，我需要调用天气工具",
                    "Observation: 天气API返回：晴天28°C",
                    "Thought: 已获取到结果，可以给出最终答案"
                ]
                调试时打印 steps 就能看到 Agent 的完整推理过程，非常有用。

    设计思路：
        把"结果"和"过程"放在同一个对象里返回，而不是只返回答案字符串。
        这个设计参考了 LangChain 的 AgentFinish 和 OpenAI 的 tool_calls 结构。
        好处：
        ① 调用方可以选择只用 answer，也可以展示整个推理链给用户看
        ② 方便写测试——断言 steps 可以验证 Agent 是否走了预期的推理路径
        ③ 日志里记录 steps 可以事后分析 Agent 的行为

    使用示例：
        # executor.py 里创建的方式：
        return AgentResponse(
            answer="北京今天晴天",
            steps=["Thought: 查询天气", "Observation: 晴天28°C"]
        )

        # CLI 层使用的方式：
        response = await agent.run("北京天气怎么样？")
        print(response.answer)   # 只给用户看答案
        print(response.steps)    # 调试时打印推理链
    """
    answer: str
    # 💡 学习提示：同样用 default_factory=list 而非 default=[]，避免所有实例共享同一列表对象
    # 如果不用 Field(default_factory=list) 而用 steps: list[str] = []，
    # Pydantic v2 实际上会处理这个问题，但显式使用 Field 是更清晰的表达意图
    steps: list[str] = Field(default_factory=list)


# ============================================================
# 📌 接口层：定义"Agent 能做什么"（最小化接口设计）
# ============================================================


class Agent(Protocol):
    """
    Athena Agent 的"接口契约"（Protocol）。

    功能说明：
        定义了所有 Athena Agent 必须提供的最小接口：一个 run() 方法。
        接收用户问题，返回 AgentResponse。
        就像餐厅的菜单定义了"能点什么"，而不是规定"厨师怎么做菜"。

    设计思路：
        这是整个 Agent 框架最核心的设计决策之一——
        用一个方法（run）定义 Agent 的全部对外接口。

        为什么只有一个方法？
        "最小化接口"（Minimal Interface）原则：
        接口越简单，实现越自由，替换成本越低。
        对调用方（CLI 层）来说，它只需要知道"给一个问题，拿一个答案"，
        不需要关心 Agent 内部是 ReAct、CoT、还是 Plan-Execute 等不同策略。

    # 🎯 面试考点：Agent 只有一个 run() 方法，这遵循了哪个设计原则？
    # 答：接口隔离原则（ISP, Interface Segregation Principle）——
    # 调用方不应该依赖它不需要的接口。CLI 只需要"运行并获得结果"，
    # 不需要知道 Agent 的内部状态、记忆管理、工具调用等细节。
    # 如果接口方法过多，每个实现类都要实现所有方法，灵活性大幅降低。
    """
    """
    🔍 原理讲解：这个 Protocol 和 executor.py 的关系

    base.py（本文件）：
        Agent Protocol ← 定义了接口的"形状"

    executor.py：
        ReActAgent ← 真正的实现，用 ReAct（Reasoning + Acting）循环来工作

    调用链：
    CLI 层 → agent.run("用户问题") → ReActAgent.run() → LLM → 工具 → LLM → 最终答案

    ReActAgent 之所以满足 Agent Protocol，是因为它有一个签名匹配的 run() 方法：
        async def run(self, query: str) -> AgentResponse

    未来如果要实现一个"只用 Chain-of-Thought 不调工具"的简单 Agent：
        class SimpleCoTAgent:
            async def run(self, query: str) -> AgentResponse:
                ...  # 直接问 LLM，不用工具
    它会自动满足 Agent Protocol，无需修改任何现有代码。
    """

    # 🎯 面试考点：这里为什么用 Protocol 而不是 ABC（抽象基类）？
    # 答：
    # ABC 方式：class Agent(ABC): @abstractmethod async def run(...) → 实现类必须显式继承 Agent
    # Protocol 方式：任何有正确 run() 方法的类自动满足，无需继承
    #
    # 在 Agent 框架里 Protocol 更合适，原因：
    # ① 第三方 Agent 实现（如 LangChain 的 AgentExecutor）不用改源码就能接入
    # ② 测试时创建 Mock Agent 更简单（只需要有 run 方法，不用 import 这个 Protocol）
    # ③ 避免多重继承的复杂性（Python 的 MRO 问题）
    async def run(self, query: str) -> AgentResponse:
        """
        运行 Agent 处理一个用户查询。

        参数说明：
            query: 用户输入的问题或任务描述
                   示例："帮我查一下北京今天的天气"
                         "计算 123 * 456 等于多少"
                         "用 Python 写一个冒泡排序"

        返回值：
            AgentResponse，包含：
            - answer：Agent 的最终回答
            - steps：推理过程的文字记录列表

        设计思路：
            方法签名刻意保持最简——只有一个字符串参数，没有 context、session_id 等额外参数。
            这使得接口调用极其简单，同时 Agent 内部可以通过 WorkingMemory 管理上下文。
        """


"""
🤔 思考题（结合 base.py 和 executor.py 一起思考）：

1. 接口扩展问题：
   现在 run() 只接收一个 query 字符串，如果要支持多模态（传图片给 Agent），
   你会怎么修改这个接口？直接在 run() 加参数，还是新建一个 Protocol？
   两种方式各有什么取舍？

2. AgentResponse 的 steps 字段：
   steps 是 list[str]（文字列表），只是记录了"步骤的文字描述"。
   如果你要做"Agent 行为分析"（统计每种工具被调用了多少次），
   steps 的设计够用吗？你会怎么改造 steps 的数据结构？

3. 单方法接口的局限：
   Agent Protocol 只有 run() 一个方法，没有 cancel()、pause()、get_status() 等。
   在什么场景下你会需要这些方法？添加这些方法对现有代码有多大影响？

4. 流式输出的挑战：
   现在 run() 等所有步骤完成才返回最终答案（阻塞式）。
   如果要支持"打字机效果"（逐步输出中间结果），
   需要怎么修改 AgentResponse 和 run() 的签名？
   提示：考虑 AsyncGenerator[AgentResponse, None] 类型。

5. （选做）多 Agent 协作：
   如果要实现"Agent A 的输出作为 Agent B 的输入"（Pipeline 模式），
   现有的 AgentResponse 结构满足需求吗？
   你会给 AgentResponse 添加什么字段来支持 Agent 间的信息传递？
"""
