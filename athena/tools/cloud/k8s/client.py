"""
📦 模块名称：Kubernetes 只读诊断客户端
📍 架构位置：CloudOps 工具层，位于工具注册入口和 Kubernetes 官方 Python SDK 之间。
🎯 核心作用：提供 list pods、describe pod、list events、pod logs、list namespaces 等只读能力，
           并统一处理命名空间白名单校验与超时。
🔗 依赖关系：依赖 kubernetes 官方客户端（真实集群）；被 register_k8s_readonly_tools 调用，
           从 AthenaSettings.ops 读取配置。
💡 设计思路：**真实优先、无 mock**——所有查询直连真实集群；连接/调用失败直接抛 OpsError，
           不返回任何模拟数据（满足“彻底移除 Mock”）。单元测试通过注入 core_api/apps_api
           测试替身（test double，非产品 mock）覆盖逻辑，无需真实集群。
📚 学习重点：
   1. 为什么白名单校验先于真实调用——安全边界必须最先生效。
   2. core_api/apps_api 依赖注入如何让单元测试无需真实集群即可覆盖读写逻辑。
   3. 为什么失败直接抛错而非降级——运维诊断必须基于真实证据，假数据会误导根因判断。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from athena.exceptions import ErrorCode, OpsError
from athena.types import JSONValue

if TYPE_CHECKING:  # 仅类型检查期引入，运行时不强依赖 kubernetes/AthenaSettings
    from athena.config import AthenaSettings

logger = logging.getLogger(__name__)


class K8sReadOnlyClient:
    """
    Kubernetes 只读诊断客户端门面（真实集群，无 mock）。

    功能说明：提供 Pod 列表、Pod 描述、事件、日志、命名空间、Deployment、Service、
        Endpoints 与节点状态等只读能力，全部直连真实集群。
    参数说明：
        kubeconfig：kubeconfig 路径，None 时用 SDK 默认查找（集群内配置或 ~/.kube/config）。
        context：kubeconfig context 名称，None 时用当前默认 context。
        namespace_allowlist：命名空间白名单；为空表示不限制。
        timeout：单次 API 调用超时（秒）。
        core_api：可注入的 CoreV1Api（真实或测试替身），None 时惰性构建。
        apps_api：可注入的 AppsV1Api（真实或测试替身），None 时惰性构建。
    返回值：各方法返回 JSON 友好的 dict/list。
    设计思路：门面模式隔离真实 SDK；连接/调用失败直接抛 OpsError，绝不返回模拟数据。
    使用示例：K8sReadOnlyClient.from_settings(load_settings()).list_pods("default")
    """

    def __init__(
        self,
        *,
        kubeconfig: str | None = None,
        context: str | None = None,
        namespace_allowlist: list[str] | tuple[str, ...] | None = None,
        timeout: float = 10.0,
        core_api: object | None = None,
        apps_api: object | None = None,
    ) -> None:
        """
        初始化只读客户端。

        功能说明：保存连接参数与安全边界，并缓存可选注入的 core_api / apps_api。
        参数说明：见类文档。core_api / apps_api 主要用于真实注入或单元测试替身。
        返回值：None。
        使用示例：K8sReadOnlyClient(namespace_allowlist=["default"])
        """
        self.kubeconfig = kubeconfig
        self.context = context
        # 白名单用 tuple 存储：不可变、可安全共享，语义上也表达“配置快照”
        self.namespace_allowlist: tuple[str, ...] = tuple(namespace_allowlist or ())
        self.timeout = timeout
        self._core_api = core_api
        self._apps_api = apps_api  # AppsV1Api：deployments 等工作负载资源
        self._kube_config_loaded = False  # kubeconfig 只加载一次，core/apps 共享

    @classmethod
    def from_settings(cls, settings: AthenaSettings) -> K8sReadOnlyClient:
        """
        从 AthenaSettings 构造客户端。

        功能说明：读取 settings.ops 配置段完成装配，统一走 config.py + .env 加载。
        参数说明：settings 是顶层配置对象。
        返回值：配置好的 K8sReadOnlyClient。
        使用示例：K8sReadOnlyClient.from_settings(load_settings())
        """
        ops = settings.ops
        return cls(
            kubeconfig=ops.kubernetes.kubeconfig,
            context=ops.kubernetes.context,
            namespace_allowlist=ops.kubernetes.namespace_allowlist,
            timeout=ops.kubernetes.timeout,
        )

    # ------------------------------------------------------------------
    # 安全边界：命名空间白名单校验（越权直接报错，先于真实调用）
    # ------------------------------------------------------------------
    def _ensure_namespace_allowed(self, namespace: str) -> None:
        """
        校验命名空间是否在白名单内。

        功能说明：白名单非空时，只有名单内的命名空间可访问，否则抛 OpsError。
        参数说明：namespace 是待访问的命名空间。
        返回值：None（通过校验）。
        设计思路：安全校验先于真实调用，越权是配置/调用错误而非集群故障，直接报错。
        使用示例：self._ensure_namespace_allowed("prod")
        """
        if not isinstance(namespace, str) or not namespace.strip():
            raise OpsError(
                ErrorCode.OPS_NAMESPACE_FORBIDDEN,
                "namespace must be a non-empty string",
            )
        if self.namespace_allowlist and namespace not in self.namespace_allowlist:
            raise OpsError(
                ErrorCode.OPS_NAMESPACE_FORBIDDEN,
                f"namespace {namespace!r} is not in the allowlist "
                f"{list(self.namespace_allowlist)}",
            )

    # ------------------------------------------------------------------
    # 真实 SDK 接入：惰性构建 CoreV1Api / AppsV1Api
    # ------------------------------------------------------------------
    def _ensure_kube_config_loaded(self) -> None:
        """加载 kubeconfig（集群内优先，其次本地文件），只加载一次，core/apps 共享。"""
        if self._kube_config_loaded:
            return
        from kubernetes import config

        try:
            config.load_incluster_config()
        except Exception:  # 非集群内环境，回退本地 kubeconfig
            config.load_kube_config(
                config_file=self.kubeconfig, context=self.context
            )
        self._kube_config_loaded = True

    def _get_core_api(self) -> object:
        """惰性加载 kubernetes CoreV1Api，优先集群内配置，其次本地 kubeconfig。"""
        if self._core_api is not None:
            return self._core_api
        from kubernetes import client

        self._ensure_kube_config_loaded()
        self._core_api = client.CoreV1Api()
        return self._core_api

    def _get_apps_api(self) -> object:
        """惰性加载 kubernetes AppsV1Api（deployments 等），复用同一 kubeconfig 加载。"""
        if self._apps_api is not None:
            return self._apps_api
        from kubernetes import client

        self._ensure_kube_config_loaded()
        self._apps_api = client.AppsV1Api()
        return self._apps_api

    def _call_real(self, real_fn):  # type: ignore[no-untyped-def]
        """
        执行一次真实集群调用，失败抛 OpsError（不降级、不返回 mock）。

        功能说明：把连接失败/未装 SDK/API 错误统一转换为 OPS_REAL_UNAVAILABLE，
            保证 Agent 收到明确错误而非任何模拟数据。
        参数说明：real_fn 无参可调用，执行真实 SDK 请求。
        返回值：real_fn 的结果。
        使用示例：self._call_real(lambda: self._real_list_pods(ns))
        """
        try:
            return real_fn()
        except OpsError:
            raise
        except Exception as exc:  # 包含 ImportError（未装 SDK）、连接失败、API 错误
            raise OpsError(
                ErrorCode.OPS_REAL_UNAVAILABLE,
                f"kubernetes real call failed: {exc}",
            ) from exc

    # ==================================================================
    # 公开只读能力
    # ==================================================================
    def list_pods(self, namespace: str = "default") -> list[dict[str, JSONValue]]:
        """列出命名空间下的 Pod 状态快照（name/namespace/status/restarts/node/ready/labels）。"""
        self._ensure_namespace_allowed(namespace)
        return self._call_real(lambda: self._real_list_pods(namespace))

    def describe_pod(self, namespace: str, name: str) -> dict[str, JSONValue]:
        """获取单个 Pod 的详细描述（status/node/containers/conditions），近似 kubectl describe。"""
        self._ensure_namespace_allowed(namespace)
        if not isinstance(name, str) or not name.strip():
            raise OpsError(
                ErrorCode.OPS_NAMESPACE_FORBIDDEN, "pod name must be non-empty"
            )
        return self._call_real(lambda: self._real_describe_pod(namespace, name))

    def list_events(
        self, namespace: str = "default", pod_name: str | None = None
    ) -> list[dict[str, JSONValue]]:
        """列出命名空间事件，可按 Pod 过滤（pod/type/reason/message/count）。"""
        self._ensure_namespace_allowed(namespace)
        return self._call_real(lambda: self._real_list_events(namespace, pod_name))

    def get_pod_logs(
        self,
        namespace: str,
        name: str,
        container: str | None = None,
        tail_lines: int = 100,
    ) -> str:
        """获取 Pod 容器日志尾部内容（默认只取尾部若干行，避免拖垮 Agent 上下文）。"""
        self._ensure_namespace_allowed(namespace)
        if not isinstance(name, str) or not name.strip():
            raise OpsError(
                ErrorCode.OPS_NAMESPACE_FORBIDDEN, "pod name must be non-empty"
            )
        if tail_lines <= 0:
            raise ValueError("tail_lines must be positive")
        return self._call_real(
            lambda: self._real_get_pod_logs(namespace, name, container, tail_lines)
        )

    def list_namespaces(self) -> list[dict[str, JSONValue]]:
        """列出集群命名空间（白名单非空时只返回名单内命名空间，尊重安全边界）。"""
        namespaces = self._call_real(self._real_list_namespaces)
        if self.namespace_allowlist:
            namespaces = [
                ns for ns in namespaces if ns.get("name") in self.namespace_allowlist
            ]
        return namespaces

    def list_deployments(
        self, namespace: str = "default"
    ) -> list[dict[str, JSONValue]]:
        """列出命名空间下的 Deployment 副本健康状态（desired/ready 不一致往往是发布卡住）。"""
        self._ensure_namespace_allowed(namespace)
        return self._call_real(lambda: self._real_list_deployments(namespace))

    def list_services(
        self, namespace: str = "default"
    ) -> list[dict[str, JSONValue]]:
        """列出命名空间下的 Service 及其选择器/端口（selector 不匹配是服务不可达常见根因）。"""
        self._ensure_namespace_allowed(namespace)
        return self._call_real(lambda: self._real_list_services(namespace))

    def list_endpoints(
        self, namespace: str = "default"
    ) -> list[dict[str, JSONValue]]:
        """列出命名空间下的 Endpoints 地址快照（有 selector 但 endpoints 为空是关键证据）。"""
        self._ensure_namespace_allowed(namespace)
        return self._call_real(lambda: self._real_list_endpoints(namespace))

    def get_node_status(self) -> list[dict[str, JSONValue]]:
        """获取集群节点健康状态（集群级只读，不限命名空间）。"""
        return self._call_real(self._real_get_node_status)

    # ==================================================================
    # 真实 SDK 实现
    # ==================================================================
    def _real_list_pods(self, namespace: str) -> list[dict[str, JSONValue]]:
        api = self._get_core_api()
        pods = api.list_namespaced_pod(  # type: ignore[attr-defined]
            namespace, _request_timeout=self.timeout
        ).items
        result: list[dict[str, JSONValue]] = []
        for pod in pods:
            statuses = pod.status.container_statuses or []
            restarts = sum(cs.restart_count for cs in statuses)
            ready = all(cs.ready for cs in statuses) if statuses else False
            result.append(
                {
                    "name": pod.metadata.name,
                    "namespace": pod.metadata.namespace,
                    "status": pod.status.phase or "Unknown",
                    "restarts": restarts,
                    "node": pod.spec.node_name or "",
                    "ready": ready,
                    "labels": dict(pod.metadata.labels or {}),
                }
            )
        return result

    def _real_describe_pod(
        self, namespace: str, name: str
    ) -> dict[str, JSONValue]:
        api = self._get_core_api()
        pod = api.read_namespaced_pod(  # type: ignore[attr-defined]
            name, namespace, _request_timeout=self.timeout
        )
        statuses = pod.status.container_statuses or []
        containers: list[dict[str, JSONValue]] = []
        for cs in statuses:
            state = "unknown"
            if cs.state is not None:
                if cs.state.running is not None:
                    state = "running"
                elif cs.state.waiting is not None:
                    state = f"waiting:{cs.state.waiting.reason or ''}"
                elif cs.state.terminated is not None:
                    state = f"terminated:{cs.state.terminated.reason or ''}"
            containers.append(
                {
                    "name": cs.name,
                    "image": cs.image,
                    "ready": bool(cs.ready),
                    "restart_count": cs.restart_count,
                    "state": state,
                }
            )
        conditions = [
            {"type": c.type, "status": c.status, "reason": c.reason or ""}
            for c in (pod.status.conditions or [])
        ]
        spec_containers = getattr(pod.spec, "containers", []) or []
        spec_ports: dict[str, list[int]] = {
            container.name: [p.container_port for p in (getattr(container, "ports", None) or [])]
            for container in spec_containers
        }
        return {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "status": pod.status.phase or "Unknown",
            "node": pod.spec.node_name or "",
            "start_time": (
                pod.status.start_time.isoformat() if pod.status.start_time else None
            ),
            "labels": dict(pod.metadata.labels or {}),
            "containers": [
                {**container, "ports": spec_ports.get(str(container.get("name", "")), [])}
                for container in containers
            ],
            "conditions": conditions,
        }

    def _real_list_events(
        self, namespace: str, pod_name: str | None
    ) -> list[dict[str, JSONValue]]:
        api = self._get_core_api()
        events = api.list_namespaced_event(  # type: ignore[attr-defined]
            namespace, _request_timeout=self.timeout
        ).items
        result: list[dict[str, JSONValue]] = []
        for ev in events:
            involved = ev.involved_object.name if ev.involved_object else None
            if pod_name is not None and involved != pod_name:
                continue
            result.append(
                {
                    "pod": involved or "",
                    "type": ev.type or "Normal",
                    "reason": ev.reason or "",
                    "message": ev.message or "",
                    "count": ev.count or 1,
                }
            )
        return result

    def _real_get_pod_logs(
        self,
        namespace: str,
        name: str,
        container: str | None,
        tail_lines: int,
    ) -> str:
        api = self._get_core_api()
        logs = api.read_namespaced_pod_log(  # type: ignore[attr-defined]
            name,
            namespace,
            container=container,
            tail_lines=tail_lines,
            _request_timeout=self.timeout,
        )
        return cast(str, logs)

    def _real_list_namespaces(self) -> list[dict[str, JSONValue]]:
        api = self._get_core_api()
        namespaces = api.list_namespace(  # type: ignore[attr-defined]
            _request_timeout=self.timeout
        ).items
        return [
            {
                "name": ns.metadata.name,
                "status": ns.status.phase or "Active",
            }
            for ns in namespaces
        ]

    def _real_list_deployments(
        self, namespace: str
    ) -> list[dict[str, JSONValue]]:
        api = self._get_apps_api()
        deployments = api.list_namespaced_deployment(  # type: ignore[attr-defined]
            namespace, _request_timeout=self.timeout
        ).items
        result: list[dict[str, JSONValue]] = []
        for dep in deployments:
            desired = dep.spec.replicas if dep.spec.replicas is not None else 0
            status = dep.status
            ready = status.ready_replicas or 0
            available = status.available_replicas or 0
            updated = status.updated_replicas or 0
            result.append(
                {
                    "name": dep.metadata.name,
                    "namespace": dep.metadata.namespace,
                    "desired": desired,
                    "ready": ready,
                    "available": available,
                    "updated": updated,
                    "healthy": ready == desired and desired > 0,
                }
            )
        return result

    def _real_list_services(self, namespace: str) -> list[dict[str, JSONValue]]:
        api = self._get_core_api()
        services = api.list_namespaced_service(  # type: ignore[attr-defined]
            namespace, _request_timeout=self.timeout
        ).items
        result: list[dict[str, JSONValue]] = []
        for svc in services:
            spec = svc.spec
            ports: list[dict[str, JSONValue]] = [
                {
                    "port": p.port,
                    "target_port": str(p.target_port) if p.target_port is not None else "",
                    "protocol": p.protocol or "TCP",
                }
                for p in (spec.ports or [])
            ]
            result.append(
                {
                    "name": svc.metadata.name,
                    "namespace": svc.metadata.namespace,
                    "type": spec.type or "ClusterIP",
                    "cluster_ip": spec.cluster_ip or "",
                    "selector": dict(spec.selector or {}),
                    "ports": ports,
                }
            )
        return result

    def _real_list_endpoints(self, namespace: str) -> list[dict[str, JSONValue]]:
        api = self._get_core_api()
        endpoints = api.list_namespaced_endpoints(  # type: ignore[attr-defined]
            namespace, _request_timeout=self.timeout
        ).items
        result: list[dict[str, JSONValue]] = []
        for endpoint in endpoints:
            addresses: list[str] = []
            ports: list[int] = []
            for subset in endpoint.subsets or []:
                addresses.extend([addr.ip for addr in (subset.addresses or [])])
                ports.extend([port.port for port in (subset.ports or [])])
            result.append(
                {
                    "name": endpoint.metadata.name,
                    "namespace": endpoint.metadata.namespace,
                    "addresses": addresses,
                    "ports": ports,
                }
            )
        return result

    def _real_get_node_status(self) -> list[dict[str, JSONValue]]:
        api = self._get_core_api()
        nodes = api.list_node(  # type: ignore[attr-defined]
            _request_timeout=self.timeout
        ).items
        result: list[dict[str, JSONValue]] = []
        for node in nodes:
            conditions = node.status.conditions or []
            ready = any(
                c.type == "Ready" and c.status == "True" for c in conditions
            )
            # 压力类 condition 为 True 表示节点资源紧张，只保留告警项供上层判断
            pressure = [
                c.type
                for c in conditions
                if c.type != "Ready" and c.status == "True"
            ]
            allocatable = dict(node.status.allocatable or {})
            result.append(
                {
                    "name": node.metadata.name,
                    "ready": ready,
                    "pressure": pressure,
                    "allocatable": {
                        "cpu": str(allocatable.get("cpu", "")),
                        "memory": str(allocatable.get("memory", "")),
                    },
                    "kubelet_version": node.status.node_info.kubelet_version
                    if node.status.node_info
                    else "",
                }
            )
        return result
