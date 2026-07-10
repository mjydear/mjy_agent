"""
📦 模块名称：CloudOps Prometheus 查询客户端
📍 架构位置：CloudOps 工具层，位于 K8s Playbook 与 Prometheus HTTP API 之间。
🎯 核心作用：封装常用 PromQL 查询，直连真实 Prometheus；不可用时返回“指标不可用”（无 mock 数据）。
🔗 依赖关系：仅依赖标准库 urllib/json；被 K8sReadOnlyDiagnoser 注入使用。
💡 设计思路：保持只读、可选；Prometheus 不可用不阻断 K8s 诊断，但绝不返回模拟指标值。
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass

from athena.types import JSONValue

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrometheusQueryResult:
    """单个 PromQL 查询结果。"""

    name: str
    query: str
    value: float | None
    source: str
    available: bool
    unit: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, JSONValue]:
        """转成 JSON 友好结构，供报告 metrics/raw_evidence 使用。"""
        return {
            "name": self.name,
            "query": self.query,
            "value": self.value,
            "source": self.source,
            "available": self.available,
            "unit": self.unit,
            "error": self.error,
        }


class PrometheusQueryClient:
    """
    Prometheus 只读查询客户端。

    功能说明：提供常用 Pod/Service 指标查询，直连真实 Prometheus HTTP API。
    参数说明：enabled 控制是否启用；base_url 是 Prometheus 地址（必须 http(s)）；timeout_seconds 是 HTTP 超时。
    返回值：各方法返回 PrometheusQueryResult。
    设计思路：enabled=false 或不可用时返回 available=False，不影响 K8s 诊断；绝不返回模拟指标值。
    使用示例：client.pod_cpu_usage("default", "api")
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        base_url: str = "http://127.0.0.1:9090",
        timeout_seconds: float = 5.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.enabled = enabled
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_settings(cls, settings: object) -> PrometheusQueryClient:
        """从 AthenaSettings.ops.prometheus 构造客户端。"""
        prometheus = getattr(getattr(settings, "ops"), "prometheus")
        return cls(
            enabled=prometheus.enabled,
            base_url=prometheus.base_url,
            timeout_seconds=prometheus.timeout_seconds,
        )

    def query(self, name: str, promql: str, unit: str = "") -> PrometheusQueryResult:
        """
        执行单个 PromQL 查询。

        功能说明：disabled 返回 unavailable；非 http(s) base_url 返回 unavailable；真实查询失败返回 unavailable（无 mock）。
        参数说明：name 指标名；promql 查询表达式；unit 展示单位。
        返回值：PrometheusQueryResult。
        设计思路：Prometheus 不可用不应阻断 K8s 诊断，所以失败转换为结构化不可用结果。
        使用示例：client.query("pod_cpu_usage", "sum(rate(...))", "cores")
        """
        if not promql.strip():
            raise ValueError("promql must be non-empty")
        if not self.enabled:
            return PrometheusQueryResult(
                name=name,
                query=promql,
                value=None,
                source=self.base_url,
                available=False,
                unit=unit,
                error="prometheus disabled",
            )
        if not self.base_url.startswith(("http://", "https://")):
            return PrometheusQueryResult(
                name=name,
                query=promql,
                value=None,
                source=self.base_url,
                available=False,
                unit=unit,
                error="prometheus base_url must be http(s); no mock data",
            )
        try:
            return self._real_query(name, promql, unit)
        except Exception as exc:
            logger.warning("prometheus query unavailable: %s", exc)
            return PrometheusQueryResult(
                name=name,
                query=promql,
                value=None,
                source=self.base_url,
                available=False,
                unit=unit,
                error=str(exc),
            )

    def pod_cpu_usage(self, namespace: str, pod: str) -> PrometheusQueryResult:
        """查询 Pod CPU 使用率（cores）。"""
        return self.query(
            "pod_cpu_usage",
            (
                "sum(rate(container_cpu_usage_seconds_total{"
                f"namespace=\"{namespace}\",pod=\"{pod}\",container!=\"\""
                "}[5m]))"
            ),
            "cores",
        )

    def pod_memory_usage(self, namespace: str, pod: str) -> PrometheusQueryResult:
        """查询 Pod 内存使用量（bytes）。"""
        return self.query(
            "pod_memory_usage",
            (
                "sum(container_memory_working_set_bytes{"
                f"namespace=\"{namespace}\",pod=\"{pod}\",container!=\"\""
                "})"
            ),
            "bytes",
        )

    def pod_restart_count(self, namespace: str, pod: str) -> PrometheusQueryResult:
        """查询 Pod 容器重启次数。"""
        return self.query(
            "pod_restart_count",
            (
                "sum(kube_pod_container_status_restarts_total{"
                f"namespace=\"{namespace}\",pod=\"{pod}\""
                "})"
            ),
            "count",
        )

    def http_5xx_error_rate(self, namespace: str, service: str) -> PrometheusQueryResult:
        """查询服务 HTTP 5xx 错误率。"""
        return self.query(
            "http_5xx_error_rate",
            (
                "sum(rate(http_requests_total{"
                f"namespace=\"{namespace}\",service=\"{service}\",status=~\"5..\""
                "}[5m]))"
            ),
            "rps",
        )

    def request_latency_p95(self, namespace: str, service: str) -> PrometheusQueryResult:
        """查询服务请求延迟 P95（秒）。"""
        return self.query(
            "request_latency_p95",
            (
                "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{"
                f"namespace=\"{namespace}\",service=\"{service}\""
                "}[5m])) by (le))"
            ),
            "seconds",
        )

    def request_latency_p99(self, namespace: str, service: str) -> PrometheusQueryResult:
        """查询服务请求延迟 P99（秒）。"""
        return self.query(
            "request_latency_p99",
            (
                "histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{"
                f"namespace=\"{namespace}\",service=\"{service}\""
                "}[5m])) by (le))"
            ),
            "seconds",
        )

    def service_availability(self, namespace: str, service: str) -> PrometheusQueryResult:
        """查询服务可用性比例。"""
        return self.query(
            "service_availability",
            (
                "avg_over_time(up{"
                f"namespace=\"{namespace}\",service=\"{service}\""
                "}[5m])"
            ),
            "ratio",
        )

    def pod_resource_snapshot(
        self, namespace: str, pod: str
    ) -> list[PrometheusQueryResult]:
        """一次性查询 Pod CPU、内存、重启次数。"""
        return [
            self.pod_cpu_usage(namespace, pod),
            self.pod_memory_usage(namespace, pod),
            self.pod_restart_count(namespace, pod),
        ]

    def service_snapshot(
        self, namespace: str, service: str
    ) -> list[PrometheusQueryResult]:
        """一次性查询服务 5xx、P95、可用性。"""
        return [
            self.http_5xx_error_rate(namespace, service),
            self.request_latency_p95(namespace, service),
            self.request_latency_p99(namespace, service),
            self.service_availability(namespace, service),
        ]

    def _real_query(
        self, name: str, promql: str, unit: str
    ) -> PrometheusQueryResult:
        encoded = urllib.parse.urlencode({"query": promql})
        url = f"{self.base_url.rstrip('/')}/api/v1/query?{encoded}"
        with urllib.request.urlopen(url, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("status") != "success":
            raise ValueError(str(payload.get("error", "unknown prometheus error")))
        value = self._extract_value(payload)
        return PrometheusQueryResult(
            name=name,
            query=promql,
            value=value,
            source=self.base_url,
            available=True,
            unit=unit,
        )

    @staticmethod
    def _extract_value(payload: dict[str, object]) -> float | None:
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        result_type = data.get("resultType")
        result = data.get("result")
        if result_type == "scalar" and isinstance(result, list) and len(result) >= 2:
            return float(result[1])
        if isinstance(result, list) and result:
            first = result[0]
            if isinstance(first, dict):
                value = first.get("value")
                if isinstance(value, list) and len(value) >= 2:
                    return float(value[1])
        return None
