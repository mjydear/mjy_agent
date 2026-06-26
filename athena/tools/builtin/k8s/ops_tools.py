"""
📦 模块名称：Kubernetes 安全运维工具
📍 架构位置：CloudOps 工具层，位于 K8sClient 和 Web/API 场景服务之间。
🎯 核心作用：提供集群快照、Deployment YAML 生成和 YAML 基础校验。
🔗 依赖关系：依赖 PyYAML 和 K8sClient；被 AthenaWebService、Demo 与测试依赖。
💡 设计思路：把安全、只读、可演示的 K8s 操作集中封装，避免业务层直接拼 YAML 或读原始客户端。
📚 学习重点：关注 YAML 生成/校验为什么放在工具层，而不是散落在 API 或前端。
"""

from __future__ import annotations

import yaml

from athena.tools.builtin.k8s.client import K8sClient
from athena.types import JSONValue


class K8sOpsTools:
    """
    Kubernetes 运维工具集合。

    功能说明：为 CloudOps 场景提供安全的 K8s 辅助能力。
    参数说明：client 是 K8sClient，不传时使用默认 Mock 客户端。
    返回值：方法返回 JSON 友好的 dict 或 YAML 字符串。
    设计思路：工具类像一个“工具箱”，工作流只拿工具箱结果，不直接碰底层 SDK。
    使用示例：K8sOpsTools().cluster_snapshot()
    """

    def __init__(self, client: K8sClient | None = None) -> None:
        """
        初始化 K8s 运维工具。

        功能说明：保存只读 K8s 客户端。
        参数说明：client 可用于注入真实或测试客户端。
        返回值：None。
        设计思路：默认 Mock 客户端让本地没有集群也能学习和演示。
        使用示例：K8sOpsTools(K8sClient(namespace="prod"))
        """
        self.client = client or K8sClient()

    def cluster_snapshot(self) -> dict[str, JSONValue]:
        """
        收集集群快照。

        功能说明：一次性返回 Pods、Nodes、Events 和资源用量。
        参数说明：无。
        返回值：包含 pods/nodes/events/usage 的字典。
        设计思路：故障排查通常需要多类上下文，一次收集能让前端和 workflow 展示更完整。
        使用示例：snapshot = tools.cluster_snapshot()
        """
        return {
            "pods": self.client.list_pods(),
            "nodes": self.client.list_nodes(),
            "events": self.client.list_events(),
            "usage": self.client.resource_usage(),
        }

    def generate_deployment_yaml(self, name: str, image: str, replicas: int = 2) -> str:
        """
        生成最小 Deployment YAML。

        功能说明：根据应用名、镜像和副本数生成 Kubernetes Deployment manifest。
        参数说明：name 是应用名；image 是镜像；replicas 是副本数量。
        返回值：YAML 字符串。
        设计思路：用结构化 dict 生成 YAML，比手写字符串更不容易缩进出错。
        使用示例：tools.generate_deployment_yaml("demo", "nginx:1.25", 2)

        🎯 面试考点：为什么不用字符串拼接 YAML？答案：YAML 对缩进敏感，结构化对象再 dump 更可靠。
        """
        if replicas <= 0:
            raise ValueError("replicas must be positive")
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name},
            "spec": {
                "replicas": replicas,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {"containers": [{"name": name, "image": image}]},
                },
            },
        }
        return yaml.safe_dump(
            manifest, sort_keys=False, allow_unicode=True
        )  # 💡 学习提示：safe_dump 避免输出 Python 专属对象标签，更适合生成配置文件。

    def validate_yaml(self, manifest_text: str) -> dict[str, JSONValue]:
        """
        校验 Kubernetes YAML 基础结构。

        功能说明：检查 YAML 是否是 mapping，并检查 apiVersion/kind/metadata/spec 四个关键字段。
        参数说明：manifest_text 是用户或工具生成的 YAML 文本。
        返回值：包含 valid/missing_fields/kind 的字典。
        设计思路：先做轻量结构校验，避免明显错误的 YAML 进入真实 kubectl 或集群。
        使用示例：tools.validate_yaml("apiVersion: apps/v1\nkind: Deployment")
        """
        loaded = yaml.safe_load(manifest_text)
        if not isinstance(loaded, dict):
            return {"valid": False, "error": "manifest must be a mapping"}
        missing = [
            field
            for field in ("apiVersion", "kind", "metadata", "spec")
            if field not in loaded
        ]  # 💡 学习提示：这里只做形状校验，不代表 manifest 语义一定能被 K8s 接受。
        return {
            "valid": not missing,
            "missing_fields": missing,
            "kind": loaded.get("kind", ""),
        }
