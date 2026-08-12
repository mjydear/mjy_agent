"""
📦 模块名称：步骤调试器（Step Debugger）
📍 架构位置：可观测性层的调试组件，位于工作流执行过程旁路。
🎯 核心作用：提供断点、暂停、单步和停止等调试控制的基础模型。
🔗 依赖关系：只依赖 dataclass；可被 WorkflowEngine、Web 调试页面或 CLI 调试命令调用。
💡 设计思路：先定义调试命令和断点状态，不强行耦合具体 UI。
📚 学习重点：理解调试器的本质是“控制执行节奏”，不是替代业务逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DebuggerCommand:
    """
    单步调试命令。

    功能说明：表示用户对调试器发出的控制命令。
    参数说明：command 可选 continue、step、pause、stop。
    返回值：数据容器。
    设计思路：把命令限制在固定集合里，避免 UI 传入拼错字符串导致不可预测行为。
    使用示例：DebuggerCommand("pause")
    """

    command: str

    def __post_init__(self) -> None:
        if self.command not in {"continue", "step", "pause", "stop"}:
            raise ValueError("debugger command must be continue, step, pause or stop")


class StepDebugger:
    """
    支持断点、单步和人工干预的轻量调试器。

    功能说明：维护断点集合和暂停状态。
    参数说明：无构造参数。
    返回值：should_pause() 返回布尔值；其他方法返回 None。
    设计思路：调试器只判断是否暂停，不直接执行或修改 Agent 业务状态。
    使用示例：debugger.add_breakpoint("step-1"); debugger.should_pause("step-1")
    """

    def __init__(self) -> None:
        self.breakpoints: set[str] = set()
        self.paused = False

    def add_breakpoint(self, step_id: str) -> None:
        """
        添加断点。

        功能说明：把指定步骤 id 加入断点集合。
        参数说明：step_id 是工作流步骤编号。
        返回值：None。
        设计思路：使用 set 可以天然去重，多次添加同一断点不会产生重复。
        使用示例：debugger.add_breakpoint("step-2")
        """
        if not step_id.strip():
            raise ValueError("step_id must be non-empty")
        self.breakpoints.add(step_id)

    def should_pause(self, step_id: str) -> bool:
        """
        判断是否应在某步骤暂停。

        功能说明：如果全局 paused 为 True，或当前 step_id 命中断点，就返回 True。
        参数说明：step_id 是当前执行步骤。
        返回值：布尔值。
        设计思路：把“全局暂停”和“断点暂停”合成一个判断，调用方更简单。
        使用示例：if debugger.should_pause(step.step_id): wait_for_user()
        """
        return self.paused or step_id in self.breakpoints

    def apply(self, command: DebuggerCommand) -> None:
        """
        应用调试命令。

        功能说明：根据命令更新调试器状态。
        参数说明：command 是 DebuggerCommand。
        返回值：None。
        设计思路：当前 MVP 只实现 pause 状态，step/stop 先保留命令模型给未来扩展。
        使用示例：debugger.apply(DebuggerCommand("pause"))
        """
        self.paused = (
            command.command == "pause"
        )  # 💡 学习提示：这里看似只处理 pause，是为了先保留稳定接口，再逐步扩展 step/stop 行为。


"""
🤔 思考题：

1. 如果要真正实现 step 单步执行，WorkflowEngine 需要在哪些地方配合？
2. stop 命令应该抛异常、中断循环，还是返回特殊状态？
3. 断点是否应该支持条件表达式，例如 output 包含 error 才暂停？
4. ⚡ 优化建议：未来可以把调试事件也写入 Tracer，方便回放调试过程。
"""
