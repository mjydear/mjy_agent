"""
📦 模块名称：Kubernetes 内置故障诊断 SOP
📍 架构位置：CloudOps 诊断层，位于 K8sClient 原始数据和故障工作流之间。
🎯 核心作用：把 Pod/节点状态转换成根因、严重级别和修复建议。
🔗 依赖关系：依赖 K8sClient；被 FaultDiagnoseWorkflow 和 AthenaWebService 的 K8s 模式调用。
💡 设计思路：使用确定性规则/SOP，让常见故障在没有 LLM 的情况下也能稳定诊断。
📚 学习重点：看 diagnose_pods() 如何把状态码映射为可执行建议。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from athena.tools.builtin.k8s.client import K8sClient
from athena.types import JSONValue


@dataclass(frozen=True)
class K8sDiagnosis:
    """
    一条 Kubernetes 诊断结果。

    功能说明：保存故障对象、症状、根因、建议和严重级别。
    参数说明：pod 可以是 Pod 名或节点名；symptom 是表现；root_cause 是推断根因。
    返回值：数据容器。
    设计思路：诊断结果结构化后，既能展示给人，也能写入知识库或 trace。
    使用示例：K8sDiagnosis("pod-a", "CrashLoopBackOff", "exit", "check logs", "high")
    """

    pod: str
    symptom: str
    root_cause: str
    recommendation: str
    severity: str


class K8sDiagnoser:
    """
    Kubernetes 常见故障诊断器。

    功能说明：识别 CrashLoopBackOff、ImagePullBackOff 和节点资源压力。
    参数说明：client 是 K8sClient，可注入 Mock 或真实只读客户端。
    返回值：diagnose_pods() 返回 K8sDiagnosis 列表。
    设计思路：把“经验 SOP”写成规则，保证结果可解释、可测试。
    使用示例：K8sDiagnoser().diagnose_pods()
    """

    def __init__(self, client: K8sClient | None = None) -> None:
        """
        初始化诊断器。

        功能说明：保存 K8s 客户端，不传时使用默认 Mock 客户端。
        参数说明：client 是只读 K8s 数据来源。
        返回值：None。
        设计思路：依赖注入让测试能传入自定义数据，不需要真实集群。
        使用示例：K8sDiagnoser(K8sClient(namespace="prod"))
        """
        self.client = client or K8sClient()

    def diagnose_pods(self) -> list[K8sDiagnosis]:
        """
        诊断 Pod 和节点资源异常。

        功能说明：读取 Pod、事件和资源用量，输出一组诊断结论。
        参数说明：无。
        返回值：K8sDiagnosis 列表。
        设计思路：先用状态匹配，再补充事件信息，最后追加节点压力诊断。
        使用示例：diagnoses = diagnoser.diagnose_pods()

        🔍 原理讲解：
        K8s 的状态是“症状”，事件和资源用量更接近“原因”。
        输入 Pod 状态 + Event + Usage → 按 SOP 匹配 → 输出根因和建议。
        """
        diagnoses: list[K8sDiagnosis] = []
        events = self.client.list_events()
        events_by_pod = {
            str(event["pod"]): event for event in events
        }  # 💡 学习提示：先建索引，避免每个 Pod 都遍历所有事件。
        for pod in self.client.list_pods():
            status = str(pod["status"])
            name = str(pod["name"])
            event = events_by_pod.get(name, {})
            if status == "CrashLoopBackOff":
                diagnoses.append(
                    K8sDiagnosis(
                        name,
                        status,
                        "container process exits repeatedly",
                        "inspect logs, verify env vars, then roll back last image",
                        "high",
                    )
                )
            elif status == "ImagePullBackOff":
                diagnoses.append(
                    K8sDiagnosis(
                        name,
                        status,
                        str(event.get("message", "image pull failed")),
                        "check image tag, registry secret, and network policy",
                        "medium",
                    )
                )
        usage = self.client.resource_usage()
        cpu_pressure_nodes = usage.get("cpu_pressure_nodes", [])
        if not isinstance(cpu_pressure_nodes, Sequence) or isinstance(
            cpu_pressure_nodes, str
        ):
            cpu_pressure_nodes = []
        for node_name in cpu_pressure_nodes:
            diagnoses.append(
                K8sDiagnosis(
                    str(node_name),
                    "NodePressure",
                    "node CPU allocation is above 85%",
                    "scale out node pool or reschedule batch workloads",
                    "medium",
                )
            )
        return diagnoses

    def as_trace_steps(self) -> list[dict[str, JSONValue]]:
        """
        把诊断结果转换成 Web trace 友好的字典。

        功能说明：将 dataclass 结果转为 dict，方便 JSON 序列化。
        参数说明：无。
        返回值：字典列表。
        设计思路：API 层和前端更适合处理 JSON，而不是 Python dataclass 对象。
        使用示例：steps = diagnoser.as_trace_steps()
        """
        return [diagnosis.__dict__ for diagnosis in self.diagnose_pods()]
