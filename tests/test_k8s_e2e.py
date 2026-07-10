"""真实 kind 集群 E2E 测试（默认跳过，需显式开启）。

与其它测试不同，本文件**不注入任何替身**，而是让 K8sReadOnlyClient 走真实
kubernetes SDK 连接 kind 集群，验证 mock→real 的真实链路端到端可用。

前置（本地手动运行）：
    1) 启动演示集群与异常工作负载：
       bash deploy/kind-demo/start-kind.sh    （或 pwsh -File deploy/kind-demo/start-kind.ps1）
    2) 开启 E2E 并指向真实集群运行：
       ATHENA_E2E_K8S=1 ATHENA_OPS_MODE=real \
       ATHENA_OPS_K8S_CONTEXT=kind-athena-demo \
       ATHENA_OPS_K8S_NAMESPACE_ALLOWLIST=athena-demo,default \
       pytest tests/test_k8s_e2e.py

默认（CI / 无集群）：ATHENA_E2E_K8S 未设 → 整文件 skip，不破坏 CI。
"""

from __future__ import annotations

import os

import pytest

from athena.config import load_settings
from athena.tools.cloud.k8s import K8sReadOnlyClient, K8sReadOnlyDiagnoser

_E2E_ENABLED = os.getenv("ATHENA_E2E_K8S", "").lower() in {"1", "true", "yes", "on"}

pytestmark = pytest.mark.skipif(
    not _E2E_ENABLED,
    reason=(
        "需 kind 集群：先 bash deploy/kind-demo/start-kind.sh，"
        "再设 ATHENA_E2E_K8S=1 ATHENA_OPS_MODE=real 后运行"
    ),
)

# kind-demo/start-kind.sh 部署异常工作负载所用的命名空间
_NAMESPACE = os.getenv("ATHENA_E2E_NAMESPACE", "athena-demo")


def _real_client(*, strict_real: bool = False) -> K8sReadOnlyClient:
    """从 settings 构造 real 客户端；断言确非 mock，避免误判为链路通过。"""
    settings = load_settings()
    client = K8sReadOnlyClient.from_settings(settings)
    assert client.mode == "real", (
        "E2E 需 ATHENA_OPS_MODE=real；当前 mode="
        f"{client.mode}，请检查环境变量"
    )
    client.strict_real = strict_real
    return client


def test_e2e_list_pods_detects_crashloop() -> None:
    client = _real_client()
    pods = client.list_pods(_NAMESPACE)
    # 真实连通：不应发生降级
    assert client.last_call_degraded is False
    statuses = {p["name"]: p["status"] for p in pods}
    # kind-demo 部署了 crashloop-app 与 bad-image-app
    assert any(name.startswith("crashloop-app") for name in statuses), statuses
    assert any(name.startswith("bad-image-app") for name in statuses), statuses
    assert any(
        s in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "Pending"}
        for s in statuses.values()
    ), statuses


def test_e2e_service_selector_mismatch_has_no_endpoints() -> None:
    client = _real_client()
    endpoints = client.list_endpoints(_NAMESPACE)
    assert client.last_call_degraded is False
    by_name = {e["name"]: e for e in endpoints}
    # service-selector-mismatch.yaml 部署的 web-backend selector 不匹配 → 无地址
    web_backend = by_name.get("web-backend")
    assert web_backend is not None, by_name
    assert web_backend["addresses"] == []


def test_e2e_node_status_ready() -> None:
    client = _real_client()
    nodes = client.get_node_status()
    assert client.last_call_degraded is False
    assert nodes
    assert any(n["ready"] for n in nodes)


def test_e2e_strict_real_no_fallback() -> None:
    # strict 模式下真实调用应成功返回，不抛错、不降级
    client = _real_client(strict_real=True)
    pods = client.list_pods(_NAMESPACE)
    assert isinstance(pods, list)
    assert client.last_call_degraded is False


def test_e2e_diagnose_report() -> None:
    settings = load_settings()
    diagnoser = K8sReadOnlyDiagnoser.from_settings(settings)
    assert diagnoser.client.mode == "real"
    report = diagnoser.build_report(_NAMESPACE, include_logs=True)
    assert diagnoser.client.last_call_degraded is False
    # kind-demo 有 4 个异常工作负载，findings 应非空
    assert report.findings
    assert report.metrics["finding_count"] == len(report.findings)
