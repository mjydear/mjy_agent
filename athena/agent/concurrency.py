"""
📦 模块名称：Agent 并发任务队列（Agent Concurrency Queue）
📍 架构位置：Agent 执行层的调度组件，位于 ReActAgent 外侧，负责把多个用户任务排队并发执行。
🎯 核心作用：让同一个 Agent 能按优先级处理多个任务，同时限制最大并发数，避免资源被打爆。
🔗 依赖关系：依赖 ReActAgent 执行真实任务，依赖 AgentResponse 返回统一结果；可被 CLI、TUI、Web 服务或调度器调用。
💡 设计思路：使用“生产者-消费者队列”模式。submit() 像投递工单，run_next() 像工作人员取下一张工单执行。
📚 学习重点：重点看 PriorityQueue 如何排序任务、Semaphore 如何限制并发、wait_for 如何给任务加超时保护。
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field

from athena.agent.base import AgentResponse
from athena.agent.executor import ReActAgent


@dataclass(order=True)
class QueuedTask:
    """
    带优先级的 Agent 任务。

    功能说明：保存一次待执行的用户请求，以及它在队列里的排序信息。
    参数说明：
        priority：优先级，数字越小越先执行，类似医院急诊分诊。
        sequence：入队顺序，用来保证同优先级任务按先来后到执行。
        query：用户真正要 Agent 处理的问题。
        timeout_seconds：单个任务允许运行的最长时间。
    返回值：dataclass 本身不主动返回值，它只是一个结构化数据容器。
    设计思路：order=True 让 PriorityQueue 可以直接比较 QueuedTask；query 不参与比较，避免两个任务文本不同导致排序混乱。
    使用示例：QueuedTask(priority=1, sequence=0, query="检查服务", timeout_seconds=30)

    🎯 面试考点：为什么要 sequence？答案：只用 priority 时，同优先级任务的顺序不稳定；sequence 可以实现公平的 FIFO。
    """

    priority: int
    sequence: int
    query: str = field(
        compare=False
    )  # 💡 学习提示：任务内容不应该参与排序，否则排序逻辑会被字符串比较干扰。
    timeout_seconds: float = field(
        default=60.0, compare=False
    )  # 💡 学习提示：超时时间是执行策略，不是队列优先级的一部分。


class AgentTaskQueue:
    """
    基于 asyncio.PriorityQueue 的并发任务池。

    功能说明：管理多个待执行 Agent 任务，按优先级取任务，并限制同时运行的数量。
    参数说明：
        agent：真正负责回答问题的 ReActAgent 实例。
        max_concurrency：最大并发数，例如 3 表示最多 3 个任务同时执行。
    返回值：构造函数不返回值；run_next() 会返回 AgentResponse。
    设计思路：队列负责“先执行谁”，Semaphore 负责“最多同时执行几个”，两者职责分离更容易理解和测试。
    使用示例：
        queue = AgentTaskQueue(agent, max_concurrency=2)
        await queue.submit("检查日志", priority=5)
        response = await queue.run_next()
    """

    def __init__(self, agent: ReActAgent, max_concurrency: int = 3) -> None:
        """
        初始化并发队列。

        功能说明：保存 Agent、创建优先级队列、创建并发控制信号量。
        参数说明：
            agent：执行任务的 ReActAgent。
            max_concurrency：同一时间允许执行的最大任务数。
        返回值：None。
        设计思路：把并发控制放在队列层，而不是塞进 ReActAgent，避免污染 Agent 的核心推理逻辑。
        使用示例：AgentTaskQueue(agent, max_concurrency=3)
        """
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        self.agent = agent
        self.max_concurrency = max_concurrency
        self._sequence = (
            itertools.count()
        )  # 💡 学习提示：无限递增计数器比手动维护整数更不容易写错。
        self._queue: asyncio.PriorityQueue[QueuedTask] = (
            asyncio.PriorityQueue()
        )  # 💡 学习提示：PriorityQueue 内部会按 dataclass 的排序字段取最小值。
        self._semaphore = asyncio.Semaphore(
            max_concurrency
        )  # 💡 学习提示：Semaphore 像“停车位”，没有空位时新任务必须等待。

    async def submit(
        self, query: str, priority: int = 10, timeout_seconds: float = 60.0
    ) -> None:
        """
        提交一个待执行任务。

        功能说明：把用户请求包装成 QueuedTask，并放入优先级队列。
        参数说明：
            query：用户输入的问题。
            priority：任务优先级，数字越小越先执行。
            timeout_seconds：任务超时时间。
        返回值：None。
        设计思路：submit 只负责入队，不直接执行；这样调用方可以先批量提交，再由 worker 慢慢消费。
        使用示例：await queue.submit("重启失败原因", priority=1, timeout_seconds=20)
        """
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        # 💡 学习提示：sequence 保证同优先级任务先来先服务，避免低层队列出现“不稳定排序”。
        await self._queue.put(
            QueuedTask(
                priority=priority,
                sequence=next(self._sequence),
                query=query,
                timeout_seconds=timeout_seconds,
            )
        )

    async def run_next(self) -> AgentResponse:
        """
        执行队列中的下一个任务。

        功能说明：从队列取出最高优先级任务，调用 Agent 执行，并返回 AgentResponse。
        参数说明：无。
        返回值：AgentResponse，包含最终答案和执行步骤。
        设计思路：用 wait_for 包住 agent.run，可以防止某个任务卡死后占住并发资源。
        使用示例：response = await queue.run_next()

        🔍 原理讲解：
        这里的执行流程像“排号叫号”：
        输入：队列里有多个 QueuedTask
        处理过程：PriorityQueue 取出优先级最高的任务 → Semaphore 申请执行名额 → wait_for 限制执行时长
        输出：AgentResponse 或超时异常
        """
        task = await self._queue.get()
        async with self._semaphore:
            try:
                # 💡 学习提示：这里没有吞掉 TimeoutError，是为了让上层知道任务真的超时了，而不是假装成功。
                return await asyncio.wait_for(
                    self.agent.run(task.query), timeout=task.timeout_seconds
                )
            finally:
                # 💡 学习提示：task_done 必须放在 finally，保证成功、失败、超时都会通知队列“这个任务处理完了”。
                self._queue.task_done()


"""
🤔 思考题：

1. 如果要一次启动多个 worker 自动消费队列，你会在哪里增加 start_workers()？
2. 这里为什么用 Semaphore，而不是只靠 PriorityQueue 控制并发？
3. 如果某个高优先级任务一直大量进入，低优先级任务会不会长期得不到执行？你会怎么优化？
4. 如果要支持取消任务，需要给 QueuedTask 增加哪些字段？
5. ⚡ 优化建议：未来可以记录任务状态 pending/running/done/failed，因为现在队列只负责执行，不保留历史状态。
"""
