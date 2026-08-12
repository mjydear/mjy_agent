"""外部集成降级测试：K8s/Prometheus/云 SDK 缺失时回退 Mock。"""

from __future__ import annotations

from athena.tools.builtin.cloud.aliyun import AliyunClient
from athena.tools.builtin.cloud.aws import AWSClient
from athena.tools.builtin.k8s.client import K8sClient
from athena.tools.builtin.observability.prometheus import PrometheusClient


def test_k8s_mock_mode_default() -> None:
    pods = K8sClient(namespace="default").list_pods()
    assert any(p["status"] == "CrashLoopBackOff" for p in pods)


def test_k8s_real_mode_without_sdk_falls_back() -> None:
    # use_mock=False 但未装 kubernetes SDK → 自动降级 Mock，不抛异常
    pods = K8sClient(namespace="default", use_mock=False).list_pods()
    assert isinstance(pods, list) and pods


def test_prometheus_mock_scheme() -> None:
    result = PrometheusClient().query("container_cpu")
    assert result["value"] == 0.91
    assert result["source"] == "mock://prometheus"


def test_prometheus_real_url_unreachable_falls_back() -> None:
    # http:// 地址但连不上 → 降级 Mock，不抛异常
    client = PrometheusClient(base_url="http://127.0.0.1:1", timeout_seconds=0.2)
    result = client.query("container_cpu")
    assert result["value"] == 0.91


def test_cloud_no_credentials_uses_mock() -> None:
    # 默认无真实凭证 → resolve_data 走 mock
    assert AliyunClient().has_real_credentials() is False
    result = AliyunClient().list_instances()
    ids = [i["id"] for i in result.data["instances"]]
    assert "i-prod-api-01" in ids


def test_aws_no_credentials_uses_mock() -> None:
    result = AWSClient().list_instances()
    assert result.provider == "aws"
    assert result.success is True
