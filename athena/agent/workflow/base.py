"""
📦 模块名称：多 Agent 工作流基类（Workflow Base）
📍 架构位置：Agent 编排层：
              [PlannerAgent] → [ExecutorAgent] → [ValidatorAgent]
🎯 核心作用：定义标准消息体、计划、状态和轻量工作流引擎，支撑 Plan-and-Execute 范式。
🔗 依赖关系：依赖 AgentResponse 和 JSONValue；被 PlannerAgent、ExecutorAgent、ValidatorAgent、Demo 和测试依赖。
💡 设计思路：使用“角色分工 + 状态机”模式。Planner 负责想清楚做什么，Executor 负责做，Validator 负责检查结果。
📚 学习重点：看 WorkflowState 如何推进步骤，以及 WorkflowEngine 如何串起三类 Agent。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from athena.agent.base import AgentResponse
from athena.types import JSONValue


@dataclass(frozen=True)
class WorkflowMessage:
    """
    角色间通信的标准化消息体。

    功能说明：表示多 Agent 之间传递的一条消息。
    参数说明：
        role：消息发送方角色，例如 planner、executor、validator。
        content：消息正文。
        metadata：附加信息，例如 trace_id、tool_name 等。
    返回值：数据容器，不主动返回。
    设计思路：统一消息格式后，未来接 Web UI、日志、回放系统会更容易。
    使用示例：WorkflowMessage(role="planner", content="先检查日志")
    """

    role: str
    content: str
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验消息角色和内容不能为空，避免无效消息进入工作流。"""
        if not self.role.strip():
            raise ValueError("role must be non-empty")
        if not self.content.strip():
            raise ValueError("content must be non-empty")


@dataclass(frozen=True)
class WorkflowStep:
    """
    规划 Agent 输出的单个结构化步骤。

    功能说明：描述计划中的一步，包括目标和推荐工具。
    参数说明：
        step_id：步骤唯一编号。
        goal：这一步要完成的目标。
        tool_hint：Planner 推测可用的工具名，可以为空。
    返回值：数据容器。
    设计思路：把自然语言任务拆成结构化步骤，Executor 才能稳定执行。
    使用示例：WorkflowStep(step_id="step-1", goal="读取日志", tool_hint="read_text_file")
    """

    step_id: str
    goal: str
    tool_hint: str | None = None

    def __post_init__(self) -> None:
        if not self.step_id.strip():
            raise ValueError("step_id must be non-empty")
        if not self.goal.strip():
            raise ValueError("goal must be non-empty")


@dataclass(frozen=True)
class WorkflowPlan:
    """
    一组可执行的结构化计划步骤。

    功能说明：保存原始任务和拆解后的步骤列表。
    参数说明：
        task：用户原始任务。
        steps：按顺序执行的 WorkflowStep。
    返回值：数据容器。
    设计思路：计划对象是 Planner 和 Engine 之间的契约，避免直接传散乱的字符串列表。
    使用示例：WorkflowPlan(task="排查故障", steps=(step1, step2))
    """

    task: str
    steps: tuple[WorkflowStep, ...]

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise ValueError("task must be non-empty")
        if not self.steps:
            raise ValueError("plan steps must not be empty")


@dataclass(frozen=True)
class WorkflowStepResult:
    """
    执行 Agent 对单个步骤的执行结果。

    功能说明：记录某一步执行是否成功、输出是什么、错误是什么。
    参数说明：
        step_id：对应的步骤编号。
        success：是否成功。
        output：执行输出。
        error：失败原因，可为空。
    返回值：数据容器。
    设计思路：结果对象让 Validator 可以独立判断，而不是直接解析 Executor 的内部状态。
    使用示例：WorkflowStepResult(step_id="step-1", success=True, output="done")
    """

    step_id: str
    success: bool
    output: str
    error: str | None = None


