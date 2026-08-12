"""Unit tests for the read-only Kubernetes diagnostics client and its tools.

覆盖点：
- mock 模式返回演示数据；
- real 模式经注入的假 CoreV1Api 返回真实转换结果；
- real 模式下 SDK 抛异常自动降级 mock（无 kubeconfig/连接失败场景）；
- 命名空间白名单越权在 mock/real 都硬失败（不降级）；
- list_namespaces 按白名单过滤；
- 非法 mode/空 Pod 名/非正 tail_lines 的边界校验；
- from_settings 装配与工具注册 + invoke 端到端。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from athena.config import AthenaSettings, K8sSettings, OpsSettings
from athena.exceptions import ErrorCode, OpsError
from athena.tools import ToolCall, ToolRegistry
from athena.tools.cloud.k8s import (
    EvidenceBoundReportSummarizer,
    K8sReadOnlyClient,
    K8sReadOnlyDiagnoser,
    K8sActionSecurityPolicy,
    K8sWriteActionExecutor,
    register_k8s_readonly_tools,
)
from athena.tools.cloud.k8s.summarizer import messages_contain_only_report
from athena.tools.cloud.prometheus import PrometheusQueryClient


# ---------------------------------------------------------------------------
# 假 CoreV1Api：用 SimpleNamespace 模拟 kubernetes SDK 返回的对象结构
# ---------------------------------------------------------------------------
def _make_pod(name: str = "checkout-5f8b") -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name, namespace="default", labels={"app": "checkout"}
        ),
        spec=SimpleNamespace(node_name="node-b"),
        status=SimpleNamespace(
            phase="Running",
            start_time=SimpleNamespace(isoformat=lambda: "2024-01-01T00:00:00+00:00"),
            container_statuses=[
                SimpleNamespace(
                    name="checkout",
                    image="registry/demo:latest",
                    ready=True,
                    restart_count=3,
                    state=SimpleNamespace(
                        running=SimpleNamespace(),
                        waiting=None,
                        terminated=None,
                    ),
                )
            ],
            conditions=[SimpleNamespace(type="Ready", status="True", reason=None)],
        ),
    )


class FakeCoreApi:
    """真实分支的成功替身。"""

    def list_namespaced_pod(self, namespace, _request_timeout=None):  # noqa: ANN001
        return SimpleNamespace(items=[_make_pod()])

    def read_namespaced_pod(
        self, name, namespace, _request_timeout=None
    ):  # noqa: ANN001
        return _make_pod(name)

    def list_namespaced_event(self, namespace, _request_timeout=None):  # noqa: ANN001
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    involved_object=SimpleNamespace(name="checkout-5f8b"),
                    type="Warning",
                    reason="BackOff",
                    message="Back-off restarting failed container",
                    count=9,
                ),
                SimpleNamespace(
                    involved_object=SimpleNamespace(name="api-7d9c"),
                    type="Normal",
                    reason="Pulled",
                    message="image present",
                    count=1,
                ),
            ]
        )

    def read_namespaced_pod_log(
        self, name, namespace, container=None, tail_lines=None, _request_timeout=None
    ):  # noqa: ANN001
        return f"real-log for {namespace}/{name} container={container}"

    def list_namespace(self, _request_timeout=None):  # noqa: ANN001
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(name="default"),
                    status=SimpleNamespace(phase="Active"),
                ),
                SimpleNamespace(
                    metadata=SimpleNamespace(name="kube-system"),
                    status=SimpleNamespace(phase="Active"),
                ),
            ]
        )

    def list_namespaced_service(self, namespace, _request_timeout=None):  # noqa: ANN001
        return SimpleNamespace(items=[])

    def list_namespaced_endpoints(
        self, namespace, _request_timeout=None
    ):  # noqa: ANN001
        return SimpleNamespace(items=[])

    def list_node(self, _request_timeout=None):  # noqa: ANN001
        return SimpleNamespace(items=[])


class ExplodingCoreApi:
    """真实分支的失败替身：任何调用都抛异常，触发自动降级。"""

    def _boom(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("connection refused")

    list_namespaced_pod = _boom
    read_namespaced_pod = _boom
    list_namespaced_event = _boom
    read_namespaced_pod_log = _boom
    list_namespace = _boom
    list_namespaced_service = _boom
    list_namespaced_endpoints = _boom
    list_node = _boom


# ---------------------------------------------------------------------------
# mock 模式
# ---------------------------------------------------------------------------
def test_mock_mode_list_pods_returns_demo_data() -> None:
    client = K8sReadOnlyClient(mode="mock")
    pods = client.list_pods("default")
    assert {p["name"] for p in pods} == {
        "api-7d9c",
        "checkout-5f8b",
        "image-worker-22a",
    }
    assert all(p["namespace"] == "default" for p in pods)


def test_mock_mode_describe_events_logs_namespaces() -> None:
    client = K8sReadOnlyClient(mode="mock")
    described = client.describe_pod("default", "checkout-5f8b")
    assert described["name"] == "checkout-5f8b"
    assert described["containers"]

    events = client.list_events("default", "checkout-5f8b")
    assert events and all(e["pod"] == "checkout-5f8b" for e in events)

    logs = client.get_pod_logs("default", "checkout-5f8b", tail_lines=2)
    assert logs.count("\n") == 1  # 只取尾部 2 行

    namespaces = client.list_namespaces()
    assert {n["name"] for n in namespaces} == {"default", "kube-system", "prod"}


# ---------------------------------------------------------------------------
# real 模式（注入成功替身）
# ---------------------------------------------------------------------------
def test_real_mode_uses_injected_core_api() -> None:
    client = K8sReadOnlyClient(mode="real", core_api=FakeCoreApi())

    pods = client.list_pods("default")
    assert pods[0]["name"] == "checkout-5f8b"
    assert pods[0]["node"] == "node-b"
    assert pods[0]["ready"] is True

    described = client.describe_pod("default", "checkout-5f8b")
    assert described["containers"][0]["state"] == "running"
    assert described["conditions"][0]["type"] == "Ready"

    events = client.list_events("default", pod_name="checkout-5f8b")
    assert len(events) == 1
    assert events[0]["count"] == 9

    logs = client.get_pod_logs("default", "checkout-5f8b", container="checkout")
    assert logs == "real-log for default/checkout-5f8b container=checkout"

    namespaces = client.list_namespaces()
    assert {n["name"] for n in namespaces} == {"default", "kube-system"}


# ---------------------------------------------------------------------------
# real 模式自动降级
# ---------------------------------------------------------------------------
def test_real_mode_falls_back_to_mock_on_error() -> None:
    client = K8sReadOnlyClient(mode="real", core_api=ExplodingCoreApi())

    # 每个方法都应降级到 mock 数据而非抛错
    assert {p["name"] for p in client.list_pods("default")} == {
        "api-7d9c",
        "checkout-5f8b",
        "image-worker-22a",
    }
    assert client.describe_pod("default", "checkout-5f8b")["name"] == "checkout-5f8b"
    assert client.list_events("default")
    assert "[mock]" in client.get_pod_logs("default", "checkout-5f8b")
    assert {n["name"] for n in client.list_namespaces()} == {
        "default",
        "kube-system",
        "prod",
    }


def test_real_mode_fail_closed_does_not_return_mock_data() -> None:
    client = K8sReadOnlyClient(
        mode="real",
        core_api=ExplodingCoreApi(),
        fallback_policy="fail_closed",
    )

    with pytest.raises(OpsError) as exc_info:
        client.list_pods("default")

    assert exc_info.value.code == ErrorCode.ENV_CONNECTION_FAILED
    assert client.last_data_origin == "unavailable"
    assert client.last_error


def test_real_mode_403_has_stable_permission_error() -> None:
    class ForbiddenCoreApi:
        def list_namespaced_pod(self, **kwargs: object) -> object:
            error = RuntimeError("provider response must stay private")
            error.status = 403  # type: ignore[attr-defined]
            raise error

    client = K8sReadOnlyClient(
        mode="real",
        core_api=ForbiddenCoreApi(),
        fallback_policy="fail_closed",
    )

    with pytest.raises(OpsError) as exc_info:
        client.list_pods("default")

    assert exc_info.value.code is ErrorCode.ENV_PERMISSION_DENIED
    assert "provider response" not in exc_info.value.message


def test_real_mode_falls_back_when_sdk_missing() -> None:
    # 不注入 core_api 且真实环境无 kubeconfig：_get_core_api 内部异常应被降级捕获
    client = K8sReadOnlyClient(mode="real", kubeconfig="/nonexistent/kubeconfig-xyz")
    pods = client.list_pods("default")  # 不应抛错
    assert {p["name"] for p in pods} == {
        "api-7d9c",
        "checkout-5f8b",
        "image-worker-22a",
    }


# ---------------------------------------------------------------------------
# 命名空间白名单（安全边界，越权硬失败，不降级）
# ---------------------------------------------------------------------------
def test_namespace_allowlist_blocks_unlisted_namespace() -> None:
    client = K8sReadOnlyClient(mode="mock", namespace_allowlist=["default"])
    # 白名单内正常
    assert client.list_pods("default")
    # 白名单外硬失败
    with pytest.raises(OpsError) as exc_info:
        client.list_pods("prod")
    assert exc_info.value.code == ErrorCode.ENV_SCOPE_DENIED


def test_namespace_allowlist_enforced_in_real_mode() -> None:
    client = K8sReadOnlyClient(
        mode="real", core_api=FakeCoreApi(), namespace_allowlist=["default"]
    )
    with pytest.raises(OpsError):
        client.describe_pod("prod", "whatever")


def test_empty_allowlist_allows_any_namespace() -> None:
    client = K8sReadOnlyClient(mode="mock", namespace_allowlist=[])
    assert client.list_pods("anything")


def test_list_namespaces_filtered_by_allowlist() -> None:
    client = K8sReadOnlyClient(mode="mock", namespace_allowlist=["prod"])
    namespaces = client.list_namespaces()
    assert {n["name"] for n in namespaces} == {"prod"}


def test_blank_namespace_rejected() -> None:
    client = K8sReadOnlyClient(mode="mock")
    with pytest.raises(OpsError):
        client.list_pods("   ")


# ---------------------------------------------------------------------------
# 边界与配置校验
# ---------------------------------------------------------------------------
def test_invalid_mode_raises() -> None:
    with pytest.raises(OpsError) as exc_info:
        K8sReadOnlyClient(mode="prod-cluster")
    assert exc_info.value.code == ErrorCode.CONFIG_INVALID


def test_blank_pod_name_and_non_positive_tail_lines() -> None:
    client = K8sReadOnlyClient(mode="mock")
    with pytest.raises(OpsError):
        client.describe_pod("default", "")
    with pytest.raises(ValueError):
        client.get_pod_logs("default", "checkout-5f8b", tail_lines=0)


def test_from_settings_wires_ops_config() -> None:
    settings = AthenaSettings(
        ops=OpsSettings(
            mode="real",
            kubernetes=K8sSettings(
                namespace_allowlist=["default", "prod"],
                timeout=3.5,
                fallback_policy="fail_closed",
            ),
        )
    )
    client = K8sReadOnlyClient.from_settings(settings)
    assert client.mode == "real"
    assert client.namespace_allowlist == ("default", "prod")
    assert client.timeout == 3.5
    assert client.fallback_policy == "fail_closed"


# ---------------------------------------------------------------------------
# 工具注册 + invoke 端到端
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_register_and_invoke_k8s_tools() -> None:
    registry = ToolRegistry()
    register_k8s_readonly_tools(registry, client=K8sReadOnlyClient(mode="mock"))

    expected = {
        "k8s_list_pods",
        "k8s_describe_pod",
        "k8s_list_events",
        "k8s_get_pod_logs",
        "k8s_list_namespaces",
    }
    assert expected.issubset(registry.tools.keys())

    result = await registry.invoke(
        ToolCall(name="k8s_list_pods", arguments={"namespace": "default"})
    )
    assert result.success is True
    payload = json.loads(result.content)
    assert {p["name"] for p in payload} == {
        "api-7d9c",
        "checkout-5f8b",
        "image-worker-22a",
    }


@pytest.mark.asyncio
async def test_tool_invoke_surfaces_allowlist_error() -> None:
    registry = ToolRegistry()
    register_k8s_readonly_tools(
        registry,
        client=K8sReadOnlyClient(mode="mock", namespace_allowlist=["default"]),
    )
    result = await registry.invoke(
        ToolCall(name="k8s_list_pods", arguments={"namespace": "prod"})
    )
    assert result.success is False
    # registry 会把异常统一包成 TOOL_EXECUTION_FAILED，白名单原因通过 message 透出
    assert "allowlist" in (result.error or "")


def test_register_defaults_to_mock_without_client_or_settings() -> None:
    registry = ToolRegistry()
    register_k8s_readonly_tools(registry)
    assert "k8s_list_namespaces" in registry.tools


# ---------------------------------------------------------------------------
# 只读诊断分析器
# ---------------------------------------------------------------------------
def test_diagnoser_flags_crashloop_with_log_evidence() -> None:
    diagnoser = K8sReadOnlyDiagnoser(K8sReadOnlyClient(mode="mock"))
    findings = diagnoser.diagnose_namespace(
        "default", include_logs=True, log_tail_lines=5
    )
    by_pod = {f.pod: f for f in findings}

    crash = by_pod["checkout-5f8b"]
    assert crash.severity == "high"
    assert crash.symptom == "CrashLoopBackOff"
    # 事件证据 + 日志证据都应出现
    assert any("BackOff" in ev for ev in crash.evidence)
    assert any(ev.startswith("log:") for ev in crash.evidence)

    image = by_pod["image-worker-22a"]
    assert image.severity == "medium"
    assert image.symptom == "ImagePullBackOff"
    assert "Failed to pull image" in image.root_cause


def test_diagnoser_can_skip_logs() -> None:
    diagnoser = K8sReadOnlyDiagnoser(K8sReadOnlyClient(mode="mock"))
    findings = diagnoser.diagnose_namespace("default", include_logs=False)
    crash = next(f for f in findings if f.pod == "checkout-5f8b")
    assert not any(ev.startswith("log:") for ev in crash.evidence)


def test_diagnoser_respects_namespace_allowlist() -> None:
    diagnoser = K8sReadOnlyDiagnoser(
        K8sReadOnlyClient(mode="mock", namespace_allowlist=["default"])
    )
    with pytest.raises(OpsError):
        diagnoser.diagnose_namespace("prod")


def test_diagnoser_rejects_non_positive_tail_lines() -> None:
    diagnoser = K8sReadOnlyDiagnoser(K8sReadOnlyClient(mode="mock"))
    with pytest.raises(ValueError):
        diagnoser.diagnose_namespace("default", log_tail_lines=0)


def test_diagnoser_log_failure_does_not_break_diagnosis() -> None:
    class LogFailingClient(K8sReadOnlyClient):
        def get_pod_logs(
            self, namespace, name, container=None, tail_lines=100
        ):  # noqa: ANN001
            raise RuntimeError("logs endpoint down")

    diagnoser = K8sReadOnlyDiagnoser(LogFailingClient(mode="mock"))
    findings = diagnoser.diagnose_namespace("default", include_logs=True)
    crash = next(f for f in findings if f.pod == "checkout-5f8b")
    assert any("log unavailable" in ev for ev in crash.evidence)


@pytest.mark.asyncio
async def test_diagnose_tool_registered_and_invokable() -> None:
    registry = ToolRegistry()
    register_k8s_readonly_tools(registry, client=K8sReadOnlyClient(mode="mock"))
    assert "k8s_diagnose_namespace" in registry.tools

    result = await registry.invoke(
        ToolCall(
            name="k8s_diagnose_namespace",
            arguments={"namespace": "default", "include_logs": False},
        )
    )
    assert result.success is True
    payload = json.loads(result.content)
    symptoms = {item["symptom"] for item in payload}
    assert "CrashLoopBackOff" in symptoms
    assert "ImagePullBackOff" in symptoms


# ---------------------------------------------------------------------------
# 阶段 1 新增只读能力：deployments / services / nodes（mock）
# ---------------------------------------------------------------------------
def test_mock_list_deployments_services_nodes() -> None:
    client = K8sReadOnlyClient(mode="mock")

    deployments = client.list_deployments("default")
    by_name = {d["name"]: d for d in deployments}
    assert by_name["api"]["healthy"] is True
    assert by_name["checkout"]["healthy"] is False  # ready != desired

    services = client.list_services("default")
    checkout_svc = next(s for s in services if s["name"] == "checkout")
    assert checkout_svc["selector"] == {}  # 空选择器：服务不可达隐患

    nodes = client.get_node_status()
    node_b = next(n for n in nodes if n["name"] == "node-b")
    assert node_b["ready"] is True
    assert "MemoryPressure" in node_b["pressure"]

    endpoints = client.list_endpoints("default")
    checkout_ep = next(e for e in endpoints if e["name"] == "checkout")
    assert checkout_ep["addresses"] == []


def test_new_readonly_methods_respect_allowlist() -> None:
    client = K8sReadOnlyClient(mode="mock", namespace_allowlist=["default"])
    assert client.list_deployments("default")
    assert client.list_services("default")
    with pytest.raises(OpsError):
        client.list_deployments("prod")
    with pytest.raises(OpsError):
        client.list_services("prod")
    with pytest.raises(OpsError):
        client.list_endpoints("prod")
    # 节点是集群级资源，不受命名空间白名单限制
    assert client.get_node_status()


# ---------------------------------------------------------------------------
# 阶段 1 新增只读能力：real 模式（注入替身）
# ---------------------------------------------------------------------------
class FakeAppsApi:
    """AppsV1Api 成功替身：返回一个不健康的 Deployment。"""

    def list_namespaced_deployment(
        self, namespace, _request_timeout=None
    ):  # noqa: ANN001
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(name="checkout", namespace=namespace),
                    spec=SimpleNamespace(replicas=2),
                    status=SimpleNamespace(
                        ready_replicas=0,
                        available_replicas=0,
                        updated_replicas=2,
                    ),
                )
            ]
        )


class FakeServiceNodeApi:
    """CoreV1Api 成功替身：覆盖 service 与 node 只读查询。"""

    def list_namespaced_service(self, namespace, _request_timeout=None):  # noqa: ANN001
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(name="api", namespace=namespace),
                    spec=SimpleNamespace(
                        type="ClusterIP",
                        cluster_ip="10.96.0.10",
                        selector={"app": "api"},
                        ports=[
                            SimpleNamespace(port=80, target_port=8080, protocol="TCP")
                        ],
                    ),
                )
            ]
        )

    def list_namespaced_endpoints(
        self, namespace, _request_timeout=None
    ):  # noqa: ANN001
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(name="api", namespace=namespace),
                    subsets=[
                        SimpleNamespace(
                            addresses=[SimpleNamespace(ip="10.244.0.10")],
                            ports=[SimpleNamespace(port=8080)],
                        )
                    ],
                )
            ]
        )

    def list_node(self, _request_timeout=None):  # noqa: ANN001
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(name="node-a"),
                    status=SimpleNamespace(
                        conditions=[
                            SimpleNamespace(type="Ready", status="True"),
                            SimpleNamespace(type="MemoryPressure", status="False"),
                        ],
                        allocatable={"cpu": "4", "memory": "8Gi"},
                        node_info=SimpleNamespace(kubelet_version="v1.29.0"),
                    ),
                )
            ]
        )


def test_real_mode_deployments_via_injected_apps_api() -> None:
    client = K8sReadOnlyClient(mode="real", apps_api=FakeAppsApi())
    deployments = client.list_deployments("default")
    assert deployments[0]["name"] == "checkout"
    assert deployments[0]["desired"] == 2
    assert deployments[0]["healthy"] is False


def test_real_mode_services_and_nodes_via_injected_core_api() -> None:
    client = K8sReadOnlyClient(mode="real", core_api=FakeServiceNodeApi())

    services = client.list_services("default")
    assert services[0]["name"] == "api"
    assert services[0]["ports"][0]["target_port"] == "8080"

    endpoints = client.list_endpoints("default")
    assert endpoints[0]["addresses"] == ["10.244.0.10"]

    nodes = client.get_node_status()
    assert nodes[0]["name"] == "node-a"
    assert nodes[0]["ready"] is True
    assert nodes[0]["pressure"] == []  # MemoryPressure=False 不计入


def test_real_mode_new_methods_fall_back_to_mock_on_error() -> None:
    client = K8sReadOnlyClient(mode="real", kubeconfig="/nonexistent/kubeconfig-xyz")
    # 无 kubeconfig/SDK：三个新方法都应降级 mock，不抛错
    assert {d["name"] for d in client.list_deployments("default")} == {
        "api",
        "checkout",
    }
    assert {s["name"] for s in client.list_services("default")} == {
        "api",
        "checkout",
    }
    assert {e["name"] for e in client.list_endpoints("default")} == {"api", "checkout"}
    assert {n["name"] for n in client.get_node_status()} == {"node-a", "node-b"}


@pytest.mark.asyncio
async def test_new_readonly_tools_registered_and_invokable() -> None:
    registry = ToolRegistry()
    register_k8s_readonly_tools(registry, client=K8sReadOnlyClient(mode="mock"))
    for name in (
        "k8s_list_deployments",
        "k8s_list_services",
        "k8s_list_endpoints",
        "k8s_get_node_status",
    ):
        assert name in registry.tools

    result = await registry.invoke(ToolCall(name="k8s_get_node_status", arguments={}))
    assert result.success is True
    assert {n["name"] for n in json.loads(result.content)} == {"node-a", "node-b"}


# ---------------------------------------------------------------------------
# 阶段 2：结构化诊断报告模型
# ---------------------------------------------------------------------------
def test_build_report_produces_structured_schema() -> None:
    diagnoser = K8sReadOnlyDiagnoser(K8sReadOnlyClient(mode="mock"))
    report = diagnoser.build_report("default", include_logs=True)

    assert report.namespace == "default"
    assert report.findings  # 至少 CrashLoop + ImagePull
    # 每条 finding 都是结构化字段，evidence 与结论强绑定
    crash = next(f for f in report.findings if f.symptom == "CrashLoopBackOff")
    assert crash.resource_kind == "Pod"
    assert crash.severity == "high"
    assert crash.probable_causes  # 非空根因
    assert crash.evidence  # 有证据支撑

    # metrics 按严重级别计数，前端可一眼看清风险分布
    assert report.metrics["finding_count"] == len(report.findings)
    assert report.metrics["severity_counts"]["high"] >= 1
    # 整体建议去重、非空
    assert report.actions
    assert len(report.actions) == len(set(report.actions))


def test_playbooks_emit_service_unreachable_ops_finding() -> None:
    diagnoser = K8sReadOnlyDiagnoser(K8sReadOnlyClient(mode="mock"))
    report = diagnoser.build_report("default", include_logs=False)
    service = next(f for f in report.findings if f.resource_kind == "Service")

    assert service.resource_name == "checkout"
    assert service.symptom == "Service unreachable"
    assert any("selector" in item for item in service.evidence)
    assert any("endpoints" in item for item in service.evidence)
    assert service.recommended_actions


def test_playbooks_emit_pending_ops_finding() -> None:
    class PendingClient(K8sReadOnlyClient):
        def list_pods(self, namespace="default"):  # noqa: ANN001
            return [
                {
                    "name": "worker-pending",
                    "namespace": namespace,
                    "status": "Pending",
                    "restarts": 0,
                    "node": "",
                    "ready": False,
                    "labels": {"app": "worker"},
                }
            ]

        def describe_pod(self, namespace, name):  # noqa: ANN001
            return {
                "name": name,
                "namespace": namespace,
                "status": "Pending",
                "node": "",
                "labels": {"app": "worker"},
                "containers": [],
                "conditions": [],
            }

        def list_events(self, namespace="default", pod_name=None):  # noqa: ANN001
            return [
                {
                    "pod": "worker-pending",
                    "type": "Warning",
                    "reason": "FailedScheduling",
                    "message": "0/2 nodes are available: insufficient cpu",
                    "count": 3,
                }
            ]

        def list_services(self, namespace="default"):  # noqa: ANN001
            return []

        def list_endpoints(self, namespace="default"):  # noqa: ANN001
            return []

        def get_node_status(self):  # noqa: ANN001
            return [
                {
                    "name": "node-a",
                    "ready": True,
                    "pressure": ["MemoryPressure"],
                    "allocatable": {"cpu": "1", "memory": "1Gi"},
                    "kubelet_version": "v1.29.0",
                }
            ]

    diagnoser = K8sReadOnlyDiagnoser(PendingClient(mode="mock"))
    report = diagnoser.build_report("default", include_logs=False)
    pending = report.findings[0]

    assert pending.symptom == "Pod Pending"
    assert pending.resource_name == "worker-pending"
    assert any("FailedScheduling" in item for item in pending.evidence)
    assert any("MemoryPressure" in item for item in pending.evidence)
    assert any("PVC" in item for item in pending.recommended_actions)


def test_evidence_bound_summarizer_prompt_contains_only_report_json() -> None:
    diagnoser = K8sReadOnlyDiagnoser(K8sReadOnlyClient(mode="mock"))
    report = diagnoser.build_report("default", include_logs=False)
    summarizer = EvidenceBoundReportSummarizer()
    messages = summarizer.build_messages(report)

    assert messages_contain_only_report(messages, report)
    assert "不得补充 JSON 中不存在的事实" in messages[0].content
    assert "OpsDiagnosisReport" in messages[1].content
    # 用户消息只包含报告 JSON，不包含用户自由输入 task，避免 LLM 从 task 补事实。
    assert "namespace=prod" not in messages[1].content


def test_deterministic_summary_is_based_on_report_fields() -> None:
    diagnoser = K8sReadOnlyDiagnoser(K8sReadOnlyClient(mode="mock"))
    report = diagnoser.build_report("default", include_logs=False)
    summary = EvidenceBoundReportSummarizer.deterministic_summary(report)

    assert report.summary in summary
    assert "Pod/checkout-5f8b" in summary
    assert "建议动作" in summary


def test_report_includes_prometheus_metrics_when_enabled() -> None:
    diagnoser = K8sReadOnlyDiagnoser(
        K8sReadOnlyClient(mode="mock"),
        PrometheusQueryClient(enabled=True, base_url="mock://prometheus"),
    )
    report = diagnoser.build_report("default", include_logs=False)

    assert report.metrics["prometheus_available"] is True
    metric_names = {metric["name"] for metric in report.metrics["prometheus_metrics"]}
    assert "pod_cpu_usage" in metric_names
    assert "pod_memory_usage" in metric_names
    assert any(item.startswith("prometheus:") for item in report.raw_evidence)


def test_cpu_memory_playbook_emits_ops_finding_when_metrics_high() -> None:
    diagnoser = K8sReadOnlyDiagnoser(
        K8sReadOnlyClient(mode="mock"),
        PrometheusQueryClient(enabled=True, base_url="mock://prometheus"),
    )
    report = diagnoser.build_report("default", include_logs=False)
    resource_findings = [
        finding
        for finding in report.findings
        if finding.symptom == "CPU / Memory abnormal"
    ]

    assert resource_findings
    assert any("pod_cpu_usage" in item for item in resource_findings[0].evidence)
    assert any("memory" in cause for cause in resource_findings[0].probable_causes)


def test_k8s_write_action_preview_and_allowlist_enforcement() -> None:
    executor = K8sWriteActionExecutor(
        K8sReadOnlyClient(mode="mock", namespace_allowlist=["default"])
    )
    preview = executor.preview(
        "environment=staging scale deployment checkout replicas=2", "staging"
    )

    assert preview is not None
    assert preview.requires_confirmation is True
    assert preview.plan is not None
    assert preview.plan.command_preview == (
        "kubectl scale deployment/checkout --replicas=2 -n staging"
    )
    assert preview.plan.environment == "staging"
    assert preview.plan.security["rbac_scope_checked"] is True
    assert preview.plan.rollback_suggestion

    result = executor.execute(preview.plan)
    assert result.success is False
    assert "allowlist" in result.error


def test_k8s_write_action_blocks_prod_by_default() -> None:
    executor = K8sWriteActionExecutor(K8sReadOnlyClient(mode="mock"))
    preview = executor.preview(
        "environment=prod scale deployment checkout replicas=2", "prod"
    )

    assert preview is not None
    assert preview.success is False
    assert preview.requires_confirmation is False
    assert "prod write is disabled" in preview.error
    assert preview.security["environment"] == "prod"


def test_k8s_write_action_allows_prod_when_explicitly_enabled() -> None:
    executor = K8sWriteActionExecutor(
        K8sReadOnlyClient(mode="mock"),
        K8sActionSecurityPolicy(prod_write_enabled=True),
        actor="tenant-a",
    )
    preview = executor.preview(
        "environment=prod scale deployment checkout replicas=2", "prod"
    )

    assert preview is not None
    assert preview.requires_confirmation is True
    assert preview.plan is not None
    assert preview.plan.actor == "tenant-a"
    assert preview.plan.security["required_scope"] == "cloud:execute"
    assert preview.plan.security["prod_write_enabled"] is True


def test_k8s_write_action_enforces_verb_allowlist() -> None:
    executor = K8sWriteActionExecutor(
        K8sReadOnlyClient(mode="mock"),
        K8sActionSecurityPolicy(allowed_verbs=("rollout_restart",)),
    )
    preview = executor.preview("scale deployment checkout replicas=2", "default")

    assert preview is not None
    assert preview.success is False
    assert preview.requires_confirmation is False
    assert "verb scale is not allowed" in preview.error


def test_k8s_write_action_enforces_resource_kind_allowlist() -> None:
    executor = K8sWriteActionExecutor(
        K8sReadOnlyClient(mode="mock"),
        K8sActionSecurityPolicy(allowed_resource_kinds=("StatefulSet",)),
    )
    preview = executor.preview("rollout restart deployment checkout", "default")

    assert preview is not None
    assert preview.success is False
    assert preview.requires_confirmation is False
    assert "resource kind Deployment is not allowed" in preview.error


def test_k8s_write_action_blocks_high_risk_commands() -> None:
    executor = K8sWriteActionExecutor(K8sReadOnlyClient(mode="mock"))
    result = executor.preview("delete pvc data-volume namespace=default", "default")

    assert result is not None
    assert result.success is False
    assert result.requires_confirmation is False
    assert "blocked" in result.error


def test_report_dict_is_json_serializable() -> None:
    diagnoser = K8sReadOnlyDiagnoser(K8sReadOnlyClient(mode="mock"))
    payload = diagnoser.report_dict("default")
    # 能被 json 序列化，且顶层字段齐全
    dumped = json.loads(json.dumps(payload, ensure_ascii=False))
    assert set(dumped.keys()) == {
        "summary",
        "namespace",
        "findings",
        "metrics",
        "actions",
        "raw_evidence",
    }
    assert isinstance(dumped["findings"], list)


def test_report_summary_when_no_findings() -> None:
    # 用一个只返回健康 Pod 的客户端，验证“未发现异常”摘要分支
    class HealthyClient(K8sReadOnlyClient):
        def list_pods(self, namespace="default"):  # noqa: ANN001
            return [
                {
                    "name": "api-7d9c",
                    "namespace": namespace,
                    "status": "Running",
                    "restarts": 0,
                    "node": "node-a",
                    "ready": True,
                }
            ]

        def list_events(self, namespace="default", pod_name=None):  # noqa: ANN001
            return []

        def list_services(self, namespace="default"):  # noqa: ANN001
            return []

        def list_endpoints(self, namespace="default"):  # noqa: ANN001
            return []

        def get_node_status(self):  # noqa: ANN001
            return []

    diagnoser = K8sReadOnlyDiagnoser(HealthyClient(mode="mock"))
    report = diagnoser.build_report("default")
    assert report.findings == []
    assert "未发现异常" in report.summary
