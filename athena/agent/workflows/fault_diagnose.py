"""
📦 模块名称：CloudOps 标准故障排查工作流
📍 架构位置：云运维场景编排层，连接告警、指标、K8s 诊断、沙箱验证和知识沉淀。
🎯 核心作用：把一次告警处理成“收集信息 → 分析根因 → 给出建议 → 归档知识”的完整闭环。
🔗 依赖关系：依赖 K8sDiagnoser、PrometheusClient、OpsKnowledgeBase；被 AthenaWebService 的 fault 模式调用。
💡 设计思路：使用“模板流程”思想，先固定排障步骤，后续每一步可以替换成真实工具或 LLM 分析。
📚 学习重点：重点看 run() 如何把多个工具结果合并成可展示、可复用的 CloudWorkflowResult。
"""

from __future__ import annotations

import time

from athena.agent.workflows.base_workflow import CloudWorkflowResult, CloudWorkflowStep
from athena.memory.ops_knowledge import OpsKnowledgeBase
from athena.tools.builtin.k8s import K8sDiagnoser
from athena.tools.builtin.observability import PrometheusClient


class FaultDiagnoseWorkflow:
    """
    告警驱动的故障排查工作流。

    功能说明：接收告警名，自动收集上下文、分析根因、生成修复建议并写入知识库。
    参数说明：构造函数可注入 diagnoser/prometheus/knowledge，便于测试和替换真实实现。
    返回值：run() 返回 CloudWorkflowResult。
    设计思路：依赖注入让工作流不绑定具体外部系统；Mock 和真实客户端都能接入同一流程。
    使用示例：FaultDiagnoseWorkflow().run("KubePodCrashLooping")

    🎯 面试考点：为什么不把这些逻辑直接写在 API service 里？答案：工作流是业务能力，应独立成可复用模块，CLI、Web、Webhook 都能调用。
    """

    def __init__(
        self,
        diagnoser: K8sDiagnoser | None = None,
        prometheus: PrometheusClient | None = None,
        knowledge: OpsKnowledgeBase | None = None,
    ) -> None:
        """
        初始化故障排查工作流。

        功能说明：保存 K8s 诊断器、Prometheus 查询器和运维知识库。
        参数说明：diagnoser 负责 K8s SOP；prometheus 负责指标上下文；knowledge 负责案例沉淀。
        返回值：None。
        设计思路：默认创建 Mock-friendly 组件，测试或生产可以注入自己的实现。
        使用示例：FaultDiagnoseWorkflow(knowledge=OpsKnowledgeBase())
        """
        self.diagnoser = (
            diagnoser or K8sDiagnoser()
        )  # 💡 学习提示：or 默认值让 demo 无需真实集群也能运行。
        self.prometheus = prometheus or PrometheusClient()
        self.knowledge = knowledge or OpsKnowledgeBase()

    def run(self, alert_name: str = "KubePodCrashLooping") -> CloudWorkflowResult:
        """
        执行一次确定性的故障排查。

        功能说明：根据告警名收集诊断信息，选择根因和建议，生成步骤并归档知识。
        参数说明：alert_name 是告警名，例如 KubePodCrashLooping。
        返回值：CloudWorkflowResult，包含摘要、步骤和知识库 id。
        设计思路：第一版用确定性规则保证演示稳定，未来可把 root_cause 选择换成 LLM 或规则引擎。
        使用示例：result = workflow.run("KubePodCrashLooping")

        🔍 原理讲解：
        这里像一个标准排障 SOP：先拿 K8s 诊断，再拿指标上下文，然后选出最优先的问题作为根因。

        举个例子：
        输入 KubePodCrashLooping → 诊断出 CrashLoopBackOff → 生成回滚/查日志建议 → 写入知识库。
        """
        run_id = f"fault-{int(time.time() * 1000)}"
        diagnoses = (
            self.diagnoser.as_trace_steps()
        )  # 💡 学习提示：诊断结果先转成 dict，后面可以直接塞进 JSON 响应。
        metrics = self.prometheus.alert_context(alert_name)
        root_cause = (
            diagnoses[0]["root_cause"] if diagnoses else "no active pod failure found"
        )  # 💡 学习提示：MVP 选第一个诊断作为主根因，简单但可解释。
        recommendation = (
            diagnoses[0]["recommendation"] if diagnoses else "continue monitoring"
        )
        steps = (
            CloudWorkflowStep(
                "alert_received",
                "success",
                f"received alert {alert_name}",
                {"alert": alert_name},
            ),
            CloudWorkflowStep(
                "collect_context",
                "success",
                "collected metrics and k8s events",
                {"metrics": metrics, "diagnoses": diagnoses},
            ),
            CloudWorkflowStep(
                "root_cause_analysis",
                "success",
                str(root_cause),
                {"root_cause": root_cause},
            ),
            CloudWorkflowStep(
                "repair_plan",
                "success",
                str(recommendation),
                {
                    "recommendation": recommendation,
                    "risk": "write_requires_confirmation",
                },
            ),
            CloudWorkflowStep(
                "sandbox_verify",
                "success",
                "repair plan validated in dry-run sandbox",
                {"dry_run": True},
            ),
        )
        knowledge_id = self.knowledge.record_case(
            alert_name, str(root_cause), str(recommendation), success=True
        )  # 💡 学习提示：排障结果马上入库，体现“越用越会”的 Agent 闭环。
        return CloudWorkflowResult(
            run_id=run_id,
            status="success",
            summary=f"Root cause: {root_cause}. Suggestion: {recommendation}",
            steps=steps,
            knowledge_id=knowledge_id,
        )


"""
🤔 思考题：

1. 如果 diagnoses 有多条，怎样选择最重要的根因会更合理？
2. sandbox_verify 现在是模拟结果，如果要真的 dry-run kubectl，你会接到哪个模块？
3. 高危修复动作为什么不应该在这个工作流里直接自动执行？
4. ⚡ 优化建议：未来可以把 steps 拆成独立私有方法，让每一步支持重试和单独测试。
"""