@dataclass
class WorkflowState:
    """
    工作流运行状态，支持断点续跑所需的最小信息。

    功能说明：保存当前执行到第几步、已经完成哪些结果。
    参数说明：
        run_id：本次工作流运行 id。
        plan：完整计划。
        current_index：当前待执行步骤下标。
        results：已完成步骤结果。
        created_at：创建时间。
    返回值：数据容器，next_step()/record() 提供状态推进能力。
    设计思路：把状态集中保存，未来做暂停、恢复、可视化时更简单。
    使用示例：state.next_step(); state.record(result)

    🎯 面试考点：为什么需要 WorkflowState？答案：复杂任务不能只靠局部变量，否则无法断点续跑、调试和回放。
    """

    run_id: str
    plan: WorkflowPlan
    current_index: int = 0
    results: list[WorkflowStepResult] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def next_step(self) -> WorkflowStep | None:
        """
        返回下一个待执行步骤。

        功能说明：根据 current_index 找到当前步骤；如果全部执行完，返回 None。
        参数说明：无。
        返回值：WorkflowStep 或 None。
        设计思路：让 Engine 不需要直接操作索引，减少越界错误。
        使用示例：step = state.next_step()
        """
        if self.current_index >= len(self.plan.steps):
            return None
        return self.plan.steps[self.current_index]

    def record(self, result: WorkflowStepResult) -> None:
        """
        记录步骤结果并推进游标。

        功能说明：保存当前步骤结果，并把 current_index 往前推进一位。
        参数说明：result 是当前步骤的执行结果。
        返回值：None。
        设计思路：先校验 step_id，防止把 A 步骤的结果误记录到 B 步骤。
        使用示例：state.record(step_result)
        """
        if result.step_id != self.plan.steps[self.current_index].step_id:
            raise ValueError("result step_id does not match current step")
        self.results.append(result)
        self.current_index += (
            1  # 💡 学习提示：只有结果成功记录后才推进游标，避免状态和结果列表不同步。
        )


class WorkflowEngine:
    """
    轻量 Plan-and-Execute 工作流引擎。

    功能说明：串联 Planner、Executor、Validator，完成一个复杂任务。
    参数说明：
        planner：负责拆任务。
        executor：负责执行步骤。
        validator：负责校验结果和轻量修复。
    返回值：run() 返回 AgentResponse。
    设计思路：这是门面/编排器角色，调用方只关心 run(task)，不需要知道内部三角色如何协作。
    使用示例：response = await WorkflowEngine(planner, executor, validator).run("检查服务; 收集日志")
    """

    def __init__(
        self,
        planner: "PlannerAgent",
        executor: "ExecutorAgent",
        validator: "ValidatorAgent",
    ) -> None:
        self.planner = planner
        self.executor = executor
        self.validator = validator

    async def run(self, task: str) -> AgentResponse:
        """
        执行规划、执行、校验三段式工作流。

        功能说明：先生成计划，再逐步执行和校验，最后汇总为 AgentResponse。
        参数说明：task 是用户输入的复杂任务。
        返回值：AgentResponse，包含最终答案和步骤摘要。
        设计思路：每一步都经过 Validator，是为了把“执行成功”和“结果可信”区分开。
        使用示例：await engine.run("inspect service; collect logs")

        🔍 原理讲解：
        输入："检查服务; 收集日志"
        处理过程：Planner 拆成两步 → Executor 执行第一步 → Validator 校验 → 继续下一步
        输出：把每一步输出拼成最终回答。
        """
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be a non-empty string")
        plan = self.planner.plan(
            task
        )  # 💡 学习提示：先规划再执行，比边想边做更适合复杂任务和可观测调试。
        state = WorkflowState(run_id=f"workflow-{int(time.time() * 1000)}", plan=plan)
        while (step := state.next_step()) is not None:
            result = await self.executor.execute(step)
            validation = self.validator.validate(step, result)
            if not validation.accepted and validation.repaired_result is not None:
                result = (
                    validation.repaired_result
                )  # 💡 学习提示：Validator 可以做轻量修复，但不直接修改 state，避免职责混乱。
            state.record(result)
        answer = "\n".join(result.output for result in state.results)
        steps = [f"{result.step_id}: {result.output}" for result in state.results]
        return AgentResponse(answer=answer, steps=steps)


from athena.agent.workflow.executor_agent import ExecutorAgent  # noqa: E402
from athena.agent.workflow.planner_agent import PlannerAgent  # noqa: E402
from athena.agent.workflow.validator_agent import ValidatorAgent  # noqa: E402

"""
🤔 思考题：

1. 如果 Validator 发现结果失败，当前只做轻量修复；如果要重试 Executor，你会怎么改？
2. 为什么 WorkflowEngine 返回 AgentResponse，而不是新建一个 WorkflowResponse？
3. 如果要支持人工暂停审批，应该加在 WorkflowState、Validator 还是 Engine？
4. ⚡ 优化建议：未来可以把 run_id 换成 uuid4，避免毫秒时间戳在高并发下重复。
"""
