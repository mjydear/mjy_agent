"""
📦 模块名称：ReAct Agent 执行器（ReAct Execution Loop）
📍 架构位置：Agent 层核心 —— 整个项目最重要的文件，实现了 AI Agent 的大脑逻辑
           [CLI 层] → [本文件 ReActAgent] → [LLM / 工具 / 记忆 / 提示词]
🎯 核心作用：实现 ReAct（Reasoning + Acting）循环——让 AI 能够「先思考、再行动、再观察」，
           反复迭代直到给出最终答案，而不是只能一次性回答问题
🔗 依赖关系：
   - 依赖：
     * athena.infra.llm       → 向大模型发送消息
     * athena.memory          → 读写对话历史（短期记忆）
     * athena.prompt          → 组装每轮发给 LLM 的提示词
     * athena.tools           → 调用计算器、查询等外部工具
     * athena.agent.base      → AgentResponse 数据结构
   - 被依赖：athena/cli/main.py（CLI 层创建并调用 ReActAgent）
💡 设计思路：
   ReAct = Reasoning（推理）+ Acting（行动），是目前最主流的 Agent 框架之一。
   核心思想：把一个复杂任务拆成「思考-行动-观察」的循环，每步只做一件事：
   ① Thought（思考）：AI 分析当前情况，决定下一步怎么做
   ② Action（行动）：调用某个工具，或者给出最终答案
   ③ Observation（观察）：把工具返回的结果告诉 AI，进入下一轮思考
   
   代码采用「数据类（dataclass）+ 依赖注入」模式，而非 Pydantic BaseModel，
   原因是 ReActAgent 需要持有复杂对象（如 LLMClient），不需要序列化。
📚 学习重点：
   1. ReAct 循环的完整流程（run() 方法里的 for 循环）
   2. scratchpad（草稿纸）是什么？为什么需要它？
   3. @dataclass(frozen=True) 在 ReActDecision 上的含义
   4. 异常处理的三层分级设计（末尾的 try/except 块）
   5. _parse_decision 中 JSON 解析 + 降级回退的健壮性设计
"""

from __future__ import annotations  # 💡 学习提示：全项目统一风格，支持类型注解前向引用

import json         # 💡 学习提示：解析 LLM 返回的 JSON 格式决策
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field  # 💡 学习提示：用 dataclass 而非 Pydantic，见类注释说明
from typing import cast

from athena.agent.base import AgentResponse    # Agent 的标准返回格式
from athena.exceptions import AgentError, AthenaError, ErrorCode
from athena.infra.llm import LLMClient, LLMMessage
from athena.memory import WorkingMemory        # 短期对话记忆
from athena.prompt import ContextAssembler     # 每步的提示词组装器
from athena.tools import ToolCall, ToolRegistry
from athena.types import JSONValue
logger = logging.getLogger(__name__)


# ============================================================
# 📌 数据模型：单步 LLM 决策的结构化表示
# ============================================================


@dataclass(frozen=True)
# 🎯 面试考点：为什么用 @dataclass(frozen=True) 而不是普通 class 或 Pydantic BaseModel？
# 答：① frozen=True 让对象不可变（创建后无法修改任何字段）。
#       不可变对象是"线程安全"的，也避免了意外修改决策对象导致的 bug。
#     ② @dataclass 比 Pydantic 更轻量——ReActDecision 只是内部临时数据，
#       不需要序列化/反序列化，不需要类型校验，dataclass 足够了。
#     ③ 相比普通 class，dataclass 自动生成 __init__、__repr__、__eq__，省去样板代码。
class ReActDecision:
    """
    单步 LLM 推理返回的结构化决策。

    功能说明：
        把 LLM 输出的 JSON 字符串解析成有名字的字段，方便后续代码按字段名访问，
        避免到处写 payload["thought"] 这样容易出错的字典访问。

    字段说明：
        thought:      AI 当前步骤的"思考过程"文字（用于日志和调试，不直接给用户看）
                      示例："用户问天气，我需要调用 weather 工具查询北京的天气"

        action:       AI 决定调用的工具名称。为 None 时表示不调用工具，直接给答案。
                      示例："calculator"、"web_search"、None

        action_input: 传给工具的参数字典。
                      示例：{"expression": "123 * 456"}、{"city": "北京"}

        final_answer: AI 给出的最终答案文字。只有不再需要调用工具时才有值。
                      当 final_answer 有值且 action 为 None 时，循环终止。

    设计思路：
        LLM 通常返回这样的 JSON：
        {
            "thought": "需要查天气",
            "action": "weather_tool",
            "action_input": {"city": "北京"},
            "final_answer": null
        }
        或者最终答案：
        {
            "thought": "已经有结果了",
            "action": null,
            "action_input": {},
            "final_answer": "北京今天晴天28°C"
        }
        ReActDecision 就是这个 JSON 的 Python 表示。
    """

    thought: str = ""
    action: str | None = None
    # 💡 学习提示：dataclass 的可变默认值也要用 field(default_factory=...)，和 Pydantic 同理
    action_input: dict[str, JSONValue] = field(default_factory=dict)
    final_answer: str | None = None


