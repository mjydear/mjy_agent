"""
📦 模块名称：Kubernetes 只读诊断分析器
📍 架构位置：CloudOps 诊断层，位于 K8sReadOnlyClient 原始只读数据和 Agent/工作流之间。
🎯 核心作用：把 Pod 状态 + 事件 + 日志尾部聚合成结构化根因、严重级别、修复建议与证据。
🔗 依赖关系：依赖 K8sReadOnlyClient（真实集群 + 白名单）；被工具注册入口调用。
💡 设计思路：使用确定性 SOP 规则，让常见故障在无 LLM 时也能稳定、可解释地诊断；
           证据（evidence）随每条结论一起返回，便于人工复核与写入知识库/trace。
📚 学习重点：
   1. 为什么把事件先按 Pod 建索引——避免每个 Pod 都遍历全部事件（O(N*M)→O(N+M)）。
   2. 为什么仅对疑似崩溃的 Pod 拉日志——控制真实集群调用量与 Agent 上下文体积。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from athena.tools.cloud.k8s.client import K8sReadOnlyClient
from athena.tools.cloud.k8s.report import OpsDiagnosisReport, OpsFinding
from athena.tools.cloud.prometheus import PrometheusQueryClient, PrometheusQueryResult
from athena.types import JSONValue

if TYPE_CHECKING:
    from athena.config import AthenaSettings

# 触发“拉日志辅助定位”的崩溃类状态
_CRASH_STATUSES = frozenset({"CrashLoopBackOff", "Error"})
# 镜像拉取失败类状态
_IMAGE_STATUSES = frozenset({"ImagePullBackOff", "ErrImagePull"})
# Running 但频繁重启的告警阈值
_RESTART_WARN_THRESHOLD = 5
_CPU_WARN_CORES = 0.85
_MEMORY_WARN_BYTES = 800 * 1024 * 1024


@dataclass(frozen=True)
class K8sFinding:
    """
    一条只读诊断结论。

    功能说明：保存故障对象、症状、根因、修复建议、严重级别与支撑证据。
    参数说明：
        namespace/pod：定位对象；symptom：可观察症状（通常是 Pod 状态）；
        root_cause：推断根因；recommendation：可执行建议；severity：high/medium/low；
        evidence：支撑该结论的证据行（事件消息、日志尾部片段等）。
    返回值：数据容器。
    设计思路：结构化结论既能展示给人，也能 JSON 序列化写入 trace/知识库。
    使用示例：K8sFinding("default", "checkout-5f8b", "CrashLoopBackOff", ...)
    """

    namespace: str
    pod: str
    symptom: str
    root_cause: str
    recommendation: str
    severity: str
    evidence: list[str] = field(default_factory=list)


class K8sReadOnlyDiagnoser:
    """
    基于只读客户端的 Kubernetes 故障诊断器。

    功能说明：聚合 Pod 状态、事件与日志，输出一组可解释的诊断结论。
    参数说明：client 是 K8sReadOnlyClient（真实集群，测试可注入 core_api/apps_api 替身）。
    返回值：diagnose_namespace() 返回 K8sFinding 列表。
    设计思路：诊断只消费只读数据，绝不产生写操作；规则化 SOP 保证可测试、可复现。
    使用示例：K8sReadOnlyDiagnoser.from_settings(load_settings()).diagnose_namespace("default")
    """

    def __init__(
        self,
        client: K8sReadOnlyClient | None = None,
        prometheus: PrometheusQueryClient | None = None,
    ) -> None:
        """
        初始化诊断器。

        功能说明：保存只读客户端，不传时使用默认真实客户端（SDK 默认查找 kubeconfig）。
        参数说明：client 是只读 K8s 数据来源。
        返回值：None。
        设计思路：依赖注入让测试无需真实集群即可覆盖各分支。
        使用示例：K8sReadOnlyDiagnoser(K8sReadOnlyClient.from_settings(settings))
        """
        self.client = client or K8sReadOnlyClient()
        self.prometheus = prometheus or PrometheusQueryClient()

    @classmethod
    def from_settings(cls, settings: AthenaSettings) -> K8sReadOnlyDiagnoser:
        """从 AthenaSettings 构造诊断器（复用客户端装配逻辑）。"""
        return cls(
            K8sReadOnlyClient.from_settings(settings),
            PrometheusQueryClient.from_settings(settings),
        )

    def diagnose_namespace(
        self,
        namespace: str = "default",
        include_logs: bool = True,
        log_tail_lines: int = 20,
    ) -> list[K8sFinding]:
        """
        诊断单个命名空间的 Pod 故障。

        功能说明：读取 Pod + 事件（必要时拉崩溃 Pod 日志），按 SOP 输出诊断结论。
        参数说明：
            namespace：目标命名空间（经客户端白名单校验）。
            include_logs：是否为崩溃类 Pod 拉取日志尾部作为证据。
            log_tail_lines：日志尾部行数上限（正整数）。
        返回值：K8sFinding 列表，无异常时返回空列表。
        设计思路：状态是“症状”，事件/日志更接近“原因”；先建事件索引再逐 Pod 匹配。
        使用示例：diagnoser.diagnose_namespace("prod", include_logs=False)
        """
        if log_tail_lines <= 0:
            raise ValueError("log_tail_lines must be positive")

        events = self.client.list_events(namespace)
        events_by_pod: dict[str, list[dict[str, JSONValue]]] = {}
        for event in events:
            events_by_pod.setdefault(str(event.get("pod", "")), []).append(event)

        findings: list[K8sFinding] = []
        for pod in self.client.list_pods(namespace):
            name = str(pod.get("name", ""))
            status = str(pod.get("status", ""))
            restarts = int(pod.get("restarts", 0) or 0)
            pod_events = events_by_pod.get(name, [])
            event_evidence = [
                f"{ev.get('reason', '')}: {ev.get('message', '')}".strip(": ")
                for ev in pod_events
                if str(ev.get("type", "")) == "Warning"
            ]

            if status in _CRASH_STATUSES:
                evidence = list(event_evidence)
                if include_logs:
                    evidence.extend(
                        self._collect_log_evidence(namespace, name, log_tail_lines)
                    )
                findings.append(
                    K8sFinding(
                        namespace=namespace,
                        pod=name,
                        symptom=status,
                        root_cause="container process exits repeatedly",
                        recommendation=(
                            "inspect logs, verify env/config, then roll back last image"
                        ),
                        severity="high",
                        evidence=evidence,
                    )
                )
            elif status in _IMAGE_STATUSES:
                findings.append(
                    K8sFinding(
                        namespace=namespace,
                        pod=name,
                        symptom=status,
                        root_cause=(
                            event_evidence[0] if event_evidence else "image pull failed"
                        ),
                        recommendation=(
                            "check image tag, registry secret, and network policy"
                        ),
                        severity="medium",
                        evidence=event_evidence,
                    )
                )
            elif status == "Pending":
                findings.append(
                    K8sFinding(
                        namespace=namespace,
                        pod=name,
                        symptom=status,
                        root_cause=(
                            event_evidence[0]
                            if event_evidence
                            else "pod cannot be scheduled"
                        ),
                        recommendation=(
                            "check node resources, taints/tolerations and PVC binding"
                        ),
                        severity="medium",
                        evidence=event_evidence,
                    )
                )
            elif restarts >= _RESTART_WARN_THRESHOLD:
                # Running 但重启频繁：潜在不稳定，降级为 low 提醒
                findings.append(
                    K8sFinding(
                        namespace=namespace,
                        pod=name,
                        symptom="FrequentRestarts",
                        root_cause=f"pod restarted {restarts} times while Running",
                        recommendation=(
                            "review liveness/readiness probes and recent deployments"
                        ),
                        severity="low",
                        evidence=event_evidence,
                    )
                )
        return findings

    def _collect_log_evidence(
        self, namespace: str, pod: str, tail_lines: int
    ) -> list[str]:
        """拉取 Pod 日志尾部作为证据；失败不阻断诊断，仅记录一行占位。"""
        try:
            logs = self.client.get_pod_logs(namespace, pod, tail_lines=tail_lines)
        except Exception as exc:  # 诊断是尽力而为，日志缺失不应让整体失败
            return [f"log unavailable: {exc}"]
        return [f"log: {line}" for line in logs.splitlines() if line.strip()]

    def as_dicts(
        self,
        namespace: str = "default",
        include_logs: bool = True,
        log_tail_lines: int = 20,
    ) -> list[dict[str, JSONValue]]:
        """
        返回 JSON 友好的诊断结论列表。

        功能说明：把 K8sFinding 转成 dict，便于 API/工具/前端序列化。
        参数说明：同 diagnose_namespace。
        返回值：字典列表。
        设计思路：dataclass 适合内部逻辑，边界处统一转 dict 输出。
        使用示例：diagnoser.as_dicts("default")
        """
        return [
            asdict(finding)
            for finding in self.diagnose_namespace(
                namespace, include_logs, log_tail_lines
            )
        ]

    def build_report(
        self,
        namespace: str = "default",
        include_logs: bool = True,
        log_tail_lines: int = 20,
    ) -> OpsDiagnosisReport:
        """
        构建结构化诊断报告（阶段 2 数据契约）。

        功能说明：把 K8sFinding 列表升级为 OpsDiagnosisReport，附带 summary、metrics、
            去重后的整体建议 actions 与原始证据 raw_evidence，供 API 返回结构化 JSON。
        参数说明：同 diagnose_namespace。
        返回值：OpsDiagnosisReport。
        设计思路：每条 finding 强绑定证据；无 finding 时 summary 明确返回“证据不足/未发现异常”，
            从数据结构上抑制 LLM 编造根因。
        使用示例：diagnoser.build_report("default")

        🎯 面试考点：metrics 里为什么要按 severity 计数？答案：前端可据此“一眼看清”风险分布，
        也让后续告警分级、SLA 统计有结构化数据来源，而不是从自由文本里正则抠数字。
        """
        ops_findings = self.diagnose_ops_findings(
            namespace, include_logs, log_tail_lines
        )

        severity_counts: dict[str, JSONValue] = {"high": 0, "medium": 0, "low": 0}
        for finding in ops_findings:
            key = finding.severity if finding.severity in severity_counts else "low"
            severity_counts[key] = int(severity_counts[key]) + 1  # type: ignore[arg-type]

        # 整体建议：按出现顺序去重，保留优先动作，避免前端展示重复项。
        actions: list[str] = []
        for finding in ops_findings:
            for action in finding.recommended_actions:
                if action not in actions:
                    actions.append(action)

        raw_evidence: list[str] = []
        for finding in ops_findings:
            raw_evidence.extend(finding.evidence)
        prometheus_metrics = self.collect_prometheus_metrics(namespace, ops_findings)
        if prometheus_metrics:
            raw_evidence.extend(self._prometheus_evidence(prometheus_metrics))

        high = int(severity_counts["high"])  # type: ignore[arg-type]
        if not ops_findings:
            summary = f"命名空间 {namespace} 未发现异常（证据不足或工作负载健康）。"
        else:
            summary = (
                f"命名空间 {namespace} 发现 {len(ops_findings)} 个问题，"
                f"其中高危 {high} 个。"
            )

        metrics: dict[str, JSONValue] = {
            "finding_count": len(ops_findings),
            "severity_counts": severity_counts,
            "prometheus_metrics": [metric.to_dict() for metric in prometheus_metrics],
            "prometheus_available": any(metric.available for metric in prometheus_metrics),
        }
        return OpsDiagnosisReport(
            summary=summary,
            namespace=namespace,
            findings=ops_findings,
            metrics=metrics,
            actions=actions,
            raw_evidence=raw_evidence,
        )

    def diagnose_ops_findings(
        self,
        namespace: str = "default",
        include_logs: bool = True,
        log_tail_lines: int = 20,
    ) -> list[OpsFinding]:
        """
        运行阶段 3 K8s 高频故障 Playbook，输出统一 OpsFinding。

        功能说明：覆盖 CrashLoopBackOff、ImagePullBackOff、Pod Pending、Service 无法访问四类 SOP。
        参数说明：namespace/include_logs/log_tail_lines 同 diagnose_namespace。
        返回值：OpsFinding 列表。
        设计思路：Playbook 读取真实只读数据（Pod/describe/events/logs/services/endpoints/nodes），
            每条结论都带证据，供 API、Web 和 LLM 摘要统一消费。
        使用示例：findings = diagnoser.diagnose_ops_findings("default")
        """
        if log_tail_lines <= 0:
            raise ValueError("log_tail_lines must be positive")

        pods = self.client.list_pods(namespace)
        events = self.client.list_events(namespace)
        services = self.client.list_services(namespace)
        endpoints = self.client.list_endpoints(namespace)
        nodes = self.client.get_node_status()
        events_by_pod = self._events_by_pod(events)
        pod_by_name = {str(pod.get("name", "")): pod for pod in pods}
        findings: list[OpsFinding] = []

        for pod in pods:
            findings.extend(
                self._diagnose_pod_playbooks(
                    namespace,
                    pod,
                    events_by_pod.get(str(pod.get("name", "")), []),
                    nodes,
                    include_logs,
                    log_tail_lines,
                )
            )
        findings.extend(self._diagnose_resource_metric_playbook(namespace, pods))

        endpoints_by_name = {
            str(endpoint.get("name", "")): endpoint for endpoint in endpoints
        }
        for service in services:
            service_finding = self._diagnose_service_playbook(
                namespace, service, endpoints_by_name, pod_by_name
            )
            if service_finding is not None:
                findings.append(service_finding)
        return findings

    def _diagnose_resource_metric_playbook(
        self, namespace: str, pods: list[dict[str, JSONValue]]
    ) -> list[OpsFinding]:
        """运行 CPU / Memory 异常 Playbook（Prometheus 可用时输出指标证据）。"""
        findings: list[OpsFinding] = []
        for pod in pods:
            name = str(pod.get("name", ""))
            metrics = self.prometheus.pod_resource_snapshot(namespace, name)
            available = [metric for metric in metrics if metric.available]
            if not available:
                continue
            cpu = next((m for m in metrics if m.name == "pod_cpu_usage"), None)
            memory = next((m for m in metrics if m.name == "pod_memory_usage"), None)
            reasons: list[str] = []
            evidence = self._prometheus_evidence(metrics)
            if cpu and cpu.value is not None and cpu.value >= _CPU_WARN_CORES:
                reasons.append("CPU usage is above the warning threshold")
            if memory and memory.value is not None and memory.value >= _MEMORY_WARN_BYTES:
                reasons.append("memory working set is above the warning threshold")
            if not reasons:
                continue
            findings.append(
                OpsFinding(
                    severity="medium",
                    resource_kind="Pod",
                    resource_name=name,
                    namespace=namespace,
                    symptom="CPU / Memory abnormal",
                    evidence=evidence,
                    probable_causes=reasons,
                    recommended_actions=[
                        "inspect CPU throttling and memory working set trends",
                        "check resource requests/limits and recent traffic changes",
                        "review OOMKilled events and container memory pressure",
                    ],
                )
            )
        return findings

    def collect_prometheus_metrics(
        self, namespace: str, findings: list[OpsFinding]
    ) -> list[PrometheusQueryResult]:
        """
        基于当前诊断对象收集 Prometheus 指标。

        功能说明：Pod finding 查询 CPU/Memory/Restart，Service finding 查询 5xx/P95/Availability。
        参数说明：namespace 是诊断命名空间；findings 是已有 Playbook 结论。
        返回值：PrometheusQueryResult 列表；Prometheus 关闭时返回 unavailable 结果。
        设计思路：指标是辅助证据，不参与 K8s API 成败路径，Prometheus 不可用不影响报告生成。
        使用示例：metrics = diagnoser.collect_prometheus_metrics("default", findings)
        """
        metrics: list[PrometheusQueryResult] = []
        seen: set[tuple[str, str]] = set()
        for finding in findings:
            key = (finding.resource_kind, finding.resource_name)
            if key in seen:
                continue
            seen.add(key)
            if finding.resource_kind == "Pod":
                metrics.extend(
                    self.prometheus.pod_resource_snapshot(namespace, finding.resource_name)
                )
            elif finding.resource_kind == "Service":
                metrics.extend(
                    self.prometheus.service_snapshot(namespace, finding.resource_name)
                )
        return metrics

    def report_dict(
        self,
        namespace: str = "default",
        include_logs: bool = True,
        log_tail_lines: int = 20,
    ) -> dict[str, JSONValue]:
        """返回 JSON 友好的结构化诊断报告（build_report 的序列化版本）。"""
        return self.build_report(namespace, include_logs, log_tail_lines).to_dict()

    @staticmethod
    def _to_ops_finding(finding: K8sFinding) -> OpsFinding:
        """把内部 K8sFinding 映射为对外的结构化 OpsFinding（单值升级为列表）。"""
        return OpsFinding(
            severity=finding.severity,
            resource_kind="Pod",
            resource_name=finding.pod,
            namespace=finding.namespace,
            symptom=finding.symptom,
            evidence=list(finding.evidence),
            probable_causes=[finding.root_cause] if finding.root_cause else [],
            recommended_actions=(
                [finding.recommendation] if finding.recommendation else []
            ),
        )

    @staticmethod
    def _events_by_pod(
        events: list[dict[str, JSONValue]]
    ) -> dict[str, list[dict[str, JSONValue]]]:
        """按 Pod 名聚合事件，供 Playbook 快速查找相关证据。"""
        grouped: dict[str, list[dict[str, JSONValue]]] = {}
        for event in events:
            grouped.setdefault(str(event.get("pod", "")), []).append(event)
        return grouped

    def _diagnose_pod_playbooks(
        self,
        namespace: str,
        pod: dict[str, JSONValue],
        events: list[dict[str, JSONValue]],
        nodes: list[dict[str, JSONValue]],
        include_logs: bool,
        log_tail_lines: int,
    ) -> list[OpsFinding]:
        """运行 Pod 级 Playbook：CrashLoopBackOff、ImagePullBackOff、Pending。"""
        name = str(pod.get("name", ""))
        status = str(pod.get("status", ""))
        restarts = int(pod.get("restarts", 0) or 0)
        described = self.client.describe_pod(namespace, name)
        event_evidence = self._warning_event_evidence(events)
        findings: list[OpsFinding] = []

        if status in _CRASH_STATUSES:
            evidence = [
                f"pod_status={status}",
                f"restart_count={restarts}",
                *self._container_state_evidence(described),
                *event_evidence,
            ]
            if include_logs:
                evidence.extend(
                    self._collect_log_evidence(namespace, name, log_tail_lines)
                )
            findings.append(
                OpsFinding(
                    severity="high",
                    resource_kind="Pod",
                    resource_name=name,
                    namespace=namespace,
                    symptom="CrashLoopBackOff",
                    evidence=evidence,
                    probable_causes=[
                        "container process exits repeatedly",
                        "bad configuration, missing dependency, or failed startup command",
                    ],
                    recommended_actions=[
                        "inspect recent container logs",
                        "verify environment variables and mounted config",
                        "roll back the last image or deployment change if failure started after release",
                    ],
                )
            )
        elif status in _IMAGE_STATUSES:
            images = self._extract_images(described, event_evidence)
            evidence = [f"pod_status={status}", *event_evidence]
            if images:
                evidence.append(f"images={', '.join(images)}")
            findings.append(
                OpsFinding(
                    severity="medium",
                    resource_kind="Pod",
                    resource_name=name,
                    namespace=namespace,
                    symptom="ImagePullBackOff",
                    evidence=evidence,
                    probable_causes=[
                        "image tag does not exist or registry is unreachable",
                        "imagePullSecret is missing or invalid",
                    ],
                    recommended_actions=[
                        "verify image registry and tag",
                        "check imagePullSecret and registry credentials",
                        "confirm node network can reach the registry",
                    ],
                )
            )
        elif status == "Pending":
            node_pressure = self._node_pressure_evidence(nodes)
            evidence = [f"pod_status={status}", *event_evidence, *node_pressure]
            findings.append(
                OpsFinding(
                    severity="medium",
                    resource_kind="Pod",
                    resource_name=name,
                    namespace=namespace,
                    symptom="Pod Pending",
                    evidence=evidence,
                    probable_causes=[
                        "scheduler cannot place the pod",
                        "node resource pressure, taint/toleration mismatch, nodeSelector mismatch, or PVC not bound",
                    ],
                    recommended_actions=[
                        "inspect scheduling events",
                        "check node resources, taints/tolerations and nodeSelector",
                        "verify PVC binding status if the workload uses volumes",
                    ],
                )
            )
        return findings

    def _diagnose_service_playbook(
        self,
        namespace: str,
        service: dict[str, JSONValue],
        endpoints_by_name: dict[str, dict[str, JSONValue]],
        pod_by_name: dict[str, dict[str, JSONValue]],
    ) -> OpsFinding | None:
        """运行 Service 无法访问 Playbook：selector/endpoints/readiness/targetPort 交叉检查。"""
        name = str(service.get("name", ""))
        selector = service.get("selector", {})
        endpoint = endpoints_by_name.get(name, {})
        addresses = endpoint.get("addresses", []) if endpoint else []
        ports = service.get("ports", []) if isinstance(service.get("ports", []), list) else []
        evidence: list[str] = []
        causes: list[str] = []
        actions: list[str] = []

        if not isinstance(selector, dict) or not selector:
            evidence.append("service selector is empty")
            causes.append("service cannot select backend pods because selector is empty")
            actions.append("set service selector to match backend pod labels")
        matched_pods = self._pods_matching_selector(selector, pod_by_name)
        if isinstance(selector, dict) and selector and not matched_pods:
            evidence.append(f"selector={selector} matched_pods=0")
            causes.append("service selector does not match any pod labels")
            actions.append("align service selector with pod labels")
        if not addresses:
            evidence.append("endpoints addresses are empty")
            causes.append("service has no ready endpoints")
            actions.append("check pod readiness and endpoint controller state")

        target_ports = [str(port.get("target_port", "")) for port in ports if isinstance(port, dict)]
        backend_ports = self._backend_container_ports(namespace, matched_pods)
        if target_ports and backend_ports and not set(target_ports).intersection(backend_ports):
            evidence.append(
                f"target_ports={target_ports} backend_container_ports={sorted(backend_ports)}"
            )
            causes.append("service targetPort does not match backend containerPort")
            actions.append("align service targetPort with backend containerPort")

        if not causes:
            return None
        return OpsFinding(
            severity="medium",
            resource_kind="Service",
            resource_name=name,
            namespace=namespace,
            symptom="Service unreachable",
            evidence=evidence,
            probable_causes=causes,
            recommended_actions=list(dict.fromkeys(actions)),
        )

    @staticmethod
    def _warning_event_evidence(events: list[dict[str, JSONValue]]) -> list[str]:
        """提取 Warning 事件作为 Playbook 证据。"""
        return [
            f"{ev.get('reason', '')}: {ev.get('message', '')}".strip(": ")
            for ev in events
            if str(ev.get("type", "")) == "Warning"
        ]

    @staticmethod
    def _container_state_evidence(described: dict[str, JSONValue]) -> list[str]:
        """提取容器 state/restart/image 证据。"""
        containers = described.get("containers", [])
        if not isinstance(containers, list):
            return []
        evidence: list[str] = []
        for container in containers:
            if not isinstance(container, dict):
                continue
            evidence.append(
                "container="
                f"{container.get('name', '')} state={container.get('state', '')} "
                f"restart_count={container.get('restart_count', 0)} "
                f"image={container.get('image', '')}"
            )
        return evidence

    @staticmethod
    def _extract_images(
        described: dict[str, JSONValue], evidence: list[str]
    ) -> list[str]:
        """从 describe 和事件消息中提取镜像名。"""
        images: list[str] = []
        containers = described.get("containers", [])
        if isinstance(containers, list):
            for container in containers:
                if isinstance(container, dict) and container.get("image"):
                    images.append(str(container["image"]))
        for item in evidence:
            for match in re.findall(r"[\w.-]+/[\w./:-]+", item):
                if match not in images:
                    images.append(match)
        return images

    @staticmethod
    def _node_pressure_evidence(nodes: list[dict[str, JSONValue]]) -> list[str]:
        """提取节点资源压力证据。"""
        evidence: list[str] = []
        for node in nodes:
            pressure = node.get("pressure", [])
            if pressure:
                evidence.append(f"node={node.get('name', '')} pressure={pressure}")
        return evidence

    @staticmethod
    def _pods_matching_selector(
        selector: object, pod_by_name: dict[str, dict[str, JSONValue]]
    ) -> list[dict[str, JSONValue]]:
        """根据 Service selector 匹配 Pod labels。"""
        if not isinstance(selector, dict) or not selector:
            return []
        matched: list[dict[str, JSONValue]] = []
        for pod in pod_by_name.values():
            labels = pod.get("labels", {})
            if isinstance(labels, dict) and all(labels.get(k) == v for k, v in selector.items()):
                matched.append(pod)
        return matched

    def _backend_container_ports(
        self, namespace: str, pods: list[dict[str, JSONValue]]
    ) -> set[str]:
        """读取匹配 Pod 的 containerPort 集合。"""
        ports: set[str] = set()
        for pod in pods[:5]:
            name = str(pod.get("name", ""))
            described = self.client.describe_pod(namespace, name)
            containers = described.get("containers", [])
            if not isinstance(containers, list):
                continue
            for container in containers:
                if isinstance(container, dict):
                    ports.update(str(port) for port in container.get("ports", []) or [])
        return ports

    @staticmethod
    def _prometheus_evidence(metrics: list[PrometheusQueryResult]) -> list[str]:
        """把 Prometheus 指标结果转为可读证据行。"""
        evidence: list[str] = []
        for metric in metrics:
            if metric.available:
                evidence.append(
                    f"prometheus:{metric.name}={metric.value} {metric.unit}".strip()
                )
            else:
                evidence.append(f"prometheus:{metric.name} unavailable: {metric.error}")
        return evidence

