"""
📦 模块名称：校验 Agent（Validator Agent）
📍 架构位置：多 Agent 工作流第三层，位于 Executor 输出和 WorkflowState 记录之间。
🎯 核心作用：检查每一步执行结果是否可信，必要时给出轻量修复。
🔗 依赖关系：依赖 WorkflowStep 和 WorkflowStepResult；被 WorkflowEngine 调用。
💡 设计思路：使用“验收员”角色，把执行和质量判断拆开，便于未来加入重试、人工审批或 LLM 评审。
📚 学习重点：理解 accepted 与 repaired_result 的区别。
"""

from __future__ import annotations

from dataclasses import dataclass

from athena.agent.workflow.base import WorkflowStep, WorkflowStepResult


@dataclass(frozen=True)
class ValidationResult:
    """
    校验 Agent 的校验结果。

    功能说明：表示某一步结果是否通过校验，以及是否提供修复结果。
    参数说明：
        accepted：是否接受原始结果。
        reason：接受或拒绝原因。
        repaired_result：可选修复结果。
    返回值：数据容器。
    设计思路：把“拒绝”和“修复”放在同一个结果里，让 Engine 可以统一处理。
    使用示例：ValidationResult(False, "empty", repaired_result=result)
    """

    accepted: bool
    reason: str
    repaired_result: WorkflowStepResult | None = None


class ValidatorAgent:
    """
    校验 Agent：校验步骤输出，必要时给出轻量修正。

    功能说明：判断 Executor 的输出是否可用。
    参数说明：无构造参数。
    返回值：validate() 返回 ValidationResult。
    设计思路：当前规则很简单：成功且输出非空即可接受；这是为了教学清晰和测试稳定。
    使用示例：validation = ValidatorAgent().validate(step, result)
    """

    def validate(
        self, step: WorkflowStep, result: WorkflowStepResult
    ) -> ValidationResult:
        """
        校验执行结果。

        功能说明：检查结果类型、成功标志和输出内容，失败时构造轻量修复结果。
        参数说明：
            step：当前步骤。
            result：Executor 的输出。
        返回值：ValidationResult。
        设计思路：先做最小可解释规则，后续可替换为 LLM-as-judge 或断言式验证。
        使用示例：validator.validate(step, result)

        🔍 原理讲解：
        输入：某一步的执行结果。
        处理过程：如果 success=True 且 output 非空，则接受；否则生成 Recovered step。
        输出：accepted=True 或带 repaired_result 的 ValidationResult。
        """
        if not isinstance(step, WorkflowStep):
            raise ValueError("step must be a WorkflowStep")
        if not isinstance(result, WorkflowStepResult):
            raise ValueError("result must be a WorkflowStepResult")
        if result.success and result.output.strip():
            return ValidationResult(accepted=True, reason="ok")
        # 💡 学习提示：这里的修复只是 Demo 级兜底，不代表真实业务已经修好；生产环境应触发重试或人工确认。
        repaired = WorkflowStepResult(
            step_id=step.step_id,
            success=True,
            output=f"Recovered step: {step.goal}",
            error=result.error,
        )
        return ValidationResult(
            accepted=False, reason="empty or failed output", repaired_result=repaired
        )


"""
🤔 思考题：

1. 如果输出内容不为空但事实错误，当前 Validator 能发现吗？为什么？
2. 如果要接 LLM-as-judge，你会把判断逻辑放在 validate() 里还是新建策略类？
3. repaired_result 是否应该被标记为人工修复/自动修复？
4. ⚡ 优化建议：未来可以增加 ValidationResult.confidence，表达校验结果的可信度。
"""
