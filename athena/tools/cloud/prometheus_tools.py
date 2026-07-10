"""
📦 模块名称：Prometheus 指标查询工具注册
📍 架构位置：CloudOps 工具层，把 PrometheusQueryClient 能力封装为 Agent 可调用的原子工具。
🎯 核心作用：向 ToolRegistry 注册常用 Prometheus 查询工具，让 ReAct Agent 能自主串联指标证据。
🔗 依赖关系：依赖 ToolRegistry、PrometheusQueryClient、可选 AthenaSettings；被 CLI/服务层装配调用。
💡 设计思路：沿用 k8s/tools.py 的“闭包注入依赖”模式——把 client 注入闭包，工具函数只关心参数与返回，
           客户端来源（真实 HTTP / 测试替身）对工具透明。指标查询是只读能力，无需人工确认。
📚 学习重点：
   1. 为什么工具函数返回 JSON 字符串——ToolResult.content 是 str，json.dumps 输出结构化可读。
   2. Prometheus 是辅助证据源：不可用时返回 available=false 的结构化结果，不阻断 Agent 诊断。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from athena.tools.cloud.prometheus import PrometheusQueryClient
from athena.types import JSONValue

if TYPE_CHECKING:
    from athena.config import AthenaSettings
    from athena.tools import ToolRegistry


def _dumps(value: JSONValue) -> str:
    """把 JSON 友好对象序列化为紧凑可读字符串，供工具返回。"""
    return json.dumps(value, ensure_ascii=False, indent=2)


def register_prometheus_tools(
    registry: ToolRegistry,
    client: PrometheusQueryClient | None = None,
    settings: AthenaSettings | None = None,
) -> None:
    """
    注册 Prometheus 指标查询工具到工具注册表。

    功能说明：把 PrometheusQueryClient 的常用 Pod/Service 指标查询封装为 Agent 工具。
    参数说明：
        registry：目标工具注册表。
        client：显式注入的查询客户端（测试/复用时使用），优先级最高。
        settings：无 client 时从 settings.ops.prometheus 构造客户端。
    返回值：None（副作用是向 registry 注册工具）。
    设计思路：client 优先级 显式 > settings > 默认（disabled，返回 unavailable）。
    使用示例：register_prometheus_tools(registry, settings=load_settings())
    """
    if client is not None:
        prom = client
    elif settings is not None:
        prom = PrometheusQueryClient.from_settings(settings)
    else:
        prom = PrometheusQueryClient()  # 默认 disabled，返回结构化 unavailable

    @registry.register
    def prom_query(promql: str, name: str = "custom") -> str:
        """Run an arbitrary read-only PromQL query and return its scalar value."""
        return _dumps(prom.query(name, promql).to_dict())

    @registry.register
    def prom_pod_cpu(namespace: str, pod: str) -> str:
        """Query a pod's CPU usage in cores over the last 5 minutes (read-only)."""
        return _dumps(prom.pod_cpu_usage(namespace, pod).to_dict())

    @registry.register
    def prom_pod_memory(namespace: str, pod: str) -> str:
        """Query a pod's working-set memory usage in bytes (read-only)."""
        return _dumps(prom.pod_memory_usage(namespace, pod).to_dict())

    @registry.register
    def prom_pod_restarts(namespace: str, pod: str) -> str:
        """Query a pod's container restart count (read-only)."""
        return _dumps(prom.pod_restart_count(namespace, pod).to_dict())

    @registry.register
    def prom_http_5xx(namespace: str, service: str) -> str:
        """Query a service's HTTP 5xx error rate in requests per second (read-only)."""
        return _dumps(prom.http_5xx_error_rate(namespace, service).to_dict())

    @registry.register
    def prom_latency_p95(namespace: str, service: str) -> str:
        """Query a service's request latency P95 in seconds (read-only)."""
        return _dumps(prom.request_latency_p95(namespace, service).to_dict())

    @registry.register
    def prom_service_availability(namespace: str, service: str) -> str:
        """Query a service's availability ratio over the last 5 minutes (read-only)."""
        return _dumps(prom.service_availability(namespace, service).to_dict())

    @registry.register
    def prom_pod_snapshot(namespace: str, pod: str) -> str:
        """Query a pod's CPU, memory and restart metrics together (read-only)."""
        return _dumps([result.to_dict() for result in prom.pod_resource_snapshot(namespace, pod)])
