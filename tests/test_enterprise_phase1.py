"""阶段1 工程化加固测试：异步任务、幂等、鉴权、多租户、统一响应体、trace_id。"""

from __future__ import annotations

import time
from collections.abc import Sequence

from fastapi.testclient import TestClient

from athena.agent import ReActAgent
from athena.api.server import create_app
from athena.api.services import AthenaWebService
from athena.config import AthenaSettings, SecuritySettings
from athena.infra.llm import LLMClient, LLMMessage, LLMResponse
from athena.memory import WorkingMemory
from athena.prompt import ContextAssembler
from athena.tools import ToolRegistry


class StaticLLMClient(LLMClient):
    async def complete(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        return LLMResponse(
            content='{"thought":"answer","action":null,"action_input":{},"final_answer":"async ok"}',
            model="static",
        )


def build_test_agent() -> ReActAgent:
    return ReActAgent(
        llm_client=StaticLLMClient(),
        prompt_assembler=ContextAssembler(),
        tool_registry=ToolRegistry(),
        memory=WorkingMemory(),
        max_steps=1,
    )


def build_client(settings: AthenaSettings | None = None) -> TestClient:
    service = AthenaWebService(agent_factory=build_test_agent, session_ttl_seconds=60)
    return TestClient(create_app(settings=settings, service=service))


def _poll_task(client: TestClient, task_id: str, headers: dict | None = None) -> dict:
    """轮询任务直到完成，返回最终任务状态字典。"""
    for _ in range(50):
        resp = client.get(f"/api/tasks/{task_id}", headers=headers or {})
        assert resp.status_code == 200
        data = resp.json()["data"]
        if data["status"] in {"success", "failed"}:
            return data
        time.sleep(0.02)
    raise AssertionError("task did not finish in time")


def test_async_task_submit_and_poll() -> None:
    """提交异步 chat 任务 → 拿 task_id → 轮询到 success，结果含答案。"""
    client = build_client()
    session_id = client.post("/api/sessions", json={"title": "t"}).json()["session"][
        "session_id"
    ]

    submit = client.post(
        "/api/tasks",
        json={"kind": "chat", "session_id": session_id, "message": "hi"},
    )
    assert submit.status_code == 200
    body = submit.json()
    # 统一响应体结构
    assert body["code"] == 0
    assert body["trace_id"]
    task_id = body["data"]["task_id"]
    assert body["data"]["status"] == "pending"

    final = _poll_task(client, task_id)
    assert final["status"] == "success"
    assert final["result"]["answer"] == "async ok"


def test_idempotent_submit_returns_same_task_id() -> None:
    """携带相同 Idempotency-Key 的重复提交返回同一个 task_id。"""
    client = build_client()
    session_id = client.post("/api/sessions", json={"title": "t"}).json()["session"][
        "session_id"
    ]
    headers = {"Idempotency-Key": "req-123"}
    payload = {"kind": "chat", "session_id": session_id, "message": "hi"}

    first = client.post("/api/tasks", json=payload, headers=headers).json()
    second = client.post("/api/tasks", json=payload, headers=headers).json()
    assert first["data"]["task_id"] == second["data"]["task_id"]


def test_trace_id_header_present() -> None:
    """每个响应都带 X-Trace-Id 头。"""
    client = build_client()
    resp = client.get("/api/metrics")
    assert resp.headers.get("X-Trace-Id")


def _auth_settings() -> AthenaSettings:
    return AthenaSettings(
        security=SecuritySettings(
            api_keys={"key-a": "tenant-a", "key-b": "tenant-b"},
            require_auth=True,
        )
    )


def test_auth_required_rejects_missing_key() -> None:
    """开启鉴权后，缺少 API Key 的写请求返回 401。"""
    client = build_client(settings=_auth_settings())
    session_id = client.post("/api/sessions", json={"title": "t"}).json()["session"][
        "session_id"
    ]
    resp = client.post(
        "/api/tasks",
        json={"kind": "chat", "session_id": session_id, "message": "hi"},
    )
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "UNAUTHORIZED"


def test_tenant_isolation_on_task_get() -> None:
    """租户 A 的任务不能被租户 B 查询到（返回 404）。"""
    client = build_client(settings=_auth_settings())
    session_id = client.post("/api/sessions", json={"title": "t"}).json()["session"][
        "session_id"
    ]
    submit = client.post(
        "/api/tasks",
        json={"kind": "chat", "session_id": session_id, "message": "hi"},
        headers={"X-API-Key": "key-a"},
    )
    task_id = submit.json()["data"]["task_id"]

    # 租户 B 用自己的 key 查询 A 的任务 → 404
    resp_b = client.get(f"/api/tasks/{task_id}", headers={"X-API-Key": "key-b"})
    assert resp_b.status_code == 404

    # 租户 A 能正常查询
    final = _poll_task(client, task_id, headers={"X-API-Key": "key-a"})
    assert final["status"] == "success"
