"""
📦 模块名称：Kubernetes 只读诊断客户端
📍 架构位置：CloudOps 工具层，位于工具注册入口和 Kubernetes 官方 Python SDK 之间。
🎯 核心作用：提供 list pods、describe pod、list events、pod logs、list namespaces 五类只读能力，
           并统一处理命名空间白名单校验、超时与“连接失败自动降级 mock”。
🔗 依赖关系：可选依赖 kubernetes 官方客户端（未安装/无 kubeconfig 时降级 Mock）；
           被 register_k8s_readonly_tools 调用，从 AthenaSettings.ops 读取配置。
💡 设计思路：使用“适配器 + 降级 Mock”模式——real 模式优先真集群，任何异常都降级到演示数据；
           白名单校验属于安全边界，无论 mock/real 都强制生效且不降级（越权直接报错）。
📚 学习重点：
   1. 为什么白名单校验先于 mock/real 分流——安全边界必须最先生效。
   2. _run_with_fallback 如何用一个方法统一“真实优先 + 异常降级”骨架。
   3. core_api 依赖注入如何让单元测试无需真实集群即可覆盖 real 分支与异常分支。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar, cast

from athena.exceptions import ErrorCode, OpsError
from athena.types import JSONValue

if TYPE_CHECKING:  # 仅类型检查期引入，运行时不强依赖 kubernetes/AthenaSettings
    from athena.config import AthenaSettings

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_VALID_MODES = ("mock", "real")


class K8sReadOnlyClient:
    """
    Kubernetes 只读诊断客户端门面。

    功能说明：提供 Pod 列表、Pod 描述、事件、日志和命名空间列表五类只读能力。
    参数说明：
        mode：mock=始终演示数据；real=优先真集群，缺 kubeconfig/连接失败自动降级 mock。
        kubeconfig：kubeconfig 路径，None 时用 SDK 默认查找。
        context：kubeconfig context 名称，None 时用当前默认 context。
        namespace_allowlist：命名空间白名单；为空表示不限制。
        timeout：单次 API 调用超时（秒）。
        core_api：可注入的 CoreV1Api（真实或测试替身），None 时惰性构建。
    返回值：各方法返回 JSON 友好的 dict/list。
    设计思路：门面模式隔离真实 SDK，业务层不关心底层是 Mock 还是真集群。
    使用示例：K8sReadOnlyClient(mode="mock").list_pods("default")

    🎯 面试考点：为什么用普通 class 而不是 frozen dataclass？
    答：需要缓存惰性构建的 core_api，普通 class 的可变属性更合适，也便于依赖注入。
    """

    def __init__(
        self,
        *,
        mode: str = "mock",
        kubeconfig: str | None = None,
        context: str | None = None,
        namespace_allowlist: list[str] | tuple[str, ...] | None = None,
        timeout: float = 10.0,
        strict_real: bool = False,
        core_api: object | None = None,
        apps_api: object | None = None,
    ) -> None:
        """
        初始化只读客户端。

        功能说明：保存连接参数与安全边界，并缓存可选注入的 core_api / apps_api。
        参数说明：见类文档。core_api / apps_api 主要用于真实注入或单元测试替身。
        返回值：None。
        设计思路：mode 归一化并校验，非法值直接报错（快速失败），避免运行期歧义。
        使用示例：K8sReadOnlyClient(mode="real", namespace_allowlist=["default"])
        """
        normalized = (mode or "mock").strip().lower()
        if normalized not in _VALID_MODES:
            raise OpsError(
                ErrorCode.CONFIG_INVALID,
                f"ops.mode must be one of {_VALID_MODES}, got: {mode!r}",
            )
        self.mode = normalized
        self.kubeconfig = kubeconfig
        self.context = context
        # 白名单用 tuple 存储：不可变、可安全共享，语义上也表达“配置快照”
        self.namespace_allowlist: tuple[str, ...] = tuple(namespace_allowlist or ())
        self.timeout = timeout
        # strict_real=True 时 real 调用失败不降级，直接抛错暴露真实故障（生产建议）。
        self.strict_real = strict_real
        # 记录最近一次 real 调用是否发生降级，供上层（前端云状态卡片）展示。
        self.last_call_degraded = False
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
        设计思路：把“配置 → 客户端”的映射集中在一处，调用方无需了解字段细节。
        使用示例：K8sReadOnlyClient.from_settings(load_settings())
        """
        ops = settings.ops
        return cls(
            mode=ops.mode,
            kubeconfig=ops.kubernetes.kubeconfig,
            context=ops.kubernetes.context,
            namespace_allowlist=ops.kubernetes.namespace_allowlist,
            timeout=ops.kubernetes.timeout,
            strict_real=ops.strict_real,
        )

    # ------------------------------------------------------------------
    # 安全边界：命名空间白名单校验（mock/real 都强制生效，越权不降级）
    # ------------------------------------------------------------------
    def _ensure_namespace_allowed(self, namespace: str) -> None:
        """
        校验命名空间是否在白名单内。

        功能说明：白名单非空时，只有名单内的命名空间可访问，否则抛 OpsError。
        参数说明：namespace 是待访问的命名空间。
        返回值：None（通过校验）。
        设计思路：安全校验先于 mock/real 分流，保证越权访问在任何模式都被拦截；
            越权是配置/调用错误而非集群故障，因此直接报错、不降级 mock。
        使用示例：self._ensure_namespace_allowed("prod")

        🎯 面试考点：为什么越权不走自动降级？答案：降级是为了容忍“集群不可用”，
        而越权是安全边界问题，静默降级会掩盖违规，必须显式失败。
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
    # 真实 SDK 接入：惰性构建 CoreV1Api（缺失/失败由公开方法降级）
    # ------------------------------------------------------------------
    def _ensure_kube_config_loaded(self) -> None:
        """加载 kubeconfig（集群内优先，其次本地文件），只加载一次，core/apps 共享。"""
        if self._kube_config_loaded:
            return
        from kubernetes import config  # 延迟导入，未装不影响 mock 模式

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
        from kubernetes import client  # 延迟导入，未装不影响 mock 模式

        self._ensure_kube_config_loaded()
        self._core_api = client.CoreV1Api()
        return self._core_api

    def _get_apps_api(self) -> object:
        """惰性加载 kubernetes AppsV1Api（deployments 等），复用同一 kubeconfig 加载。"""
        if self._apps_api is not None:
            return self._apps_api
        from kubernetes import client  # 延迟导入，未装不影响 mock 模式

        self._ensure_kube_config_loaded()
        self._apps_api = client.AppsV1Api()
        return self._apps_api

    def _run_with_fallback(
        self, real_fn: Callable[[], _T], mock_fn: Callable[[], _T]
    ) -> _T:
        """
        统一“真实优先 + 异常降级”执行骨架。

        功能说明：mock 模式直接跑 mock_fn；real 模式先试 real_fn，异常时按 strict_real 决定
            抛错还是降级 mock_fn，并更新 last_call_degraded 标记供上层展示。
        参数说明：real_fn 真实调用；mock_fn 演示数据回退。
        返回值：real_fn 或 mock_fn 的结果。
        设计思路：把降级逻辑收敛到一处，公开方法都复用，避免重复 try/except；
            strict_real=True 时不静默降级，避免生产上真实故障被 mock 数据掩盖。
        使用示例：见 list_pods。
        """
        if self.mode != "real":
            return mock_fn()
        try:
            result = real_fn()
            self.last_call_degraded = False
            return result
        except Exception as exc:  # 包含 ImportError（未装 SDK）、连接失败、API 错误
            if self.strict_real:
                # 严格模式：不降级，抛错暴露真实连接/调用故障
                raise OpsError(
                    ErrorCode.OPS_REAL_UNAVAILABLE,
                    f"k8s real call failed and strict_real is enabled: {exc}",
                ) from exc
            logger.warning(
                "k8s real call failed, falling back to mock: %s", exc
            )
            self.last_call_degraded = True
            return mock_fn()

    # ==================================================================
    # 公开只读能力
    # ==================================================================
    def list_pods(self, namespace: str = "default") -> list[dict[str, JSONValue]]:
        """
        列出命名空间下的 Pod 状态快照。

        功能说明：real 模式查询集群 Pod，mock/失败降级返回典型故障 Pod。
        参数说明：namespace 目标命名空间（需通过白名单校验）。
        返回值：Pod 字典列表（name/namespace/status/restarts/node/ready）。
        设计思路：Mock 覆盖 CrashLoopBackOff/ImagePullBackOff 常见故障，便于诊断演示。
        使用示例：client.list_pods("default")
        """
        self._ensure_namespace_allowed(namespace)
        return self._run_with_fallback(
            lambda: self._real_list_pods(namespace),
            lambda: self._mock_list_pods(namespace),
        )

    def describe_pod(
        self, namespace: str, name: str
    ) -> dict[str, JSONValue]:
        """
        获取单个 Pod 的详细描述（近似 kubectl describe pod）。

        功能说明：real 模式读取 Pod 详情，mock/失败降级返回演示描述。
        参数说明：namespace 命名空间；name Pod 名称。
        返回值：包含 status/node/containers/conditions 的字典。
        设计思路：结构化返回容器状态与 conditions，供上层做根因判断。
        使用示例：client.describe_pod("default", "checkout-5f8b")
        """
        self._ensure_namespace_allowed(namespace)
        if not isinstance(name, str) or not name.strip():
            raise OpsError(
                ErrorCode.OPS_NAMESPACE_FORBIDDEN, "pod name must be non-empty"
            )
        return self._run_with_fallback(
            lambda: self._real_describe_pod(namespace, name),
            lambda: self._mock_describe_pod(namespace, name),
        )

    def list_events(
        self, namespace: str = "default", pod_name: str | None = None
    ) -> list[dict[str, JSONValue]]:
        """
        列出命名空间事件，可按 Pod 过滤。

        功能说明：real 模式查询集群事件，mock/失败降级返回典型事件。
        参数说明：namespace 命名空间；pod_name 为空返回全部，否则只返回该 Pod 事件。
        返回值：事件字典列表（pod/type/reason/message/count）。
        设计思路：事件常比状态更接近根因，过滤逻辑放在客户端层输出干净数据。
        使用示例：client.list_events("default", "checkout-5f8b")
        """
        self._ensure_namespace_allowed(namespace)
        return self._run_with_fallback(
            lambda: self._real_list_events(namespace, pod_name),
            lambda: self._mock_list_events(namespace, pod_name),
        )

    def get_pod_logs(
        self,
        namespace: str,
        name: str,
        container: str | None = None,
        tail_lines: int = 100,
    ) -> str:
        """
        获取 Pod 日志尾部内容。

        功能说明：real 模式读取容器日志，mock/失败降级返回演示日志。
        参数说明：namespace 命名空间；name Pod 名；container 容器名（多容器时指定）；
            tail_lines 尾部行数上限（正整数）。
        返回值：日志文本字符串。
        设计思路：默认只取尾部若干行，避免海量日志拖垮 Agent 上下文。
        使用示例：client.get_pod_logs("default", "checkout-5f8b", tail_lines=50)
        """
        self._ensure_namespace_allowed(namespace)
        if not isinstance(name, str) or not name.strip():
            raise OpsError(
                ErrorCode.OPS_NAMESPACE_FORBIDDEN, "pod name must be non-empty"
            )
        if tail_lines <= 0:
            raise ValueError("tail_lines must be positive")
        return self._run_with_fallback(
            lambda: self._real_get_pod_logs(namespace, name, container, tail_lines),
            lambda: self._mock_get_pod_logs(namespace, name, container, tail_lines),
        )

    def list_namespaces(self) -> list[dict[str, JSONValue]]:
        """
        列出集群命名空间。

        功能说明：real 模式查询集群命名空间，mock/失败降级返回演示命名空间；
            白名单非空时只返回名单内命名空间（尊重安全边界）。
        参数说明：无。
        返回值：命名空间字典列表（name/status）。
        设计思路：白名单过滤放在最终返回处，保证不越界暴露命名空间清单。
        使用示例：client.list_namespaces()
        """
        namespaces = self._run_with_fallback(
            self._real_list_namespaces,
            self._mock_list_namespaces,
        )
        if self.namespace_allowlist:
            namespaces = [
                ns
                for ns in namespaces
                if ns.get("name") in self.namespace_allowlist
            ]
        return namespaces

    def list_deployments(
        self, namespace: str = "default"
    ) -> list[dict[str, JSONValue]]:
        """
        列出命名空间下的 Deployment 副本健康状态。

        功能说明：real 模式查询集群 Deployment，mock/失败降级返回演示数据。
        参数说明：namespace 目标命名空间（需通过白名单校验）。
        返回值：Deployment 字典列表（name/namespace/desired/ready/available/updated/healthy）。
        设计思路：desired 与 ready 不一致往往意味着滚动发布卡住或副本不可用，是巡检重点。
        使用示例：client.list_deployments("default")
        """
        self._ensure_namespace_allowed(namespace)
        return self._run_with_fallback(
            lambda: self._real_list_deployments(namespace),
            lambda: self._mock_list_deployments(namespace),
        )

    def list_services(
        self, namespace: str = "default"
    ) -> list[dict[str, JSONValue]]:
        """
        列出命名空间下的 Service 及其选择器/端口。

        功能说明：real 模式查询集群 Service，mock/失败降级返回演示数据。
        参数说明：namespace 目标命名空间（需通过白名单校验）。
        返回值：Service 字典列表（name/namespace/type/cluster_ip/selector/ports）。
        设计思路：selector 为空或与 Pod label 不匹配是“服务无法访问”的常见根因，先把原始数据取干净。
        使用示例：client.list_services("default")
        """
        self._ensure_namespace_allowed(namespace)
        return self._run_with_fallback(
            lambda: self._real_list_services(namespace),
            lambda: self._mock_list_services(namespace),
        )

    def list_endpoints(
        self, namespace: str = "default"
    ) -> list[dict[str, JSONValue]]:
        """
        列出命名空间下的 Endpoints 地址快照。

        功能说明：real 模式查询集群 Endpoints，mock/失败降级返回演示数据。
        参数说明：namespace 目标命名空间（需通过白名单校验）。
        返回值：Endpoints 字典列表（name/namespace/addresses/ports）。
        设计思路：Service 有 selector 但 endpoints 为空，是“服务无法访问”的关键证据。
        使用示例：client.list_endpoints("default")
        """
        self._ensure_namespace_allowed(namespace)
        return self._run_with_fallback(
            lambda: self._real_list_endpoints(namespace),
            lambda: self._mock_list_endpoints(namespace),
        )

    def get_node_status(self) -> list[dict[str, JSONValue]]:
        """
        获取集群节点健康状态（集群级只读，不限命名空间）。

        功能说明：real 模式查询节点 Ready/压力状态与可分配资源，mock/失败降级返回演示数据。
        参数说明：无（节点是集群级资源）。
        返回值：节点字典列表（name/ready/pressure/allocatable/kubelet_version）。
        设计思路：节点 NotReady 或资源压力会连带影响其上所有 Pod，是自上而下排障的关键一环。
        使用示例：client.get_node_status()

        🎯 面试考点：为什么节点查询不做命名空间白名单？答案：Node 是集群级资源、不属于任何
        命名空间，白名单是命名空间边界，对节点不适用；节点只读本身不泄露业务命名空间数据。
        """
        return self._run_with_fallback(
            self._real_get_node_status,
            self._mock_get_node_status,
        )

    # ==================================================================
    # 真实 SDK 实现（任何异常由 _run_with_fallback 捕获降级）
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

    # ==================================================================
    # Mock 实现（演示数据 + 降级目标）
    # ==================================================================
    def _mock_list_pods(self, namespace: str) -> list[dict[str, JSONValue]]:
        return [
            {
                "name": "api-7d9c",
                "namespace": namespace,
                "status": "Running",
                "restarts": 0,
                "node": "node-a",
                "ready": True,
                "labels": {"app": "api"},
            },
            {
                "name": "checkout-5f8b",
                "namespace": namespace,
                "status": "CrashLoopBackOff",
                "restarts": 7,
                "node": "node-b",
                "ready": False,
                "labels": {"app": "checkout"},
            },
            {
                "name": "image-worker-22a",
                "namespace": namespace,
                "status": "ImagePullBackOff",
                "restarts": 0,
                "node": "node-b",
                "ready": False,
                "labels": {"app": "image-worker"},
            },
        ]

    def _mock_describe_pod(
        self, namespace: str, name: str
    ) -> dict[str, JSONValue]:
        return {
            "name": name,
            "namespace": namespace,
            "status": "CrashLoopBackOff",
            "node": "node-b",
            "start_time": "2024-01-01T00:00:00+00:00",
            "labels": {"app": name.split("-")[0]},
            "containers": [
                {
                    "name": name.split("-")[0],
                    "image": "registry/demo:latest",
                    "ready": False,
                    "restart_count": 7,
                    "state": "waiting:CrashLoopBackOff",
                    "ports": [8080] if name.startswith("api") else [9090],
                }
            ],
            "conditions": [
                {"type": "Ready", "status": "False", "reason": "ContainersNotReady"}
            ],
        }

    def _mock_list_events(
        self, namespace: str, pod_name: str | None
    ) -> list[dict[str, JSONValue]]:
        events: list[dict[str, JSONValue]] = [
            {
                "pod": "checkout-5f8b",
                "type": "Warning",
                "reason": "BackOff",
                "message": "Back-off restarting failed container",
                "count": 12,
            },
            {
                "pod": "image-worker-22a",
                "type": "Warning",
                "reason": "Failed",
                "message": "Failed to pull image registry/demo:missing",
                "count": 5,
            },
            {
                "pod": "api-7d9c",
                "type": "Normal",
                "reason": "Pulled",
                "message": "Container image already present",
                "count": 1,
            },
        ]
        return [
            event
            for event in events
            if pod_name is None or event["pod"] == pod_name
        ]

    def _mock_get_pod_logs(
        self,
        namespace: str,
        name: str,
        container: str | None,
        tail_lines: int,
    ) -> str:
        lines = [
            f"[mock][{namespace}/{name}] starting container "
            f"{container or 'main'}",
            "[mock] connecting to database ...",
            "[mock] ERROR: connection refused (10.0.0.5:5432)",
            "[mock] panic: failed to initialize, exiting",
        ]
        return "\n".join(lines[-tail_lines:])

    def _mock_list_namespaces(self) -> list[dict[str, JSONValue]]:
        return [
            {"name": "default", "status": "Active"},
            {"name": "kube-system", "status": "Active"},
            {"name": "prod", "status": "Active"},
        ]

    def _mock_list_deployments(
        self, namespace: str
    ) -> list[dict[str, JSONValue]]:
        return [
            {
                "name": "api",
                "namespace": namespace,
                "desired": 3,
                "ready": 3,
                "available": 3,
                "updated": 3,
                "healthy": True,
            },
            {
                "name": "checkout",
                "namespace": namespace,
                "desired": 2,
                "ready": 0,
                "available": 0,
                "updated": 2,
                "healthy": False,
            },
        ]

    def _mock_list_services(self, namespace: str) -> list[dict[str, JSONValue]]:
        return [
            {
                "name": "api",
                "namespace": namespace,
                "type": "ClusterIP",
                "cluster_ip": "10.96.0.10",
                "selector": {"app": "api"},
                "ports": [
                    {"port": 80, "target_port": "8080", "protocol": "TCP"}
                ],
            },
            {
                "name": "checkout",
                "namespace": namespace,
                "type": "ClusterIP",
                "cluster_ip": "10.96.0.20",
                "selector": {},  # 选择器为空：典型“Service 无法访问”隐患
                "ports": [
                    {"port": 80, "target_port": "9090", "protocol": "TCP"}
                ],
            },
        ]

    def _mock_list_endpoints(self, namespace: str) -> list[dict[str, JSONValue]]:
        return [
            {
                "name": "api",
                "namespace": namespace,
                "addresses": ["10.244.0.10"],
                "ports": [8080],
            },
            {
                "name": "checkout",
                "namespace": namespace,
                "addresses": [],
                "ports": [],
            },
        ]

    def _mock_get_node_status(self) -> list[dict[str, JSONValue]]:
        return [
            {
                "name": "node-a",
                "ready": True,
                "pressure": [],
                "allocatable": {"cpu": "4", "memory": "8Gi"},
                "kubelet_version": "v1.29.0",
            },
            {
                "name": "node-b",
                "ready": True,
                "pressure": ["MemoryPressure"],
                "allocatable": {"cpu": "4", "memory": "8Gi"},
                "kubelet_version": "v1.29.0",
            },
        ]