# ============================================================
# 📌 核心实现：ReAct Agent 执行器
# ============================================================

@dataclass
# 💡 学习提示：这里用 @dataclass 而不是 @dataclass(frozen=True)
# 原因：ReActAgent 持有 memory 等可变状态（对话历史会随时更新），
# 不能冻结。而 ReActDecision 是一次性的临时解析结果，冻结更安全。
class ReActAgent:
    """
    基于 ReAct 模式的 Agent 核心实现。

    功能说明：
        实现了"思考-行动-观察"循环（ReAct Loop），是整个 Agent 项目的大脑。
        每次用户提问，Agent 会反复思考、调用工具、整合结果，直到能给出最终答案。

    字段说明（依赖注入的组件）：
        llm_client:       调用大模型的客户端（发问、获取决策）
        prompt_assembler: 每一步把"问题+历史+工具列表+已有思考"组装成提示词
        tool_registry:    管理所有可用工具，负责按名字调用工具
        memory:           短期对话记忆，存储历史消息，影响下一轮提示词
        max_steps:        最多执行几轮 Thought-Action-Observation，防止无限循环

    设计思路：
        依赖注入（Dependency Injection）模式——ReActAgent 不自己创建 LLMClient 等对象，
        而是在构造时由外部传入。好处：
        ① 测试时可以注入 mock 对象，不需要真正调用 OpenAI API
        ② 各组件可以独立替换（换模型、换工具库），不影响 Agent 逻辑
        ③ 配置和逻辑分离，更清晰

    使用示例：
        agent = ReActAgent(
            llm_client=LiteLLMClient(model="gpt-4o"),
            prompt_assembler=ContextAssembler(),
            tool_registry=registry,
            memory=WorkingMemory(),
            max_steps=6,
        )
        response = await agent.run("北京今天天气怎么样？")
        print(response.answer)
    """

    llm_client: LLMClient
    prompt_assembler: ContextAssembler
    tool_registry: ToolRegistry
    memory: WorkingMemory
    max_steps: int = 6  # 💡 学习提示：默认 6 步，足够处理需要 2-3 次工具调用的任务

    def __post_init__(self) -> None:
        """
        dataclass 的构造后验证钩子（Constructor Post-Validation Hook）。

        功能说明：
            @dataclass 生成的 __init__ 只会赋值，不会做逻辑校验。
            __post_init__ 会在 __init__ 之后自动调用，是 dataclass 约定的验证时机。

        设计思路：
            "快速失败"原则——在对象创建时立刻检查配置合法性，
            而不是等到真正调 run() 时才发现 max_steps 配错了。

        # 🎯 面试考点：Pydantic 怎么实现同样的构造后验证？
        # 答：Pydantic 用 @model_validator(mode='after') 装饰器，效果类似 __post_init__。
        # dataclass 用 __post_init__，Pydantic 用 validator，两者目的相同但写法不同。
        """
        if self.max_steps <= 0:
            raise AgentError(ErrorCode.AGENT_EXECUTION_FAILED, "max_steps must be positive")

    async def run(self, query: str) -> AgentResponse:
        """
        ReAct 循环的入口——处理一个用户查询，返回最终答案。

        功能说明：
            这是整个 Agent 项目最核心的方法。
            它实现了"思考-行动-观察"的迭代循环，直到 AI 给出最终答案或达到步数上限。

        参数说明：
            query: 用户输入的任务或问题
                   示例："帮我计算 1234 * 5678 等于多少"

        返回值：
            AgentResponse，包含：
            - answer：最终给用户的答案
            - steps：完整的推理链（Thought + Observation 记录）

        异常：
            AgentError: 查询为空、循环失败等情况

        设计思路：
            整个方法可以分为 4 个区域：
            ① 前置校验（输入检查）
            ② 初始化（记忆、草稿纸）
            ③ ReAct 主循环（核心算法）
            ④ 异常处理（三级分类）
        """
        """
        🔍 原理讲解：ReAct 循环是怎么工作的？

        ReAct 的核心思路是把复杂问题拆成多轮 LLM 调用：

        第一轮：
        输入：用户问题 + 工具列表
        输出：{"thought": "需要用计算器", "action": "calculator", "action_input": {"expr": "1234*5678"}}

        Agent 看到有 action，就调用 calculator 工具
        工具返回：Observation = "7006652"

        第二轮：
        输入：用户问题 + 工具列表 + 第一轮的思考/行动/观察（scratchpad）
        输出：{"thought": "已经有结果了", "action": null, "final_answer": "1234 * 5678 = 7006652"}

        Agent 看到 action=null + final_answer 有值，循环结束，返回答案。

        关键变量：
        - scratchpad（草稿纸）：记录"这次对话从开始到现在"的所有步骤，每轮都附加进提示词
        - steps：返回给调用方看的推理链记录（内容与 scratchpad 类似但格式不同）
        """
        if not query.strip():
            # 💡 学习提示：.strip() 去除首尾空白字符，防止用户只输入空格也能触发 Agent
            raise AgentError(ErrorCode.AGENT_EXECUTION_FAILED, "Query must not be empty")

        # 💡 学习提示：把用户消息存入记忆，importance=2.0 表示重要程度高。
        # 后续的提示词组装会从 memory 里取历史，这样 AI 能记住这次对话的上下文。
        self.memory.add_message("user", query, importance=2.0)

        scratchpad = ""   # 💡 学习提示：草稿纸——累积本次任务所有步骤的文字，每轮追加，始终送入 LLM
        steps: list[str] = []  # 💡 学习提示：给调用方看的推理链，与 scratchpad 内容相似但格式更简洁

        try:
            # 💡 学习提示：range(1, max_steps + 1) 从 1 开始是为了让日志里的步骤编号更直观
            # 比如 "Agent step 1" 而不是 "Agent step 0"
            for step_index in range(1, self.max_steps + 1):

                # --- 步骤 1：组装本轮提示词 ---
                # 💡 学习提示：每轮循环都重新组装提示词，加入最新的 scratchpad（累积的思考记录）
                # 这是让 AI "记住"前几步做了什么的关键机制
                prompt = self.prompt_assembler.build_prompt(
                    query=query,
                    memory=self.memory,
                    tools=self.tool_registry,
                    scratchpad=scratchpad,
                )

                # --- 步骤 2：调用 LLM 获取决策 ---
                # 💡 学习提示：每一步都是独立的 LLM 调用，上下文通过 scratchpad 传递，
                # 而不是依赖 LLM 的对话历史（这样更可控，避免 Token 超限）
                response = await self.llm_client.complete([LLMMessage(role="user", content=prompt)])

                # --- 步骤 3：解析 LLM 的 JSON 决策 ---
                decision = self._parse_decision(response.content)
                logger.info(
                    "Agent step %s thought=%s action=%s",
                    step_index, decision.thought, decision.action
                )
                steps.append(f"Thought: {decision.thought}")  # 记录思考过程到返回值

                # --- 步骤 4：判断是否终止循环 ---

                # 情况 A：有最终答案且无行动 → 正常结束
                if decision.final_answer and decision.action is None:
                    # 💡 学习提示：把 AI 的最终回答也存入记忆，
                    # 下一次用户提问时，AI 能看到这次的问答历史
                    self.memory.add_message("assistant", decision.final_answer, importance=2.0)
                    return AgentResponse(answer=decision.final_answer, steps=steps)

                # 情况 B：没有行动也没有最终答案 → 兜底结束（AI 输出了纯文字但没有 JSON 的 action 字段）
                if decision.action is None:
                    # 💡 学习提示：优先用 final_answer，没有则直接用原始回复内容（response.content）
                    # 这是 _parse_decision 降级处理后的兜底路径
                    answer = decision.final_answer or response.content
                    self.memory.add_message("assistant", answer, importance=2.0)
                    return AgentResponse(answer=answer, steps=steps)

                # 情况 C：有行动 → 调用工具，获取观察结果，继续循环

                # --- 步骤 5：修复工具参数（容错处理）---
                tool_arguments = self._repair_tool_arguments(
                    action=decision.action,
                    arguments=decision.action_input,
                    query=query,
                )

                # --- 步骤 6：执行工具调用 ---
                tool_result = await self.tool_registry.invoke(
                    ToolCall(name=decision.action, arguments=tool_arguments)
                )
                # 💡 学习提示：工具可能成功也可能失败。
                # 成功时用工具返回的内容作为"观察"，失败时把错误信息作为"观察"传给 AI，
                # 让 AI 知道"这个工具失败了，我需要换个办法"
                observation = tool_result.content if tool_result.success else str(tool_result.error)

                # echo 工具是特殊的"直通"工具——直接把文字原样返回，无需再问 LLM
                if tool_result.success and decision.action == "echo":
                    self.memory.add_message("assistant", observation, importance=2.0)
                    steps.append(f"Observation: {observation}")
                    return AgentResponse(answer=observation, steps=steps)

                # --- 步骤 7：把这一步的结果追加到草稿纸，进入下一轮 ---
                # 💡 学习提示：scratchpad 是这个循环的"短期工作记忆"。
                # 每步的 Thought/Action/Observation 都追加进来，
                # 下一轮 LLM 调用时能看到所有历史步骤，从而连贯地推理。
                scratchpad += (
                    f"\nStep {step_index}\n"
                    f"Thought: {decision.thought}\n"
                    f"Action: {decision.action}\n"
                    f"Observation: {observation}\n"
                )
                steps.append(f"Observation: {observation}")

            # --- 超出最大步数，给出兜底回复 ---
            # 💡 学习提示：importance=1.0 比正常答案低，表示这是一个"不太重要"的兜底信息，
            # 记忆系统可以在空间不足时优先丢弃低重要性的记录
            fallback = "I reached the maximum reasoning steps before producing a final answer."
            self.memory.add_message("assistant", fallback, importance=1.0)
            return AgentResponse(answer=fallback, steps=steps)

        except AgentError:
            # 💡 学习提示：三层异常处理的设计——从最具体到最通用：
            # ① AgentError：Athena 自己的 Agent 层异常，直接往上抛，不需要包装
            raise
        except AthenaError:
            # ② AthenaError：其他 Athena 层异常（如 LLMError、ToolError），也直接抛
            # 上层调用者（CLI）会统一处理这些有 error_code 的异常，显示友好错误信息
            raise
        except Exception as exc:
            # ③ 其他所有未知异常：包装成 AgentError，保证上层不会收到非 Athena 的裸异常
            # 💡 学习提示：logger.exception() 会自动把完整堆栈写入日志，方便事后排查
            logger.exception("Agent execution failed")
            raise AgentError(ErrorCode.AGENT_EXECUTION_FAILED, str(exc)) from exc

    def _parse_decision(self, raw_content: str) -> ReActDecision:
        """
        把 LLM 返回的原始字符串解析成结构化的 ReActDecision。

        功能说明：
            LLM 被要求返回 JSON 格式的决策，但它不总是乖乖听话。
            这个方法实现了"JSON 优先，纯文字兜底"的容错解析策略。

        参数说明：
            raw_content: LLM 返回的原始文字内容，理想情况下是 JSON 字符串，
                         但有时是纯文字（如 LLM 直接说"答案是42"）

        返回值：
            ReActDecision，包含解析出的 thought/action/action_input/final_answer

        设计思路：
            "降级处理"（Graceful Degradation）——系统遇到非预期输入时不直接崩溃，
            而是用最安全的兜底方式处理。就像翻译软件遇到生僻字，不报错而是显示原字。

        使用示例：
            # 正常情况（JSON 输入）：
            decision = self._parse_decision('{"thought": "需要计算", "action": "calc", ...}')
            decision.action  # → "calc"

            # 兜底情况（纯文字输入）：
            decision = self._parse_decision("答案是42")
            decision.final_answer  # → "答案是42"
            decision.action        # → None
        """
        """
        🔍 原理讲解：为什么 LLM 有时不返回 JSON？

        提示词让 LLM "必须返回 JSON"，但实际上 LLM 可能：
        1. 直接回答（"答案是42"）—— 尤其是简单问题
        2. 返回 Markdown 包裹的 JSON（```json {...} ```）—— 格式不对
        3. 返回格式错误的 JSON（少引号、多逗号等）—— 生成错误

        这个方法用 try/except json.JSONDecodeError 来检测第 3 种情况，
        并把所有非 JSON 的情况统一降级为 final_answer = raw_content。

        这就是为什么 Agent 在 LLM 回复不规范时也能正常运行，而不是崩溃。
        """
        try:
            loaded = json.loads(raw_content)
            if not isinstance(loaded, Mapping):
                # 💡 学习提示：json.loads 成功但结果不是字典（如 JSON 数组 [1,2,3] 或数字 42）
                # 也当作纯文字处理，用 final_answer 包装
                return ReActDecision(final_answer=raw_content)
            payload = cast(Mapping[str, JSONValue], loaded)

            # 💡 学习提示：action_input 可能是字典，也可能是 null 或其他类型，需要防御
            action_input = payload.get("action_input", {})
            arguments: dict[str, JSONValue] = {}
            if isinstance(action_input, Mapping):
                arguments = dict(action_input)

            action_value = payload.get("action")
            answer_value = payload.get("final_answer")
            return ReActDecision(
                thought=str(payload.get("thought", "")),
                # 💡 学习提示：严格检查 action 必须是字符串类型，
                # 防止 LLM 返回 "action": null（JSON null → Python None）时被当成工具名
                action=str(action_value) if isinstance(action_value, str) else None,
                action_input=arguments,
                final_answer=str(answer_value) if isinstance(answer_value, str) else None,
            )
        except json.JSONDecodeError:
            # 💡 学习提示：LLM 返回了非 JSON 文字（最常见的兜底情况）
            # 记录 warning 而非 error，因为这是"预期中的非预期"情况，不是程序 bug
            logger.warning("LLM returned non-JSON content; using final-answer fallback")
            return ReActDecision(final_answer=raw_content)

    def _repair_tool_arguments(
        self,
        action: str,
        arguments: dict[str, JSONValue],
        query: str,
    ) -> dict[str, JSONValue]:
        """
        修复 LLM 漏传的工具参数（容错兜底）。

        功能说明：
            LLM 有时会忘记传某些必要参数。这个方法通过简单规则自动补全，
            避免工具因参数缺失而报错，提升 Agent 的鲁棒性。

        参数说明：
            action:    当前调用的工具名称
            arguments: LLM 提供的工具参数字典（可能不完整）
            query:     用户原始问题（作为修复时的数据来源）

        返回值：
            修复后的完整参数字典。如果无需修复，返回原始 arguments。

        设计思路：
            "宽容接受原则"（Be Liberal in What You Accept）——
            对 LLM 的输入保持一定容忍度，自动修复简单错误，而不是直接报错。
            只针对已知的常见问题做修复，不做过度猜测。

        # ⚡ 优化建议：目前只修复了 echo 工具的 text 参数。
        # 随着工具增多，可以改成从 ToolRegistry 读取每个工具的 required_parameters，
        # 对所有工具做通用的"用 query 填充缺失必填参数"逻辑，而不是硬编码工具名。

        使用示例：
            # LLM 调用 echo 但忘了传 text：
            args = self._repair_tool_arguments("echo", {}, "帮我重复这句话")
            # → {"text": "帮我重复这句话"}

            # 参数完整，原样返回：
            args = self._repair_tool_arguments("echo", {"text": "hello"}, "...")
            # → {"text": "hello"}
        """
        if action == "echo" and "text" not in arguments:
            # 💡 学习提示：创建新字典而不是直接修改 arguments，
            # 因为 arguments 来自 ReActDecision（frozen dataclass），
            # 修改原始对象可能引发意外的副作用（即使 frozen 只限制顶层赋值）
            repaired = dict(arguments)
            repaired["text"] = query
            return repaired
        return arguments


