"""
📦 模块名称：工作记忆（Working Memory）
📍 架构位置：记忆层（Memory Layer）—— 夹在 Agent 执行器和提示词组装器之间：
           [ReActAgent.run()] → 写入 → 【本文件 WorkingMemory】 → 渲染 → [ContextAssembler]
🎯 核心作用：为 Agent 提供"短期记忆"——记录本轮对话的消息历史，
           并在消息太多超出 Token 预算时，智能淘汰最不重要的旧消息
🔗 依赖关系：
   - 依赖：pydantic（数据模型和字段校验）
   - 被依赖：
     * athena/agent/executor.py   → 每步调用 add_message() 写入用户/AI/工具消息
     * athena/prompt/assembler.py → 调用 render() 把记忆渲染进提示词
💡 设计思路：
   "重要性加权滑动窗口"（Importance-Weighted Sliding Window）：
   不同于简单的"先进先出（FIFO）队列"，这里每条消息都有一个重要性分数（importance）。
   当总 Token 超出预算时，优先删除重要性最低的旧消息，而不是单纯删最早的那条。
   这样能保留关键信息（如用户的原始问题），即使它们比较早。
   
   设计上有意保持简单——Token 估算用"字符数 ÷ 4"的粗略方法，
   精确 tokenizer 属于二阶段增强，MVP 阶段这样已够用。
📚 学习重点：
   1. importance（重要性）字段的作用——基于优先级的淘汰策略 vs 简单 FIFO
   2. _prune_if_needed() 的核心算法：min() + key 参数的妙用
   3. render() 如何把消息列表变成提示词文字（格式很重要）
   4. recent_messages() 返回 tuple 而不是 list 的原因
   5. Token 估算的"4字符≈1Token"经验公式及其局限性
"""

from __future__ import annotations  # 💡 学习提示：支持类型注解前向引用，全项目统一风格

import logging
import math
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

logger = logging.getLogger(__name__)  # 💡 学习提示：日志显示 "athena.memory.working"，方便追踪哪些消息被剪枝了


# ============================================================
# 📌 数据模型层：单条消息的结构定义
# ============================================================


class Message(BaseModel):
    """
    工作记忆中存储的一条消息。

    功能说明：
        代表对话中的一条记录——可以是用户说的话、AI 的回复、或工具返回的结果。
        三个字段分别回答："谁说的"、"说了什么"、"这条记录有多重要"。

    字段说明：
        role:       消息角色，标识这条消息来自谁：
                    - "user"      → 用户输入的问题或指令
                    - "assistant" → AI 的回复
                    - "tool"      → 工具调用的结果（如计算器返回值）

        content:    消息的具体文字内容
                    示例："帮我查一下北京天气" / "北京今天晴天，28°C"

        importance: 重要性分数，决定内存不足时被淘汰的优先级。
                    分数越高 → 越不容易被删除（越被保护）。
                    默认 1.0（普通级别）；
                    在 executor.py 中：用户原始问题和 AI 最终回答被设为 2.0（重要）；
                    超出步数时的兜底回复被设为 1.0（可淘汰）。

    设计思路：
        把 importance 直接放在 Message 里，而不是在 WorkingMemory 中维护一个
        单独的 {消息 → 重要性} 字典。好处是数据局部性——消息和它的重要性分数绑定在一起，
        不会出现"消息删了但字典里还有残留"的不一致状态（数据一致性问题）。

    使用示例：
        msg = Message(role="user", content="你好", importance=2.0)
        msg2 = Message(role="assistant", content="有什么可以帮你？")
        print(msg2.importance)  # 1.0（默认值）
    """

    role: str
    content: str
    # 💡 学习提示：importance 默认 1.0 是"普通"级别。
    # 调用方可以传 2.0 表示"重要，尽量保留"，传 0.5 表示"可随时丢弃"。
    # 这个浮点数设计比布尔值（重要/不重要）更灵活，支持细粒度的优先级排序。
    importance: float = 1.0
    compressed: bool = False


# ============================================================
# 📌 核心实现层：带 Token 预算的滑动窗口记忆管理器
# ============================================================


