"""工作流 LLM 规划/执行 + 降级测试。"""

from __future__ import annotations

import pytest

from athena.agent.workflow.executor_agent import ExecutorAgent
from athena.agent.workflow.planner_agent import PlannerAgent
from athena.agent.workflow.base import WorkflowStep
from athena.infra.llm import LLMResponse


class _StubLLM:
    """返回预设内容的假 LLM，用于验证真实路径。"""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0

    async def complete(self, messages):  # noqa: ANN001
        self.calls += 1
        return LLMResponse(content=self._content, model="stub")


class _BrokenLLM:
    """总是抛异常的假 LLM，用于验证降级路径。"""

    async def complete(self, messages):  # noqa: ANN001
        raise RuntimeError("llm down")


@pytest.mark.asyncio
async def test_planner_llm_decomposition() -> None:
    llm = _StubLLM(
        '[{"goal": "检查服务状态", "tool_hint": "git_status"}, '
        '{"goal": "汇总日志", "tool_hint": null}]'
    )
    plan = await PlannerAgent(llm_client=llm).aplan("排障")
    assert llm.calls == 1
    assert [s.goal for s in plan.steps] == ["检查服务状态", "汇总日志"]
    assert plan.steps[0].tool_hint == "git_status"
    assert plan.steps[1].tool_hint is None


@pytest.mark.asyncio
async def test_planner_falls_back_on_llm_error() -> None:
    plan = await PlannerAgent(llm_client=_BrokenLLM()).aplan("检查服务; 收集日志")
    # 降级到规则拆分：按分号切成两步
    assert len(plan.steps) == 2


@pytest.mark.asyncio
async def test_planner_without_llm_uses_rules() -> None:
    plan = await PlannerAgent().aplan("单步任务")
    assert len(plan.steps) == 1


@pytest.mark.asyncio
async def test_executor_llm_output_for_no_tool_step() -> None:
    llm = _StubLLM("服务运行正常")
    result = await ExecutorAgent(llm_client=llm).execute(
        WorkflowStep(step_id="step-1", goal="检查服务", tool_hint=None)
    )
    assert result.success is True
    assert result.output == "服务运行正常"


@pytest.mark.asyncio
async def test_executor_placeholder_when_llm_fails() -> None:
    result = await ExecutorAgent(llm_client=_BrokenLLM()).execute(
        WorkflowStep(step_id="step-1", goal="检查服务", tool_hint=None)
    )
    assert result.output == "Executed: 检查服务"


@pytest.mark.asyncio
async def test_executor_placeholder_without_llm() -> None:
    result = await ExecutorAgent().execute(
        WorkflowStep(step_id="step-1", goal="检查服务", tool_hint=None)
    )
    assert result.output == "Executed: 检查服务"
