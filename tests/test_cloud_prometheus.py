"""Tests for CloudOps Prometheus query client."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlparse

from athena.config import AthenaSettings, OpsSettings, PrometheusSettings
from athena.tools.cloud.prometheus import PrometheusQueryClient


class _PromHandler(BaseHTTPRequestHandler):
    """Tiny test-only Prometheus HTTP API."""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query).get("query", [""])[0]
        value = "0.42" if "container_cpu" in query else "1"
        body = json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [1, value]}],
                },
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def test_prometheus_disabled_returns_unavailable() -> None:
    client = PrometheusQueryClient(enabled=False)
    result = client.pod_cpu_usage("default", "api")
    assert result.available is False
    assert result.value is None
    assert result.error == "prometheus disabled"


def test_prometheus_unavailable_without_http_base_url() -> None:
    # 已彻底移除 mock：非 http(s) base_url 不再返回模拟数据，而是标记不可用。
    client = PrometheusQueryClient(enabled=True, base_url="mock://prometheus")
    result = client.pod_cpu_usage("default", "api")
    assert result.available is False
    assert result.value is None
    assert "http(s)" in result.error

    # snapshot 里的每一项都应为不可用（无任何模拟指标值）。
    pod_metrics = client.pod_resource_snapshot("default", "api")
    assert {metric.name for metric in pod_metrics} == {
        "pod_cpu_usage",
        "pod_memory_usage",
        "pod_restart_count",
    }
    assert all(metric.available is False for metric in pod_metrics)


def test_prometheus_from_settings() -> None:
    settings = AthenaSettings(
        ops=OpsSettings(
            prometheus=PrometheusSettings(
                enabled=True,
                base_url="mock://prometheus",
                timeout_seconds=2.5,
            )
        )
    )
    client = PrometheusQueryClient.from_settings(settings)
    assert client.enabled is True
    assert client.base_url == "mock://prometheus"
    assert client.timeout_seconds == 2.5


def test_prometheus_real_http_query_parses_vector_value_real() -> None: # 测试真实 HTTP 查询解析向量值
    server = HTTPServer(("127.0.0.1", 0), _PromHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = PrometheusQueryClient(
            enabled=True,
            base_url=f"http://127.0.0.1:{server.server_port}",
            timeout_seconds=2,
        )
        result = client.pod_cpu_usage("default", "api")
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert result.available is True
    assert result.value == 0.42
    assert result.source.startswith("http://127.0.0.1:")


def test_prometheus_real_http_failure_is_unavailable() -> None:
    client = PrometheusQueryClient(
        enabled=True,
        base_url="http://127.0.0.1:1",
        timeout_seconds=0.1,
    )
    result = client.pod_cpu_usage("default", "api")
    assert result.available is False
    assert result.value is None
    assert result.error