class WorkingMemory(BaseModel):
    """
    带 Token 预算的短期工作记忆。

    功能说明：
        像人类的"工作记忆"一样——容量有限，装不下就自动清理最不重要的内容。
        在 Agent 的每次对话中，所有消息都存在这里，提示词组装时从这里取出渲染。

    字段说明：
        max_tokens: 记忆的最大 Token 容量（默认 8000）。
                    超出后触发自动剪枝，删除重要性最低的旧消息。
                    注意：这是"估算 Token"而非精确 Token——用字符数 ÷ 4 粗略计算。

        messages:   所有保留的消息列表，按时间顺序排列（最早的在前）。

    设计思路：
        为什么用 max_tokens 而不是 max_messages（最多保留几条）？
        因为 LLM 的实际限制是上下文窗口的 Token 数，而不是消息条数。
        一条长消息（1000 字）比 10 条短消息（各 10 字）消耗更多 Token，
        用 max_tokens 更贴近 LLM 的真实限制。

    # 🎯 面试考点：为什么这里用 Pydantic BaseModel 而不是普通 dataclass？
    # 答：① Pydantic 的 PositiveInt 可以校验 max_tokens 不能为 0 或负数
    #     ② Field(default_factory=list) 正确处理可变默认值（避免共享列表的经典坑）
    #     ③ 与整个 Athena 项目的配置风格统一，可以从 config.yaml 直接构造

    使用示例：
        memory = WorkingMemory(max_tokens=4000)  # 限制 4000 Token
        memory.add_message("user", "北京天气怎么样？", importance=2.0)
        memory.add_message("assistant", "北京今天晴天")
        print(memory.render())
        # user: 北京天气怎么样？
        # assistant: 北京今天晴天
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # 💡 学习提示：PositiveInt 是 Pydantic 的约束类型，确保 max_tokens > 0，
    # 如果传入 0 或 -1 会在创建时立刻报错，而不是等到真正使用时才发现
    max_tokens: PositiveInt = 8000
    compression_threshold: float = 0.85
    summarizer: Callable[[Sequence[Message]], str] | None = Field(default=None, exclude=True)
    # 💡 学习提示：Field(default_factory=list) 避免所有实例共享同一个列表对象，
    # 这是 Python 可变默认值的经典陷阱，Pydantic 通过 factory 彻底规避
    messages: list[Message] = Field(default_factory=list)

    def add_message(self, role: str, content: str, importance: float | None = None) -> None:
        """
        向记忆中添加一条新消息，并在必要时自动剪枝。

        功能说明：
            这是 WorkingMemory 最常用的方法。
            先追加新消息，然后检查是否超出 Token 预算，超出则自动删除最不重要的旧消息。
            "先加后剪"的顺序确保了刚加入的最新消息不会被立刻剪掉。

        参数说明：
            role:       消息角色（"user" / "assistant" / "tool"）
            content:    消息文字内容
            importance: 重要性分数（默认 1.0，越高越不容易被删除）

        返回值：
            无（None），就地修改 self.messages

        设计思路：
            "先添加后剪枝"（Append-then-Prune）模式：
            新消息先无条件加入，再由 _prune_if_needed() 决定要不要删旧消息。
            这保证了最新消息（刚加的那条）永远存在于记忆中，
            因为 _prune_if_needed() 的设计排除了最后一条消息被删除的可能。

        使用示例：
            memory = WorkingMemory()
            memory.add_message("user", "你好", importance=2.0)
            memory.add_message("assistant", "你好！有什么可以帮你？")
            # 此时 len(memory.messages) == 2
        """
        role = self._validate_role(role)
        content = self._validate_content(content)
        scored_importance = self._score_importance(role, content, importance)
        self.messages.append(Message(role=role, content=content, importance=scored_importance))
        # 💡 学习提示：每次加消息后都检查一次是否需要剪枝，
        # 而不是等到读取记忆时才检查。"写时剪枝"确保记忆始终在预算内，
        # 不会在读取时产生意外的延迟。
        self._prune_if_needed()

    def recent_messages(self) -> Sequence[Message]:
        """
        返回当前保留的所有消息（按时间顺序）。

        功能说明：
            提供给外部代码的"只读视图"——用于查看当前记忆内容，不能直接修改。

        返回值：
            Message 的不可变序列（tuple），按时间先后排列（最早的在最前面）

        设计思路：
            返回 tuple 而不是直接返回 self.messages（list）的原因：
            "防御性拷贝"——如果返回原始 list，调用方可以对它做 .append() / .pop()，
            绕过 add_message() 的剪枝逻辑，直接破坏内存管理。
            返回 tuple 让调用方只能"看"不能"改"，保护内部状态的一致性。

        # 🎯 面试考点：为什么返回 tuple 而不是 list？
        # 答：元组（tuple）是不可变的，返回它相当于"只读快照"。
        # 如果返回 list，外部代码可以随意增删消息，绕过 _prune_if_needed 的保护。
        # 这是"封装性"（Encapsulation）原则的体现：
        # 只暴露必要的操作接口，隐藏内部实现细节。

        使用示例：
            msgs = memory.recent_messages()
            for msg in msgs:
                print(f"{msg.role}: {msg.content}")
            # msgs.append(...)  ← 这会报错！tuple 不支持追加
        """
        # 💡 学习提示：tuple(self.messages) 创建了一个新的 tuple 对象，
        # 修改它不会影响 self.messages，调用方拿到的是一个安全的只读视图
        return tuple(self.messages)

    def render(self) -> str:
        """
        把记忆中的所有消息渲染成提示词文字（供 ContextAssembler 使用）。

        功能说明：
            将消息列表转换成适合放入提示词的纯文字格式。
            ContextAssembler.build_prompt() 调用此方法，把结果填入 {memory} 槽位。

        返回值：
            多行字符串，每行格式为 "role: content"

        设计思路：
            格式选择 "role: content" 而不是 JSON 或 Markdown，原因是：
            ① 简洁，不引入额外符号
            ② LLM 能轻松理解"user: xxx"、"assistant: xxx"的对话格式
            ③ 避免 JSON 字符串里的特殊符号（引号、大括号）与提示词其他部分冲突

        使用示例：
            memory = WorkingMemory()
            memory.add_message("user", "你好")
            memory.add_message("assistant", "你好！有什么可以帮你？")
            print(memory.render())
            # 输出：
            # user: 你好
            # assistant: 你好！有什么可以帮你？
        """
        # 💡 学习提示：生成器表达式 + "\n".join() 是 Python 里拼接多行字符串的惯用写法，
        # 比循环 += 字符串更高效（避免重复创建中间字符串对象）
        return "\n".join(
            f"{message.role}: {message.content}" for message in self.messages
        )

    def _prune_if_needed(self) -> None:
        """
        当 Token 估算值超出预算时，反复删除重要性最低的旧消息。

        功能说明：
            每次 add_message() 后自动调用，是工作记忆的"清洁工"。
            不做一次性批量清理，而是每次只删一条，循环直到恢复预算或只剩一条为止。

        返回值：
            无（None），就地修改 self.messages

        设计思路：
            "最低重要性优先淘汰"（Least-Important-First Eviction），
            类似操作系统缓存管理中的 LRU（最近最少使用）策略，
            但这里用的是重要性分数，而不是"最久未访问"。
        """
        """
        🔍 原理讲解：_prune_if_needed() 的算法步骤

        假设 messages 列表是（按时间顺序）：
        [msg_A(importance=2.0), msg_B(importance=1.0), msg_C(importance=0.5), msg_D(最新，importance=1.0)]

        超出 Token 预算时：
        第 1 步：range(len-1) = range(3) = [0, 1, 2]（排除最后一条 msg_D，保护最新消息）
                min() 找到 index=2（msg_C，importance=0.5 最低）
                删除 msg_C

        此时还超出预算时继续：
        第 2 步：range(len-1) = range(2) = [0, 1]（排除 msg_D）
                min() 找到 index=1（msg_B，importance=1.0）
                删除 msg_B

        直到：① Token 估算值 ≤ max_tokens，或 ② 只剩 1 条消息为止

        关键规则：
        - range(len-1) 永远排除最后一条（刚加入的最新消息），确保它不会被立刻删除
        - len > 1 保证至少保留 1 条消息，防止死循环或完全清空记忆
        """
        while self._estimated_tokens() > int(self.max_tokens * self.compression_threshold) and len(self.messages) > 1:
            if self._compress_one_message():
                continue
            break

        while self._estimated_tokens() > self.max_tokens and len(self.messages) > 1:
            # 💡 学习提示：range(len(self.messages) - 1) 生成索引 [0, 1, ..., n-2]，
            # 有意排除最后一条消息（index = n-1），即刚刚加入的最新消息永远不会被删除。
            # 这保证了 add_message() 的"先加后剪"逻辑的正确性。
            removable_index = min(
                range(len(self.messages) - 1),
                # 💡 学习提示：min(..., key=lambda index: ...) 是 Python 的"最小值查找"惯用写法。
                # key 函数把"候选索引"转换成"比较依据"（这里是消息的 importance 值）。
                # min() 找到让 key 返回值最小的那个索引，即重要性最低的消息的位置。
                # 等价于：找出 messages[0..n-2] 中 importance 最小的那条的下标。
                key=lambda index: self.messages[index].importance,
            )
            removed = self.messages.pop(removable_index)
            # 💡 学习提示：用 DEBUG 级别记录日志，而不是 INFO 或 WARNING，
            # 因为剪枝是预期行为，不是警告；只在调试时才需要关注哪些消息被删了
            logger.debug("Pruned working-memory message role=%s", removed.role)

    def _compress_one_message(self) -> bool:
        candidates = [
            index
            for index, message in enumerate(self.messages[:-1])
            if not message.compressed and self._estimate_message_tokens(message) > 8
        ]
        if not candidates:
            return False

        target_index = min(
            candidates,
            key=lambda index: (self.messages[index].importance, index),
        )
        target = self.messages[target_index]
        summary = self._summarize_messages([target])
        if self._estimate_text_tokens(summary) >= self._estimate_message_tokens(target):
            return False

        self.messages[target_index] = Message(
            role=target.role,
            content=summary,
            importance=max(target.importance, 0.8),
            compressed=True,
        )
        logger.debug("Compressed working-memory message role=%s", target.role)
        return True

    def _estimated_tokens(self) -> int:
        """
        粗略估算当前所有消息占用的 Token 数量。

        功能说明：
            给 _prune_if_needed() 提供"何时开始剪枝"的判断依据。
            不是精确计算，而是用"字符数 ÷ 4"的经验公式快速估算。

        返回值：
            估算的总 Token 数（整数）

        设计思路：
            "够用就好"（Good Enough）原则：
            精确 tokenization 需要加载 tiktoken 等专用库，增加依赖和延迟。
            英文约 4 字符 = 1 Token（经验值，OpenAI 的参考数据）；
            中文约 1-2 字符 = 1 Token（但这个公式对中文高估了 Token 用量，是安全的偏差）。
            用粗略估算判断剪枝时机，偏差在可接受范围内。

        # ⚡ 优化建议：如果要提高精度，可以集成 tiktoken 库：
        # import tiktoken
        # enc = tiktoken.encoding_for_model("gpt-4o")
        # return sum(len(enc.encode(m.content)) for m in self.messages)
        # 代价是：需要安装 tiktoken，首次调用会下载 vocab 文件（约几 MB）。
        """
        """
        🔍 原理讲解：为什么用 len(content) // 4 估算 Token？

        Token 是 LLM 处理文字的基本单位，大致规律：
        - 英文："hello world" = 2 tokens（约 4 字符/token）
        - 中文："你好世界" = 4 tokens（约 1-2 字符/token，中文每个字接近 1 token）
        - 数字："1234567890" = 3 tokens（约 3-4 字符/token）

        用 // 4 对所有内容统一估算，对中文来说是低估了 Token 数（实际用更多），
        这意味着允许存更多消息，是"宽松安全"的方向——真正超限时 LLM 会报错，
        而不是过早删除有用的消息。

        max(1, ...) 确保哪怕是空字符串消息（len=0），也至少计 1 个 Token，
        避免空消息占用"零成本"导致无限积累空消息。
        """
        # 💡 学习提示：sum() + 生成器表达式比 for 循环累加更 Pythonic，
        # 而且性能稍好（sum 在 C 层实现，不需要每次回到 Python 层累加）
        return sum(self._estimate_message_tokens(message) for message in self.messages)
        # 💡 学习提示：max(1, ...) 确保即使 content 是空字符串（len=0），
        # 每条消息也至少贡献 1 个估算 Token，防止"零长度消息无限堆积"

    def _estimate_message_tokens(self, message: Message) -> int:
        return self._estimate_text_tokens(message.content)

    def _estimate_text_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def _summarize_messages(self, messages: Sequence[Message]) -> str:
        if self.summarizer is not None:
            summary = self.summarizer(messages).strip()
            if summary:
                return f"[compressed] {summary}"
        combined = " ".join(message.content.strip() for message in messages if message.content.strip())
        if len(combined) <= 120:
            return f"[compressed] {combined}"
        return f"[compressed] {combined[:117].rstrip()}..."

    def _score_importance(self, role: str, content: str, explicit_importance: float | None) -> float:
        if explicit_importance is not None:
            if not math.isfinite(explicit_importance) or explicit_importance < 0:
                raise ValueError("importance must be a non-negative finite number")
            return explicit_importance

        score = {"user": 2.0, "assistant": 1.4, "tool": 0.8}.get(role, 1.0)
        lowered = content.lower()
        important_keywords = ("must", "重要", "偏好", "记住", "需求", "error", "exception", "failed")
        if any(keyword in lowered for keyword in important_keywords):
            score += 0.6
        if len(content) > 800:
            score -= 0.2
        return max(0.1, score)

    def _validate_role(self, role: str) -> str:
        if not isinstance(role, str) or not role.strip():
            raise ValueError("role must be a non-empty string")
        normalized = role.strip().lower()
        if normalized not in {"user", "assistant", "tool", "system"}:
            raise ValueError("role must be one of: user, assistant, tool, system")
        return normalized

    def _validate_content(self, content: str) -> str:
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        return content


"""
🤔 思考题（结合 WorkingMemory 的设计深入思考）：

