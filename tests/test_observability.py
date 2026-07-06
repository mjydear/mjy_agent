"""阶段4 可观测性测试：Prometheus 指标导出与请求埋点。"""

from __future__ import annotations

from athena.observability.prometheus import PrometheusMetrics


def test_prometheus_records_requests_and_errors() -> None:
    m = PrometheusMetrics()
    m.observe_request("GET", "/api/chat", 200, 0.12)
    m.observe_request("GET", "/api/chat", 500, 0.30)
    body, content_type = m.render()
    text = body.decode("utf-8")
    assert "text/plain" in content_type
    assert "athena_http_requests_total" in text
    assert "athena_http_errors_total" in text
    assert 'status="500"' in text


def test_prometheus_tokens_and_cache_and_vector() -> None:
    m = PrometheusMetrics()
    m.observe_tokens("deepseek-chat", prompt=100, completion=50)
    m.set_cache_hit_ratio("athena", 0.85)
    m.observe_vector_retrieval(0.03)
    m.observe_incident("L3")
    text = m.render()[0].decode("utf-8")
    assert "athena_llm_tokens_total" in text
    assert "athena_cache_hit_ratio" in text
    assert "athena_vector_retrieval_seconds" in text
    assert "athena_incidents_total" in text


def test_prometheus_endpoint_via_app() -> None:
    from fastapi.testclient import TestClient

    from athena.api.server import create_app
    from athena.config import load_settings

    settings = load_settings()
    settings.rate_limit.enabled = False  # 避免限流干扰
    app = create_app(settings=settings)
    client = TestClient(app)

    resp = client.get("/api/metrics/prometheus")
    assert resp.status_code == 200
    # 第二次抓取时，前一次请求已被埋点计数
    resp2 = client.get("/api/metrics/prometheus")
    assert resp2.status_code == 200
    assert "athena_http_requests_total" in resp2.text
    assert "/api/metrics/prometheus" in resp2.text
