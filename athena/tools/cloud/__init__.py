"""
📦 模块名称：CloudOps 工具包
📍 架构位置：工具层（Tool Layer）下的云运维子包，聚合 K8s 等只读诊断能力。
🎯 核心作用：为 Agent 提供接入真实云基础设施（当前为 Kubernetes）的安全只读工具。
🔗 依赖关系：子模块 k8s 依赖 kubernetes 官方 SDK（可选）；被 CLI/tool registry 装配。
💡 设计思路：与 builtin 演示工具分开，独立承载“真实集群 + 自动降级 mock”的生产向能力。
"""

from athena.tools.cloud.prometheus import PrometheusQueryClient, PrometheusQueryResult

__all__ = ["PrometheusQueryClient", "PrometheusQueryResult"]
