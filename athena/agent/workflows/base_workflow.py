"""
📦 模块名称：CloudOps 工作流基础模型
📍 架构位置：云运维工作流层的数据契约，位于具体故障流程和 API 服务层之间。
🎯 核心作用：定义 CloudOps 工作流每一步和最终结果的统一数据结构。
🔗 依赖关系：依赖 dataclass 和 JSONValue；被 FaultDiagnoseWorkflow 与 AthenaWebService 依赖。
💡 设计思路：使用“数据传输对象 DTO”模式，让流程输出稳定、可测试、可展示。
📚 学习重点：看 CloudWorkflowStep 如何把复杂运维动作拆成前端可展示的步骤。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from athena.types import JSONValue


@dataclass(frozen=True)
class CloudWorkflowStep:
    """
    CloudOps 工作流中的一个可观测步骤。

    功能说明：记录某一步的名称、状态、说明和结构化数据。
    参数说明：name 是步骤名；status 是执行状态；detail 是给人看的说明；data 是机器可读上下文。
    返回值：数据容器，不主动返回业务结果。
    设计思路：一步一条记录，Web 右侧轨迹面板就能按步骤展示“发生了什么”。
    使用示例：CloudWorkflowStep("collect_context", "success", "collected metrics")
    """

    name: str
    status: str
    detail: str
    data: dict[str, JSONValue] = field(
        default_factory=dict
    )  # 💡 学习提示：用 default_factory 避免多个步骤共享同一个 dict。


@dataclass(frozen=True)
class CloudWorkflowResult:
    """
    CloudOps 工作流最终结果。

    功能说明：保存一次工作流的 run_id、状态、摘要、步骤和可选知识库 id。
    参数说明：run_id 标识本次运行；summary 是最终回答；knowledge_id 指向沉淀的运维知识。
    返回值：数据容器，被服务层转换为 API 响应。
    设计思路：把执行结果和知识沉淀 id 放在一起，体现“排障完成后复盘入库”的闭环。
    使用示例：CloudWorkflowResult("fault-1", "success", "Root cause...", steps)
    """

    run_id: str
    status: str
    summary: str
    steps: tuple[CloudWorkflowStep, ...]
    knowledge_id: str | None = None


"""
🤔 思考题：

1. 如果一个工作流步骤需要人工确认，你会给 CloudWorkflowStep 增加哪些字段？
2. 为什么 steps 用 tuple，而不是 list？在不可变结果对象里这样有什么好处？
3. 如果 data 里包含密钥或 Token，应该在哪里做脱敏？
4. ⚡ 优化建议：未来可以给 CloudWorkflowStep 增加 duration_ms，前端就能展示每一步耗时。
"""
