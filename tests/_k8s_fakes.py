"""
测试专用 Kubernetes SDK 替身（test doubles，非产品 mock）。

生产代码已彻底移除 mock：K8sReadOnlyClient 只走真实 SDK。单元测试通过注入这里的
假 CoreV1Api / AppsV1Api 覆盖真实转换逻辑，无需真实集群。这些替身复刻了原演示场景
（api 健康 / checkout CrashLoop / image-worker ImagePull），让诊断类测试保持稳定。

用法：
    client = K8sReadOnlyClient(core_api=DemoCoreApi(), apps_api=DemoAppsApi())
"""

from __future__ import annotations

from types import SimpleNamespace


def _pod(
    name: str,
    *,
    namespace: str = "default",
    phase: str = "Running",
    restart_count: int = 0,
    ready: bool = True,
    node: str = "node-a",
    waiting_reason: str | None = None,
    labels: dict | None = None,
) -> SimpleNamespace:
    """构造一个近似 kubernetes SDK V1Pod 的替身对象。"""
    if waiting_reason is not None:
        state = SimpleNamespace(
            running=None,
            waiting=SimpleNamespace(reason=waiting_reason),
            terminated=None,
        )
    else:
        state = SimpleNamespace(running=SimpleNamespace(), waiting=None, terminated=None)
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name, namespace=namespace, labels=labels or {"app": name}
        ),
        spec=SimpleNamespace(
            node_name=node,
            containers=[SimpleNamespace(name=name, ports=[])],
        ),
        status=SimpleNamespace(
            phase=phase,
            start_time=SimpleNamespace(isoformat=lambda: "2024-01-01T00:00:00+00:00"),
            container_statuses=[
                SimpleNamespace(
                    name=name,
                    image="registry/demo:latest",
                    ready=ready,
                    restart_count=restart_count,
                    state=state,
                )
            ],
            conditions=[SimpleNamespace(type="Ready", status="True", reason=None)],
        ),
    )


class DemoCoreApi:
    """
    CoreV1Api 替身：复刻演示场景（api 健康 / checkout CrashLoop / image-worker ImagePull）。

    支持按 namespace 定制 pod/事件；默认场景与旧 mock 数据等价，供诊断类测试复用。
    """

    def list_namespaced_pod(self, namespace, _request_timeout=None):  # noqa: ANN001
        return SimpleNamespace(
            items=[
                _pod("api-7d9c", namespace=namespace),
                _pod(
                    "checkout-5f8b",
                    namespace=namespace,
                    phase="CrashLoopBackOff",
                    restart_count=5,
                    ready=False,
                    node="node-b",
                    waiting_reason="CrashLoopBackOff",
                ),
                _pod(
                    "image-worker-22a",
                    namespace=namespace,
                    phase="ImagePullBackOff",
                    ready=False,
                    node="",
                    waiting_reason="ImagePullBackOff",
                ),
            ]
        )

    def read_namespaced_pod(self, name, namespace, _request_timeout=None):  # noqa: ANN001
        return _pod(name, namespace=namespace)

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
                    involved_object=SimpleNamespace(name="image-worker-22a"),
                    type="Warning",
                    reason="Failed",
                    message="Failed to pull image registry/demo:latest",
                    count=4,
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
        lines = [f"line{i} error for {namespace}/{name}" for i in range(1, 6)]
        if tail_lines:
            lines = lines[-tail_lines:]
        return "\n".join(lines)

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
                SimpleNamespace(
                    metadata=SimpleNamespace(name="prod"),
                    status=SimpleNamespace(phase="Active"),
                ),
            ]
        )

    def list_namespaced_service(self, namespace, _request_timeout=None):  # noqa: ANN001
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(name="checkout", namespace=namespace),
                    spec=SimpleNamespace(
                        type="ClusterIP",
                        cluster_ip="10.96.0.20",
                        selector={},  # 空选择器：服务不可达隐患
                        ports=[SimpleNamespace(port=80, target_port=8080, protocol="TCP")],
                    ),
                ),
            ]
        )

    def list_namespaced_endpoints(self, namespace, _request_timeout=None):  # noqa: ANN001
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(name="checkout", namespace=namespace),
                    subsets=[],  # endpoints 为空
                ),
            ]
        )

    def list_node(self, _request_timeout=None):  # noqa: ANN001
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(name="node-a"),
                    status=SimpleNamespace(
                        conditions=[SimpleNamespace(type="Ready", status="True")],
                        allocatable={"cpu": "4", "memory": "8Gi"},
                        node_info=SimpleNamespace(kubelet_version="v1.29.0"),
                    ),
                ),
                SimpleNamespace(
                    metadata=SimpleNamespace(name="node-b"),
                    status=SimpleNamespace(
                        conditions=[
                            SimpleNamespace(type="Ready", status="True"),
                            SimpleNamespace(type="MemoryPressure", status="True"),
                        ],
                        allocatable={"cpu": "4", "memory": "8Gi"},
                        node_info=SimpleNamespace(kubelet_version="v1.29.0"),
                    ),
                ),
            ]
        )


class DemoAppsApi:
    """AppsV1Api 替身：一个健康 api Deployment + 一个不健康 checkout Deployment。"""

    def list_namespaced_deployment(self, namespace, _request_timeout=None):  # noqa: ANN001
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=SimpleNamespace(name="api", namespace=namespace),
                    spec=SimpleNamespace(replicas=2),
                    status=SimpleNamespace(
                        ready_replicas=2, available_replicas=2, updated_replicas=2
                    ),
                ),
                SimpleNamespace(
                    metadata=SimpleNamespace(name="checkout", namespace=namespace),
                    spec=SimpleNamespace(replicas=2),
                    status=SimpleNamespace(
                        ready_replicas=0, available_replicas=0, updated_replicas=2
                    ),
                ),
            ]
        )

    def patch_namespaced_deployment(self, name, namespace, body=None, _request_timeout=None):  # noqa: ANN001
        return SimpleNamespace(metadata=SimpleNamespace(name=name, namespace=namespace))

    def patch_namespaced_deployment_scale(self, name, namespace, body=None, _request_timeout=None):  # noqa: ANN001
        return SimpleNamespace(metadata=SimpleNamespace(name=name, namespace=namespace))


class ExplodingCoreApi:
    """任何调用都抛异常的替身：验证真实调用失败时抛 OpsError（不降级、无 mock）。"""

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


def demo_client(namespace_allowlist=None):
    """便捷构造：注入 Demo 替身的真实客户端（供诊断类测试复用）。"""
    from athena.tools.cloud.k8s import K8sReadOnlyClient

    return K8sReadOnlyClient(
        core_api=DemoCoreApi(),
        apps_api=DemoAppsApi(),
        namespace_allowlist=namespace_allowlist,
    )