1. importance 分数的赋值策略：
   目前调用方手动传 importance=2.0 给重要消息，这需要每个调用点都记住这个约定。
   如果未来有 10 个地方调用 add_message()，有的忘记传 importance 怎么办？
   你会怎么设计，让重要性赋值更"自动"、更不容易出错？
   提示：考虑根据 role 自动设置默认 importance（如 user 消息自动设 2.0）。

2. 剪枝策略的局限：
   当前策略是"删最不重要的那一条"。如果有 5 条消息，其中 4 条 importance=2.0，
   1 条 importance=1.0，每次都只删那 1 条。删完后若还超出预算，
   接下来会发生什么？你觉得这个行为合理吗？

3. render() 格式的影响：
   render() 输出 "user: content\nassistant: content" 格式。
   如果换成 Markdown 格式（**User**: content），LLM 会表现不同吗？
   格式选择对 AI 的理解有多大影响？你会怎么测试这个差异？

4. 多轮对话的上下文完整性：
   假设用户和 AI 进行了 100 轮对话，WorkingMemory 一直在剪枝。
   用户在第 100 轮问"你还记得我第 1 轮说的 XXX 吗？"
   Agent 会怎么回答？这是 WorkingMemory 的根本局限，你有什么解决思路？
   提示：想想"长期记忆"（VectorStore）和"短期记忆"（WorkingMemory）如何协作。

5. （选做）线程安全问题：
   如果两个并发请求同时操作同一个 WorkingMemory 实例，
   _prune_if_needed() 里的 while 循环可能产生竞争条件（race condition）。
   在 Python 的 asyncio 环境下，这个问题实际会发生吗？
   提示：asyncio 是单线程的，思考"协程切换点"在哪里。
"""
