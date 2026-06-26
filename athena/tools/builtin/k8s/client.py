"""
📦 模块名称：K8s 客户端封装
📍 架构位置：CloudOps 工具层，位于诊断逻辑和 Kubernetes 官方 Python 客户端之间。
🎯 核心作用：提供 Pod、节点、事件和资源用量的安全读取接口；无真实集群时返回 Mock 数据。
🔗 依赖关系：可选依赖 kubernetes 官方客户端；被 K8sOpsTools 和 K8sDiagnoser 调用。
💡 设计思路：使用“适配器 + 降级 Mock”模式，演示环境不需要真实集群，生产环境可替换真实 API。
📚 学习重点：看工具层如何隔离外部系统不稳定性，让上层工作流保持稳定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from athena.types import JSONValue


@dataclass(frozen=True)
class K8sClient:
    """
    Kubernetes 只读客户端门面。

    功能说明：提供 Pod、节点、事件和资源用量的读取接口。
    参数说明：namespace 是命名空间；use_mock 表示当前使用演示数据。
    返回值：各 list/resource 方法返回 JSON 友好的 dict/list。
    设计思路：门面模式隔离真实 Kubernetes SDK，业务层不关心底层是 Mock 还是真集群。
    使用示例：K8sClient(namespace="default").list_pods()
    """

    namespace: str = "default"
    use_mock: bool = True

    def list_pods(self) -> list[dict[str, JSONValue]]:
        """
        获取 Pod 状态快照。

        功能说明：返回 Running、CrashLoopBackOff、ImagePullBackOff 三类典型 Pod。
        参数说明：无，读取构造函数中的 namespace。
        返回值：Pod 字典列表。
        设计思路：Mock 数据覆盖常见故障，方便 K8sDiagnoser 演示 SOP 判断。
        使用示例：pods = client.list_pods()
        """
        return [
            {
                "name": "api-7d9c",
                "namespace": self.namespace,
                "status": "Running",
                "restarts": 0,
                "cpu_m": 180,
                "memory_mi": 256,
            },
            {
                "name": "checkout-5f8b",
                "namespace": self.namespace,
                "status": "CrashLoopBackOff",
                "restarts": 7,
                "cpu_m": 90,
                "memory_mi": 128,
            },
            {
                "name": "image-worker-22a",
                "namespace": self.namespace,
                "status": "ImagePullBackOff",
                "restarts": 0,
                "cpu_m": 0,
                "memory_mi": 0,
            },
        ]

    def list_nodes(self) -> list[dict[str, JSONValue]]:
        """
        获取节点健康快照。

        功能说明：返回节点 ready 状态和资源分配率。
        参数说明：无。
        返回值：节点字典列表。
        设计思路：节点资源压力是 Pod 异常的常见背景信息，所以和 Pod 一起提供。
        使用示例：nodes = client.list_nodes()
        """
        return [
            {
                "name": "node-a",
                "ready": True,
                "cpu_allocated": 0.62,
                "memory_allocated": 0.71,
            },
            {
                "name": "node-b",
                "ready": True,
                "cpu_allocated": 0.91,
                "memory_allocated": 0.88,
            },
        ]

    def list_events(self, pod_name: str | None = None) -> list[dict[str, JSONValue]]:
        """
        查询 Kubernetes 事件。

        功能说明：返回 Pod 相关事件，可按 pod_name 过滤。
        参数说明：pod_name 为空时返回全部事件；不为空时只返回指定 Pod 事件。
        返回值：事件字典列表。
        设计思路：事件往往比状态更接近原因，例如 ImagePullBackOff 的具体拉取失败信息。
        使用示例：client.list_events("checkout-5f8b")
        """
        events: list[dict[str, JSONValue]] = [
            {
                "pod": "checkout-5f8b",
                "type": "Warning",
                "reason": "BackOff",
                "message": "Back-off restarting failed container",
            },
            {
                "pod": "image-worker-22a",
                "type": "Warning",
                "reason": "Failed",
                "message": "Failed to pull image registry/demo:missing",
            },
            {
                "pod": "api-7d9c",
                "type": "Normal",
                "reason": "Pulled",
                "message": "Container image already present",
            },
        ]
        return [
            event for event in events if pod_name is None or event["pod"] == pod_name
        ]  # 💡 学习提示：过滤逻辑写在客户端层，上层诊断器可以直接拿到干净数据。

    def resource_usage(self) -> dict[str, JSONValue]:
        """
        汇总集群资源使用情况。

        功能说明：统计 Pod/节点数量，并找出 CPU/内存压力节点。
        参数说明：无。
        返回值：资源摘要字典。
        设计思路：把阈值判断集中在工具层，诊断器只消费 cpu_pressure_nodes 等结论。
        使用示例：usage = client.resource_usage()
        """
        pods = self.list_pods()
        nodes = self.list_nodes()
        return {
            "namespace": self.namespace,
            "pod_count": len(pods),
            "node_count": len(nodes),
            "cpu_pressure_nodes": [
                node["name"]
                for node in nodes
                if float(cast(str | int | float, node["cpu_allocated"])) > 0.85
            ],  # 💡 学习提示：0.85 是演示阈值，真实系统应来自配置或 SLO。
            "memory_pressure_nodes": [
                node["name"]
                for node in nodes
                if float(cast(str | int | float, node["memory_allocated"])) > 0.85
            ],
        }
