"""
📦 模块名称：规划 Agent（Planner Agent）
📍 架构位置：多 Agent 工作流第一层，位于用户任务和 WorkflowPlan 之间。
🎯 核心作用：把复杂任务拆成一组可执行步骤。
🔗 依赖关系：依赖 WorkflowPlan 和 WorkflowStep；被 WorkflowEngine 调用。
💡 设计思路：使用简单规则实现 MVP 规划器，先保证可测和可解释，再逐步替换成 LLM 规划。
📚 学习重点：看自然语言任务如何被转换为结构化计划。
"""

from __future__ import annotations

from athena.agent.workflow.base import WorkflowPlan, WorkflowStep


class PlannerAgent:
    """
    规划 Agent：把复杂任务拆成结构化步骤。

    功能说明：读取用户任务文本，输出 WorkflowPlan。
    参数说明：无构造参数。
    返回值：plan() 返回 WorkflowPlan。
    设计思路：先用分号拆分模拟任务规划，避免 MVP 阶段引入不稳定 LLM 规划。
    使用示例：PlannerAgent().plan("检查服务; 收集日志")
    """

    def plan(self, task: str) -> WorkflowPlan:
        """
        根据任务文本生成计划。

        功能说明：把任务按中英文分号拆成多个步骤，并为每步推测工具。
        参数说明：task 是用户输入的复杂任务。
        返回值：WorkflowPlan。
        设计思路：分号是最简单的人类可控任务边界，适合教学和 Demo。
        使用示例：plan = planner.plan("读文件; git 状态")

        🔍 原理讲解：
        输入："检查服务; 收集日志"
        处理过程：替换中文分号 → split → 去空格 → 生成 step-1、step-2
        输出：WorkflowPlan(task=..., steps=(...))
        """
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be a non-empty string")
        parts = [
            part.strip() for part in task.replace("；", ";").split(";") if part.strip()
        ]  # 💡 学习提示：兼容中文分号，降低中文用户输入导致规划失败的概率。
        if not parts:
            parts = [task.strip()]
        steps = tuple(
            WorkflowStep(
                step_id=f"step-{index + 1}", goal=part, tool_hint=self._infer_tool(part)
            )
            for index, part in enumerate(parts)
        )
        return WorkflowPlan(task=task.strip(), steps=steps)

    def _infer_tool(self, goal: str) -> str | None:
        """
        根据步骤目标推测可能使用的工具。

        功能说明：用关键词规则给步骤添加 tool_hint。
        参数说明：goal 是单个步骤目标。
        返回值：工具名或 None。
        设计思路：这是一个轻量路由器，先用规则保证结果可解释，未来可换成工具选择模型。
        使用示例：planner._infer_tool("查看 git 状态") 返回 "git_status"。
        """
        lowered = goal.lower()
        if "git" in lowered:
            return "git_status"
        if "file" in lowered or "文件" in goal:
            return "read_text_file"
        return None


"""
🤔 思考题：

1. 如果用户不用分号，而是输入一大段自然语言，当前 Planner 会如何表现？
2. 为什么 _infer_tool 只返回工具名，不直接执行工具？
3. 如果要接入 LLM 规划，你会保留 WorkflowPlan 这个输出格式吗？为什么？
4. ⚡ 优化建议：未来可以加入步骤依赖关系，例如 step-2 必须等 step-1 成功后执行。
"""
