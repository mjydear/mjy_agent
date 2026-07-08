"""Smoke tests for CloudOps vertical scenarios."""

from __future__ import annotations

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.api.services import AthenaWebService
from tests.test_web_console import build_test_agent


def build_cloud_client() -> TestClient:
    """Build an isolated Web API client for CloudOps tests."""
    service = AthenaWebService(agent_factory=build_test_agent, session_ttl_seconds=60)
    return TestClient(create_app(service=service))


def test_cloud_ops_modes_and_four_scenarios() -> None:
    client = build_cloud_client()

    modes = client.get("/api/cloud-ops/modes")
    assert modes.status_code == 200
    assert {mode["mode"] for mode in modes.json()} == {
        "k8s",
        "resource",
        "fault",
        "cost",
    }

    for mode in ("k8s", "resource", "fault", "cost"):
        response = client.post(
            "/api/cloud-ops/run", json={"mode": mode, "task": "KubePodCrashLooping"}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "success"
        assert payload["steps"]
        assert payload["answer"]


def test_cloud_ops_k8s_includes_readonly_findings() -> None:
    client = build_cloud_client()

    response = client.post(
        "/api/cloud-ops/run", json={"mode": "k8s", "task": "巡检集群"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    # 新增：只读诊断器产出的证据型结论应出现在结构化数据里
    findings = payload["data"]["readonly_findings"]
    assert findings
    symptoms = {item["symptom"] for item in findings}
    assert "CrashLoopBackOff" in symptoms
    crash = next(item for item in findings if item["symptom"] == "CrashLoopBackOff")
    assert crash["evidence"]  # 事件/日志证据


def test_cloud_ops_k8s_includes_structured_report() -> None:
    client = build_cloud_client()

    response = client.post(
        "/api/cloud-ops/run", json={"mode": "k8s", "task": "巡检集群"}
    )
    assert response.status_code == 200
    payload = response.json()
    # 阶段 2：结构化诊断报告应随 K8s 场景一起返回
    report = payload["data"]["readonly_report"]
    assert set(report.keys()) == {
        "summary",
        "namespace",
        "findings",
        "metrics",
        "actions",
        "raw_evidence",
    }
    assert report["namespace"] == "default"
    assert report["metrics"]["finding_count"] == len(report["findings"])
    status = payload["data"]["cloud_status"]
    assert status["source"] == "mock"
    assert status["k8s_context"] == "default"
    assert status["namespace_scope"] == ["*"]
    assert status["prometheus"]["enabled"] is False


def test_cloud_ops_k8s_parses_target_namespace() -> None:
    # 白名单为空（默认），命名空间解析应尊重用户输入
    parse = AthenaWebService._parse_k8s_namespace
    assert parse("诊断 prod 命名空间", ()) == "prod"
    assert parse("namespace=staging 有异常", ()) == "staging"
    assert parse("ns dev 巡检", ()) == "dev"
    assert parse("随便看看", ()) == "default"
    # 白名单越权：安全兜底回退 default
    assert parse("诊断 prod 命名空间", ("default", "dev")) == "default"
    # 白名单内的命名空间在文本出现时被采用
    assert parse("看看 dev 环境", ("default", "dev")) == "dev"


def test_cloud_ops_k8s_namespace_reflected_in_response() -> None:
    client = build_cloud_client()
    response = client.post(
        "/api/cloud-ops/run",
        json={"mode": "k8s", "task": "诊断 prod 命名空间"},
    )
    assert response.status_code == 200
    payload = response.json()
    # 无白名单时应诊断 prod，data.namespace 与报告命名空间一致
    assert payload["data"]["namespace"] == "prod"
    assert payload["data"]["readonly_report"]["namespace"] == "prod"


def test_cloud_ops_high_risk_requires_confirmation() -> None:
    client = build_cloud_client()

    blocked = client.post(
        "/api/cloud-ops/run", json={"mode": "resource", "task": "restart instance"}
    )
    assert blocked.status_code == 200
    assert blocked.json()["requires_confirmation"] is True
    assert blocked.json()["status"] == "waiting_confirmation"

    confirmed = client.post(
        "/api/cloud-ops/run",
        json={"mode": "resource", "task": "restart instance", "confirmed": True},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["requires_confirmation"] is False
    assert confirmed.json()["status"] == "success"


def test_cloud_ops_k8s_write_action_requires_confirmation() -> None:
    client = build_cloud_client()

    response = client.post(
        "/api/cloud-ops/run",
        json={
            "mode": "k8s",
            "task": "namespace=default rollout restart deployment checkout",
        },
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["status"] == "waiting_confirmation"
    assert payload["requires_confirmation"] is True
    action = payload["data"]["k8s_action"]
    assert action["plan"]["action_type"] == "rollout_restart_deployment"
    assert action["plan"]["command_preview"] == (
        "kubectl rollout restart deployment/checkout -n default"
    )
    assert action["plan"]["actor"] == "public"
    assert action["plan"]["required_scope"] == "cloud:execute"
    assert action["plan"]["security"]["rbac_scope_checked"] is True
    assert action["plan"]["rollback_suggestion"]
    assert payload["data"]["cloud_status"]["namespace"] == "default"


def test_cloud_ops_k8s_write_action_executes_after_confirmation() -> None:
    client = build_cloud_client()

    response = client.post(
        "/api/cloud-ops/run",
        json={
            "mode": "k8s",
            "task": "namespace=default scale deployment checkout replicas=2",
            "confirmed": True,
        },
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["status"] == "success"
    assert payload["requires_confirmation"] is False
    action = payload["data"]["k8s_action"]
    assert action["success"] is True
    assert action["plan"]["action_type"] == "scale_deployment"
    assert action["plan"]["parameters"]["replicas"] == 2
    assert action["verification"]["deployments"]


def test_cloud_ops_k8s_blocks_high_risk_action() -> None:
    client = build_cloud_client()

    response = client.post(
        "/api/cloud-ops/run",
        json={"mode": "k8s", "task": "delete namespace prod"},
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["status"] == "failed"
    assert payload["requires_confirmation"] is False
    action = payload["data"]["k8s_action"]
    assert action["success"] is False
    assert "blocked" in action["error"]


def test_cloud_ops_k8s_blocks_prod_write_by_default() -> None:
    client = build_cloud_client()

    response = client.post(
        "/api/cloud-ops/run",
        json={
            "mode": "k8s",
            "task": "environment=prod namespace=prod scale deployment checkout replicas=2",
        },
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["status"] == "failed"
    assert payload["requires_confirmation"] is False
    action = payload["data"]["k8s_action"]
    assert action["success"] is False
    assert "prod write is disabled" in action["error"]
    assert action["security"]["environment"] == "prod"


def test_cloud_ops_stream_and_knowledge() -> None:
    client = build_cloud_client()

    with client.stream(
        "POST",
        "/api/cloud-ops/stream",
        json={"mode": "fault", "task": "KubePodCrashLooping"},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "data:" in body
    assert "Root cause" in body

    knowledge = client.get("/api/cloud-ops/knowledge", params={"query": "CrashLoop"})
    assert knowledge.status_code == 200
    assert knowledge.json()["items"]
