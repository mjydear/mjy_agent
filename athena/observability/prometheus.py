"""
Prometheus 指标导出：定义并暴露企业级监控指标。

指标覆盖：请求量(QPS)、请求延迟(P99可算)、错误率、LLM token 消耗、
缓存命中率、向量检索耗时。所有指标注册到自定义 registry，经 /api/metrics/prometheus 暴露。
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class PrometheusMetrics:
    """集中管理所有 Prometheus 指标的容器，避免重复注册。"""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()

        self.http_requests_total = Counter(
            "athena_http_requests_total",
            "HTTP 请求总数",
            ["method", "path", "status"],
            registry=self.registry,
        )
        self.http_request_duration_seconds = Histogram(
            "athena_http_request_duration_seconds",
            "HTTP 请求耗时（秒）",
            ["method", "path"],
            buckets=(0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0, 2.0, 5.0),
            registry=self.registry,
        )
        self.http_errors_total = Counter(
            "athena_http_errors_total",
            "HTTP 5xx/4xx 错误总数",
            ["method", "path", "status"],
            registry=self.registry,
        )
        self.llm_tokens_total = Counter(
            "athena_llm_tokens_total",
            "LLM token 消耗总数",
            ["model", "kind"],  # kind: prompt / completion
            registry=self.registry,
        )
        self.cache_hit_ratio = Gauge(
            "athena_cache_hit_ratio",
            "缓存命中率（0-1）",
            ["namespace"],
            registry=self.registry,
        )
        self.vector_retrieval_seconds = Histogram(
            "athena_vector_retrieval_seconds",
            "向量检索耗时（秒）",
            buckets=(0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5),
            registry=self.registry,
        )
        self.incidents_total = Counter(
            "athena_incidents_total",
            "按严重级别统计的故障事件数",
            ["severity"],
            registry=self.registry,
        )

    def observe_request(
        self, method: str, path: str, status: int, duration_seconds: float
    ) -> None:
        """记录一次 HTTP 请求的量、耗时与错误。"""
        status_str = str(status)
        self.http_requests_total.labels(method, path, status_str).inc()
        self.http_request_duration_seconds.labels(method, path).observe(
            duration_seconds
        )
        if status >= 400:
            self.http_errors_total.labels(method, path, status_str).inc()

    def observe_tokens(self, model: str, prompt: int, completion: int) -> None:
        """记录 LLM token 消耗。"""
        if prompt:
            self.llm_tokens_total.labels(model, "prompt").inc(prompt)
        if completion:
            self.llm_tokens_total.labels(model, "completion").inc(completion)

    def observe_vector_retrieval(self, duration_seconds: float) -> None:
        """记录一次向量检索耗时。"""
        self.vector_retrieval_seconds.observe(duration_seconds)

    def set_cache_hit_ratio(self, namespace: str, ratio: float) -> None:
        """更新缓存命中率快照。"""
        self.cache_hit_ratio.labels(namespace).set(ratio)

    def observe_incident(self, severity: str) -> None:
        """按级别累加故障事件。"""
        self.incidents_total.labels(severity).inc()

    def render(self) -> tuple[bytes, str]:
        """导出 Prometheus 文本格式，返回 (body, content_type)。"""
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