"""
🤔 思考题（结合整个 ReAct 循环深入思考）：

1. scratchpad 的 Token 限制问题：
   每轮循环都把所有历史步骤追加到 scratchpad，然后拼进提示词发给 LLM。
   如果任务需要 10+ 步，scratchpad 会越来越长，最终可能超过 LLM 的上下文窗口（Token 限制）。
   你有什么办法解决这个问题？
   提示：想想"滑动窗口"、"压缩摘要"两种思路各有什么取舍。

2. 并发安全性：
   WorkingMemory 和 scratchpad 是共享状态。
   如果同时有两个用户的请求调用同一个 ReActAgent 实例，会有什么问题？
   现有架构是否支持多用户并发？如果不支持，你会怎么改造？

3. 工具调用失败后的策略：
   当前代码把工具错误信息作为 Observation 继续告诉 AI（"这个工具失败了"）。
   这样做有什么问题？AI 可能陷入什么困境？
   你会加什么机制来处理"连续工具失败"的情况？

4. max_steps 耗尽的体验：
   现在达到最大步数时返回一句英文兜底语。
   这对中文用户不友好，而且用户不知道为什么 Agent 没有完成任务。
   你会如何改进这个兜底行为？可以考虑：让 AI 总结到目前为止的发现，再给出部分答案。

5. （选做）流式 ReAct：
   现在 run() 要等所有步骤完成才返回结果（比如 6 步全跑完才能看到答案）。
   如果要支持"每完成一步就向前端推送进度"（WebSocket 流式输出），
   run() 的返回类型需要怎么改？yield 关键字在这里有什么用？
"""
