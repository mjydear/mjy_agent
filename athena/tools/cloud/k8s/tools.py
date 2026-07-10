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

from athena.tools.cloud.k8s.actions import (
    K8sActionSecurityPolicy,
    K8sWriteActionExecutor,
)
from athena.tools.cloud.k8s.client import K8sReadOnlyClient
from athena.tools.cloud.k8s.diagnose import K8sReadOnlyDiagnoser
from athena.types import JSONValue

if TYPE_CHECKING:
    from athena.config import AthenaSettings
    from athena.tools import ToolRegistry


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


def register_k8s_write_tools(
    registry: ToolRegistry,
    client: K8sReadOnlyClient | None = None,
    settings: AthenaSettings | None = None,
    *,
    actor: str = "system",
) -> None:
    """
    注册 K8s 受控写操作的**预览**工具到工具注册表（安全护栏）。

    功能说明：只注册 `k8s_preview_action`，让 Agent 能在诊断中提出低风险写操作方案，
        但绝不在 ReAct 循环内直接执行——真正执行由“人工确认后的确定性路径”完成
        （见 athena/api/services.py 的 run_cloud_ops confirmed 分支）。
    参数说明：
        registry：目标工具注册表。
        client：显式注入的只读客户端（写执行器复用它做 patch 与 verify），优先级最高。
        settings：无 client 时从 settings.ops 构造客户端与安全策略。
        actor：操作者标识，透传到 K8sActionPlan 的安全元数据。
    返回值：None（副作用是向 registry 注册工具）。
    设计思路：把“写操作必须人工确认”做成架构约束——Agent 只能 preview，不能 execute，
        使这条安全护栏无法被 LLM 的自主决策绕过。
    使用示例：register_k8s_write_tools(registry, settings=load_settings())
    """
    if client is not None:
        k8s = client
    elif settings is not None:
        k8s = K8sReadOnlyClient.from_settings(settings)
    else:
        k8s = K8sReadOnlyClient()

    if settings is not None:
        policy = K8sActionSecurityPolicy.from_settings(settings.ops.security)
    else:
        policy = K8sActionSecurityPolicy()

    executor = K8sWriteActionExecutor(k8s, policy, actor=actor)

    @registry.register
    def k8s_preview_action(task: str, namespace: str = "default") -> str:
        """Preview a low-risk Kubernetes write action (rollout restart / scale / pause / resume).

        This NEVER executes the action. It returns a plan describing the command,
        risk, whether human confirmation is required, blocked high-risk actions, and
        a rollback suggestion. Actual execution happens only after human confirmation.
        Returns a JSON object; when the task is read-only (no write intent) it returns
        {"action": null} so the agent knows to keep diagnosing read-only.
        """
        result = executor.preview(task, namespace)
        if result is None:
            return _dumps({"action": None, "reason": "no write intent detected; stay read-only"})
        return _dumps(result.to_dict())
