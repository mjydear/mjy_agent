"""
📦 模块名称：规划 Agent（Planner Agent）
📍 架构位置：多 Agent 工作流第一层，位于用户任务和 WorkflowPlan 之间。
🎯 核心作用：把复杂任务拆成一组可执行步骤。
🔗 依赖关系：依赖 WorkflowPlan 和 WorkflowStep；被 WorkflowEngine 调用。
💡 设计思路：使用简单规则实现 MVP 规划器，先保证可测和可解释，再逐步替换成 LLM 规划。
📚 学习重点：看自然语言任务如何被转换为结构化计划。
"""

from __future__ import annotations

import json
import logging

from athena.agent.workflow.base import WorkflowPlan, WorkflowStep

logger = logging.getLogger(__name__)


class PlannerAgent:
    """
    规划 Agent：把复杂任务拆成结构化步骤。

    功能说明：读取用户任务文本，输出 WorkflowPlan。
    参数说明：llm_client 可选，注入后 aplan() 用 LLM 规划，否则规则拆分。
    返回值：plan()/aplan() 返回 WorkflowPlan。
    设计思路：LLM 规划更贴近真实语义，失败时降级规则拆分保证稳定可测。
    使用示例：PlannerAgent().plan("检查服务; 收集日志")
    """

    def __init__(self, llm_client: "object | None" = None) -> None:
        self.llm_client = llm_client

    async def aplan(self, task: str) -> WorkflowPlan:
        """
        用 LLM 把任务分解为步骤，失败时降级规则拆分。

        功能说明：请求 LLM 输出 JSON 步骤列表并解析成 WorkflowPlan。
        参数说明：task 是用户输入的复杂任务。
        返回值：WorkflowPlan。
        设计思路：LLM 缺失或输出不可解析时回退 plan()，保证工作流永不因规划失败中断。
        """
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be a non-empty string")
        if self.llm_client is None:
            return self.plan(task)
        try:
            return await self._llm_plan(task)
        except Exception as exc:
            logger.warning("LLM planning failed, falling back to rule-based: %s", exc)
            return self.plan(task)

    async def _llm_plan(self, task: str) -> WorkflowPlan:
        """调用 LLM 生成结构化步骤（内部方法）。"""
        from athena.infra.llm import LLMMessage

        prompt = (
            "你是任务规划器。把用户任务拆成有序的可执行步骤，"
            "只输出 JSON 数组，每个元素形如 "
            '{"goal": "步骤目标", "tool_hint": "工具名或null"}。\n'
            f"任务：{task.strip()}"
        )
        response = await self.llm_client.complete(  # type: ignore[union-attr]
            [LLMMessage(role="user", content=prompt)]
        )
        payload = json.loads(self._extract_json(response.content))
        steps = tuple(
            WorkflowStep(
                step_id=f"step-{index + 1}",
                goal=str(item["goal"]).strip(),
                tool_hint=self._normalize_hint(item.get("tool_hint")),
            )
            for index, item in enumerate(payload)
            if str(item.get("goal", "")).strip()
        )
        if not steps:
            raise ValueError("LLM returned no usable steps")
        return WorkflowPlan(task=task.strip(), steps=steps)

    @staticmethod
    def _extract_json(text: str) -> str:
        """从 LLM 回复中截取 JSON 数组（容忍 ```json 包裹）。"""
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("no JSON array found in LLM response")
        return text[start : end + 1]

    @staticmethod
    def _normalize_hint(value: "object | None") -> str | None:
        """把 LLM 给的 tool_hint 归一化：空/None/'null' → None。"""
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() in {"null", "none"}:
            return None
        return text

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
