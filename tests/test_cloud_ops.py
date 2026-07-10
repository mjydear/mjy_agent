"""Smoke tests for CloudOps vertical scenarios."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.api.services import AthenaWebService
from athena.config import AthenaSettings
from athena.exceptions import ConfigError
from athena.tools.cloud.k8s import K8sReadOnlyDiagnoser
from tests._k8s_fakes import demo_client
from tests.test_web_console import build_test_agent


def build_cloud_client() -> TestClient:
    """Build an isolated Web API client for CloudOps tests."""
    # 生产已无 mock：注入 Demo 只读诊断器 + 测试 Agent，保证 CloudOps 测试确定且离线。
    service = AthenaWebService(
        agent_factory=build_test_agent,
        cloud_agent_factory=lambda actor: build_test_agent(),
        k8s_diagnoser=K8sReadOnlyDiagnoser(demo_client()),
        session_ttl_seconds=60,
    )
    return TestClient(create_app(service=service))


def test_cloud_ops_modes_and_scenarios() -> None:
    client = build_cloud_client()

    # CloudOps 现仅支持 k8s / fault 两种模式（resource/cost 已移除）。
    modes = client.get("/api/cloud-ops/modes")
    assert modes.status_code == 200
    assert {mode["mode"] for mode in modes.json()} == {"k8s", "fault"}

    for mode in ("k8s", "fault"):
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
# 进行测试以确认输出的正常属性由调用情况而定。返回信息应体现所有修复后的状态和版本信息; 需要确保无论是 mock 方式还是 real 方式都是正确的。

# 进行测试以确认输出的正常属性由调用情况而定。返回信息应体现所有修复后的状态和版本信息; 需要确保无论是 mock 方式还是 real 方式都是正确的。

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
    # 真实链路，无 mock：source 恒为 real，不存在降级。
    assert status["source"] == "real"
    assert status["k8s_context"] == "default"
    assert status["namespace_scope"] == ["*"]
    assert status["prometheus"]["enabled"] is False
    # 缺口1：真实链路 degraded 恒为 False
    assert status["degraded"] is False
    # 缺口2：遗留 builtin 冗余字段已移除
    assert "snapshot" not in payload["data"]
    assert "diagnoses" not in payload["data"]


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
    # fault 场景现由 Agent 驱动，最终答案来自测试 Agent（"web ok"）。
    assert "web ok" in body

    # _run_fault_ops 会把该告警记入运维知识库，据此可检索到对应案例。
    knowledge = client.get(
        "/api/cloud-ops/knowledge", params={"query": "KubePodCrashLooping"}
    )
    assert knowledge.status_code == 200
    assert knowledge.json()["items"]


def test_require_durable_audit_raises_without_redis() -> None:
    # 缺口4：强制持久化审计但未配置 Redis → 启动即快速失败
    settings = AthenaSettings()
    settings.ops.require_durable_audit = True
    settings.cache.redis_url = None
    service = AthenaWebService(agent_factory=build_test_agent, session_ttl_seconds=60)
    with pytest.raises(ConfigError):
        create_app(settings=settings, service=service)


def test_durable_audit_ok_with_redis_url() -> None:
    # 配置了 redis_url 时不抛错（连接由 create_cache 内部降级处理，此处只校验配置约束）
    settings = AthenaSettings()
    settings.ops.require_durable_audit = True
    settings.cache.redis_url = "redis://localhost:6379/0"
    service = AthenaWebService(agent_factory=build_test_agent, session_ttl_seconds=60)
    app = create_app(settings=settings, service=service)
    assert app is not None
