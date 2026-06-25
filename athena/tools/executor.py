"""
📦 模块名称：工具执行器（Tool Executor）
📍 架构位置：工具层调度器（Tool Layer Orchestrator）—— 位于 Agent 决策和 ToolRegistry 之间：
              [ReActAgent] → ToolCall → 【ToolExecutor】 → [ToolRegistry.invoke()]
🎯 核心作用：在“直接调用工具”之外增加生产级恢复能力：超时控制、失败重试、参数修正、降级工具。
🔗 依赖关系：
    - 依赖：ToolRegistry、ToolCall、ToolResult
    - 被依赖：未来 ReActAgent 可通过它替代直接 registry.invoke()
💡 设计思路：
    ToolRegistry 负责“有哪些工具、怎么调用”；ToolExecutor 负责“调用失败后怎么办”。
    这是一层恢复策略，不改变工具注册机制，也不侵入每个工具函数。

    三级错误恢复机制：
    ① retry：同一调用短暂失败时重试
    ② argument repair：删除多余参数、单参数错名时自动映射到必填参数
    ③ fallback：主工具失败后切到配置好的降级工具
📚 学习重点：
    1. 为什么把恢复逻辑从 ToolRegistry 中拆出来，符合单一职责原则
    2. asyncio.wait_for 如何给异步工具统一加超时
    3. 参数修正为什么只做保守修复，避免“猜太多”导致错误调用
    4. fallback_tools 为什么用配置映射，而不是写死 if/else
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from athena.tools.registry import ToolCall, ToolRegistry, ToolResult
from athena.types import JSONValue


@dataclass(frozen=True)
class ToolExecutionPolicy:
    """
    工具执行策略配置。

    字段说明：
        timeout_seconds: 每次工具调用最大等待时间
        max_retries:    失败后的最大重试次数
        fallback_tools: 主工具名 → 降级工具名的映射
    """

    timeout_seconds: float = 10.0
    max_retries: int = 1
    fallback_tools: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")


class ToolExecutor:
    """
    带恢复能力的工具调用门面。

    功能说明：
        对外仍然接收 ToolCall、返回 ToolResult，因此可以平滑替换原有 registry.invoke() 调用点。
        内部按“参数修正 → 带超时调用 → 重试 → 降级”的顺序执行。

    # 🎯 面试考点：为什么失败也返回 ToolResult，而不是抛异常？
    # 答：Agent 需要把失败作为 Observation 反馈给 LLM，让它调整计划；异常会打断 ReAct 循环。
    """

    def __init__(self, registry: ToolRegistry, policy: ToolExecutionPolicy | None = None) -> None:
        self.registry = registry
        self.policy = policy or ToolExecutionPolicy()

    async def execute(self, call: ToolCall) -> ToolResult:
        """执行一次工具调用，并应用三级恢复策略。"""
        self._validate_call(call)
        last_result: ToolResult | None = None
        repaired_call = self._repair_arguments(call)
        for _ in range(self.policy.max_retries + 1):
            last_result = await self._invoke_with_timeout(repaired_call)
            if last_result.success:
                return last_result

        fallback_name = self.policy.fallback_tools.get(call.name)
        if fallback_name:
            fallback_call = ToolCall(name=fallback_name, arguments=repaired_call.arguments)
            fallback_result = await self._invoke_with_timeout(self._repair_arguments(fallback_call))
            if fallback_result.success:
                return fallback_result

        return last_result or ToolResult(success=False, content="", error="tool execution failed")

    def _repair_arguments(self, call: ToolCall) -> ToolCall:
        tool = self.registry.tools.get(call.name)
        if tool is None:
            return call
        allowed = set(tool.parameters)
        repaired: dict[str, JSONValue] = {key: value for key, value in call.arguments.items() if key in allowed}
        for parameter in tool.required_parameters:
            if parameter not in repaired and len(call.arguments) == 1:
                repaired[parameter] = next(iter(call.arguments.values()))
        return ToolCall(name=call.name, arguments=repaired)

    async def _invoke_with_timeout(self, call: ToolCall) -> ToolResult:
        try:
            return await asyncio.wait_for(self.registry.invoke(call), timeout=self.policy.timeout_seconds)
        except TimeoutError:
            return ToolResult(success=False, content="", error="tool execution timed out")

    def _validate_call(self, call: ToolCall) -> None:
        if not isinstance(call, ToolCall):
            raise ValueError("call must be a ToolCall")
        if not call.name.strip():
            raise ValueError("tool name must be non-empty")