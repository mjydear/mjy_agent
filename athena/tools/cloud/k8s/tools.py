"""
📦 模块名称：K8s 只读诊断工具注册
📍 架构位置：CloudOps 工具层，把 K8sReadOnlyClient 能力封装为 Agent 可调用的工具函数。
🎯 核心作用：向 ToolRegistry 注册 5 个只读 K8s 工具（list pods/describe/events/logs/namespaces）。
🔗 依赖关系：依赖 ToolRegistry、K8sReadOnlyClient、可选 AthenaSettings；被 CLI 装配调用。
💡 设计思路：沿用 register_file_tools 的“闭包注入依赖”模式——把 client 注入闭包，
           工具函数只关心参数与返回，客户端来源（mock/real/测试替身）对工具透明。
📚 学习重点：
   1. 为什么工具函数返回 JSON 字符串——ToolResult.content 是 str，json.dumps 输出可读且结构化。
   2. client 优先级：显式 client > 从 settings 构造 > 默认 mock，保证零配置也能注册。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from athena.agent.policy.contracts import RiskLevel, ToolSpecV2
from athena.tools.cloud.k8s.client import K8sReadOnlyClient
from athena.tools.cloud.k8s.diagnose import K8sReadOnlyDiagnoser
from athena.types import JSONValue

if TYPE_CHECKING:
    from athena.config import AthenaSettings
    from athena.tools import ToolRegistry


K8S_READONLY_TOOL_SPECS: tuple[ToolSpecV2, ...] = (
    ToolSpecV2(
        "k8s.pod.list",
        "1.0.0",
        "kubernetes",
        {
            "type": "object",
            "properties": {"namespace": {"type": "string"}},
            "additionalProperties": False,
        },
        {},
        ("k8s.workload.read",),
        RiskLevel.S1,
        True,
        True,
        10.0,
    ),
    ToolSpecV2(
        "k8s.pod.get",
        "1.0.0",
        "kubernetes",
        {
            "type": "object",
            "properties": {"namespace": {"type": "string"}, "name": {"type": "string"}},
            "required": ["namespace", "name"],
            "additionalProperties": False,
        },
        {},
        ("k8s.workload.read",),
        RiskLevel.S1,
        True,
        True,
        10.0,
    ),
    ToolSpecV2(
        "k8s.events.list",
        "1.0.0",
        "kubernetes",
        {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "pod_name": {"type": "string"},
            },
            "additionalProperties": False,
        },
        {},
        ("k8s.events.read",),
        RiskLevel.S1,
        True,
        True,
        10.0,
    ),
    ToolSpecV2(
        "k8s.logs.read",
        "1.0.0",
        "kubernetes",
        {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "name": {"type": "string"},
                "container": {"type": "string"},
                "tail_lines": {"type": "integer"},
            },
            "required": ["namespace", "name"],
            "additionalProperties": False,
        },
        {},
        ("k8s.logs.read",),
        RiskLevel.S1,
        True,
        True,
        10.0,
    ),
)

K8S_READONLY_LEGACY_TOOL_NAMES: dict[str, str] = {
    "k8s.pod.list": "k8s_list_pods",
    "k8s.pod.get": "k8s_describe_pod",
    "k8s.events.list": "k8s_list_events",
    "k8s.logs.read": "k8s_get_pod_logs",
}

K8S_READONLY_V2_ADAPTER_NAMES: dict[str, str] = {
    "k8s.pod.list": "k8s_v2_pod_list",
    "k8s.pod.get": "k8s_v2_pod_get",
    "k8s.events.list": "k8s_v2_events_list",
    "k8s.logs.read": "k8s_v2_logs_read",
}


def _dumps(value: JSONValue) -> str:
    """把 JSON 友好对象序列化为紧凑可读字符串，供工具返回。"""
    return json.dumps(value, ensure_ascii=False, indent=2)


def register_k8s_readonly_tools(
    registry: ToolRegistry,
    client: K8sReadOnlyClient | None = None,
    settings: AthenaSettings | None = None,
) -> None:
    """
    注册 K8s 只读诊断工具到工具注册表。

    功能说明：把 K8sReadOnlyClient 的五类只读能力封装为 Agent 工具。
    参数说明：
        registry：目标工具注册表。
        client：显式注入的只读客户端（测试/复用时使用），优先级最高。
        settings：无 client 时从 settings.ops 构造客户端。
    返回值：None（副作用是向 registry 注册工具）。
    设计思路：client 优先级 显式 > settings > 默认 mock，保证任何调用场景都能安全注册。
    使用示例：register_k8s_readonly_tools(registry, settings=load_settings())
    """
    if client is not None:
        k8s = client
    elif settings is not None:
        k8s = K8sReadOnlyClient.from_settings(settings)
    else:
        k8s = K8sReadOnlyClient()  # 零配置默认 mock，本地演示友好

    diagnoser = K8sReadOnlyDiagnoser(k8s)  # 诊断器复用同一 client，保证数据来源一致

    @registry.register
    def k8s_list_pods(namespace: str = "default") -> str:
        """List pods with status/restarts in a Kubernetes namespace (read-only)."""
        return _dumps(k8s.list_pods(namespace))

    @registry.register
    def k8s_describe_pod(namespace: str, name: str) -> str:
        """Describe a single pod: status, node, containers and conditions (read-only)."""
        return _dumps(k8s.describe_pod(namespace, name))

    @registry.register
    def k8s_list_events(namespace: str = "default", pod_name: str = "") -> str:
        """List Kubernetes events in a namespace, optionally filtered by pod (read-only)."""
        return _dumps(k8s.list_events(namespace, pod_name or None))

    @registry.register
    def k8s_get_pod_logs(
        namespace: str,
        name: str,
        container: str = "",
        tail_lines: int = 100,
    ) -> str:
        """Get the tail of a pod container's logs (read-only)."""
        return k8s.get_pod_logs(namespace, name, container or None, tail_lines)

    @registry.register
    def k8s_list_namespaces() -> str:
        """List cluster namespaces visible under the allowlist (read-only)."""
        return _dumps(k8s.list_namespaces())

    @registry.register
    def k8s_list_deployments(namespace: str = "default") -> str:
        """List deployments with desired/ready replica health in a namespace (read-only)."""
        return _dumps(k8s.list_deployments(namespace))

    @registry.register
    def k8s_list_services(namespace: str = "default") -> str:
        """List services with selector and ports in a namespace (read-only)."""
        return _dumps(k8s.list_services(namespace))

    @registry.register
    def k8s_list_endpoints(namespace: str = "default") -> str:
        """List service endpoints with ready addresses and ports in a namespace (read-only)."""
        return _dumps(k8s.list_endpoints(namespace))

    @registry.register
    def k8s_get_node_status() -> str:
        """Get cluster node readiness, resource pressure and allocatable capacity (read-only)."""
        return _dumps(k8s.get_node_status())

    @registry.register
    def k8s_diagnose_namespace(
        namespace: str = "default",
        include_logs: bool = True,
        tail_lines: int = 20,
    ) -> str:
        """Diagnose pod failures in a namespace via status+events+logs (read-only)."""
        return _dumps(
            diagnoser.as_dicts(
                namespace, include_logs=include_logs, log_tail_lines=tail_lines
            )
        )


def register_k8s_readonly_v2_adapters(
    registry: ToolRegistry, client: K8sReadOnlyClient
) -> None:
    """Register source-aware provider adapters used only by ToolRuntime V2."""

    @registry.register
    def k8s_v2_pod_list(namespace: str = "default") -> str:
        items = client.list_pods(namespace)
        return _dumps({"items": items, "data_origin": client.last_data_origin})

    @registry.register
    def k8s_v2_pod_get(namespace: str, name: str) -> str:
        item = client.describe_pod(namespace, name)
        return _dumps({"item": item, "data_origin": client.last_data_origin})

    @registry.register
    def k8s_v2_events_list(namespace: str = "default", pod_name: str = "") -> str:
        items = client.list_events(namespace, pod_name or None)
        return _dumps({"items": items, "data_origin": client.last_data_origin})

    @registry.register
    def k8s_v2_logs_read(
        namespace: str,
        name: str,
        container: str = "",
        tail_lines: int = 100,
    ) -> str:
        content = client.get_pod_logs(namespace, name, container or None, tail_lines)
        return _dumps({"content": content, "data_origin": client.last_data_origin})
