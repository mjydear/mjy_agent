"""
📦 模块名称：执行 Agent（Executor Agent）
📍 架构位置：多 Agent 工作流第二层，位于 WorkflowStep 和 ToolExecutor 之间。
🎯 核心作用：执行 Planner 生成的步骤，必要时调用工具。
🔗 依赖关系：依赖 WorkflowStep、WorkflowStepResult、ToolRegistry、ToolExecutor；被 WorkflowEngine 调用。
💡 设计思路：使用适配器思想，把“步骤”转换成“工具调用”或普通执行结果。
📚 学习重点：看 tool_hint 如何把规划结果连接到工具系统。
"""

from __future__ import annotations

import logging

from athena.agent.workflow.base import WorkflowStep, WorkflowStepResult
from athena.tools import ToolCall, ToolExecutor, ToolRegistry

logger = logging.getLogger(__name__)


class ExecutorAgent:
    """
    执行 Agent：执行规划步骤，可复用工具注册中心。

    功能说明：接收一个 WorkflowStep，并返回 WorkflowStepResult。
    参数说明：
        tool_registry：工具注册中心，不传则创建空注册中心。
        tool_executor：工具执行器，不传则用 registry 创建默认执行器。
        llm_client：可选，无工具时用 LLM 生成步骤结果，否则返回占位文本。
    返回值：execute() 返回 WorkflowStepResult。
    设计思路：依赖注入让测试可以传入假工具，也让生产环境可以复用真实工具系统。
    使用示例：result = await ExecutorAgent(registry).execute(step)
    """

    def __init__(
        self,
        tool_registry: ToolRegistry | None = None,
        tool_executor: ToolExecutor | None = None,
        llm_client: "object | None" = None,
    ) -> None:
        self.tool_registry = tool_registry or ToolRegistry()
        self.tool_executor = tool_executor or ToolExecutor(self.tool_registry)
        self.llm_client = llm_client

    async def execute(self, step: WorkflowStep) -> WorkflowStepResult:
        """
        执行一个计划步骤。

        功能说明：优先调用匹配工具；无工具时用 LLM 产出结果，LLM 缺失/失败则占位文本。
        参数说明：step 是 Planner 输出的步骤。
        返回值：WorkflowStepResult。
        设计思路：工具优先保证可执行性，LLM 兜底提升无工具步骤的实用性。
        使用示例：await executor.execute(WorkflowStep("step-1", "检查 git", "git_status"))
        """
        if not isinstance(step, WorkflowStep):
            raise ValueError("step must be a WorkflowStep")
        if step.tool_hint and step.tool_hint in self.tool_registry.tools:
            # 💡 学习提示：只有工具真实注册时才调用，避免 Planner 猜错工具名导致系统崩溃。
            result = await self.tool_executor.execute(
                ToolCall(name=step.tool_hint, arguments={})
            )
            return WorkflowStepResult(
                step_id=step.step_id,
                success=result.success,
                output=result.content,
                error=result.error,
            )
        if self.llm_client is not None:
            output = await self._llm_execute(step)
            if output is not None:
                return WorkflowStepResult(
                    step_id=step.step_id, success=True, output=output
                )
        return WorkflowStepResult(
            step_id=step.step_id, success=True, output=f"Executed: {step.goal}"
        )

    async def _llm_execute(self, step: WorkflowStep) -> str | None:
        """用 LLM 执行无工具步骤，失败返回 None 交由占位文本兜底。"""
        try:
            from athena.infra.llm import LLMMessage

            response = await self.llm_client.complete(  # type: ignore[union-attr]
                [
                    LLMMessage(
                        role="user",
                        content=f"执行以下运维/任务步骤并给出简洁结果：{step.goal}",
                    )
                ]
            )
            text = (response.content or "").strip()
            return text or None
        except Exception as exc:
            logger.warning("LLM step execution failed, using placeholder: %s", exc)
            return None


"""
🤔 思考题：

1. 如果工具需要参数，Executor 应该从哪里拿参数？Planner、LLM 还是用户输入？
2. 当前没有 tool_hint 时直接返回 Executed，这适合 Demo；生产环境会有什么风险？
3. 为什么 Executor 不自己校验结果，而是交给 Validator？
4. ⚡ 优化建议：未来可以给 WorkflowStep 增加 arguments 字段，让工具调用更完整。
"""
