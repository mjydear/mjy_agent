"""
📦 模块名称：Athena Web Console API 测试
📍 架构位置：测试层，位于 FastAPI TestClient 和 Web API 服务层之间。
🎯 核心作用：验证 Web 首页、会话、对话、流式输出、工作流、轨迹、指标和 Benchmark 接口可用。
🔗 依赖关系：依赖 create_app、AthenaWebService、ReActAgent 和 FastAPI TestClient；被 pytest 执行。
💡 设计思路：使用假 LLM + 真实 ReActAgent + 注入式 service，既避免外部 API 依赖，又覆盖真实 API 路由。
📚 学习重点：看 build_client() 如何把测试专用 service 注入 create_app()，这是可测试架构的关键。
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi.testclient import TestClient

from athena.agent import ReActAgent
from athena.api.server import create_app
from athena.api.services import AthenaWebService
from athena.config import AthenaSettings
from athena.infra.llm import LLMClient, LLMMessage, LLMResponse
from athena.memory import WorkingMemory
from athena.prompt import ContextAssembler
from athena.tools import ToolRegistry


class StaticLLMClient(LLMClient):
    """
    测试专用确定性 LLM 客户端。

    功能说明：不访问外部模型，固定返回一个 ReAct JSON 结果。
    参数说明：继承 LLMClient，complete() 接收消息序列。
    返回值：LLMResponse。
    设计思路：API 测试应该稳定、快速、离线可运行，所以不能依赖真实 API Key。
    使用示例：StaticLLMClient().complete(messages)
    """

    async def complete(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        """
        返回固定最终答案。

        功能说明：模拟 LLM 输出一个合法 ReAct 决策。
        参数说明：messages 是 prompt 消息；这里不读取内容，因为测试只关心 API 链路。
        返回值：LLMResponse，content 是 JSON 字符串。
        设计思路：固定输出能让断言稳定，不会因为模型随机性导致测试偶发失败。
        使用示例：response = await client.complete(messages)
        """
        return LLMResponse(
            content='{"thought":"answer directly","action":null,"action_input":{},"final_answer":"web ok"}',
            model="static",
        )


def build_test_agent() -> ReActAgent:
    """
    构造测试用 ReActAgent。

    功能说明：组装假 LLM、PromptAssembler、ToolRegistry 和 WorkingMemory。
    参数说明：无。
    返回值：ReActAgent。
    设计思路：用真实 Agent 执行循环覆盖更多代码，但把 LLM 换成稳定假实现。
    使用示例：agent = build_test_agent()
    """
    return ReActAgent(
        llm_client=StaticLLMClient(),
        prompt_assembler=ContextAssembler(),
        tool_registry=ToolRegistry(),
        memory=WorkingMemory(),
        max_steps=1,
    )


def build_client() -> TestClient:
    """
    构造带注入服务的 FastAPI 测试客户端。

    功能说明：创建 AthenaWebService，并传给 create_app(service=...)。
    参数说明：无。
    返回值：TestClient。
    设计思路：依赖注入让测试不用启动真实 uvicorn，也不用真实模型服务。
    使用示例：client = build_client()

    🎯 面试考点：为什么测试不直接请求正在运行的 8000 端口？答案：端到端环境不稳定，TestClient 更快、更可控。
    """
    service = AthenaWebService(
        agent_factory=build_test_agent, session_ttl_seconds=60
    )  # 💡 学习提示：每个测试 client 都有独立内存状态，测试之间互不污染。
    return TestClient(create_app(service=service))


def test_web_console_root_and_session_chat() -> None:
    """
    验证首页、创建会话、同步对话和指标接口。

    功能说明：覆盖用户打开页面并发送一条消息的最小闭环。
    参数说明：无。
    返回值：None，失败时 pytest 会报告断言错误。
    设计思路：一个测试覆盖一条最常用用户路径，比只测单个函数更接近真实使用。
    使用示例：pytest tests/test_web_console.py
    """
    client = build_client()

    root = client.get("/")
    assert root.status_code == 200
    assert "Athena Agent Web Console" in root.text

    session_response = client.post("/api/sessions", json={"title": "test"})
    assert session_response.status_code == 200
    session_id = session_response.json()["session"][
        "session_id"
    ]  # 💡 学习提示：后续 chat 必须使用真实返回的 session_id，不能手写假 id。

    chat_response = client.post(
        "/api/chat", json={"session_id": session_id, "message": "hello"}
    )
    assert chat_response.status_code == 200
    assert chat_response.json()["answer"] == "web ok"

    metrics_response = client.get("/api/metrics")
    assert metrics_response.status_code == 200
    assert metrics_response.json()["total_tasks"] == 1


def test_http_sessions_and_chat_are_tenant_scoped() -> None:
    """All session/chat HTTP paths must use the request TenantContext."""
    settings = AthenaSettings()
    settings.security.require_auth = True
    settings.security.api_keys = {"key-a": "tenant-a", "key-b": "tenant-b"}
    client = TestClient(
        create_app(
            settings=settings,
            service=AthenaWebService(
                agent_factory=build_test_agent, session_ttl_seconds=60
            ),
        )
    )
    headers_a = {"X-API-Key": "key-a"}
    headers_b = {"X-API-Key": "key-b"}

    session_a = client.post(
        "/api/sessions", headers=headers_a, json={"title": "tenant-a"}
    ).json()["session"]["session_id"]
    session_b = client.post(
        "/api/sessions", headers=headers_b, json={"title": "tenant-b"}
    ).json()["session"]["session_id"]

    assert [
        item["session_id"]
        for item in client.get("/api/sessions", headers=headers_a).json()
    ] == [session_a]
    assert [
        item["session_id"]
        for item in client.get("/api/sessions", headers=headers_b).json()
    ] == [session_b]

    assert (
        client.get(f"/api/sessions/{session_a}", headers=headers_b).status_code == 404
    )
    assert (
        client.delete(f"/api/sessions/{session_a}", headers=headers_b).status_code
        == 404
    )
    assert (
        client.post(
            "/api/chat",
            headers=headers_b,
            json={"session_id": session_a, "message": "cross tenant"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/chat/stream",
            headers=headers_b,
            json={"session_id": session_a, "message": "cross tenant"},
        ).status_code
        == 404
    )

    response = client.post(
        "/api/chat",
        headers=headers_a,
        json={"session_id": session_a, "message": "own session"},
    )
    assert response.status_code == 200
    assert response.json()["answer"] == "web ok"


def test_web_console_static_assets_include_cloud_ops_report_renderer() -> None:
    """
    验证 Web 控制台静态资源包含 CloudOps 结构化报告渲染能力。

    功能说明：请求 index/app.js/style.css，确认页面引用新静态资源版本，脚本与样式包含 readonly_report 展示入口。
    参数说明：无。
    返回值：None。
    设计思路：项目当前没有浏览器端测试框架，用 TestClient 做静态资源冒烟，避免引入前端构建链。
    使用示例：pytest tests/test_web_console.py -k static_assets
    """
    client = build_client()

    root = client.get("/")
    assert root.status_code == 200
    assert "/static/app.js?v=20260719-01" in root.text
    assert "/static/style.css?v=20260713-01" in root.text
    assert 'data-tab="alerts"' in root.text
    assert "api-key-trigger" in root.text
    assert "llm-api-key" in root.text

    script = client.get("/static/app.js?v=20260719-01")
    assert script.status_code == 200
    assert "getLlmConfigPayload" in script.text
    assert "llm_config" in script.text
    assert "/api/llm/configs" in script.text
    assert "localStorage.setItem" not in script.text
    assert "cloudReport" in script.text
    assert "cloudStatus" in script.text
    assert "cloudAction" in script.text
    assert "alertHistory" in script.text
    assert "renderCloudReport" in script.text
    assert "renderCloudStatus" in script.text
    assert "renderK8sAction" in script.text
    assert "renderAlertHistory" in script.text
    assert "readonly_report" in script.text
    assert "cloud_status" in script.text
    assert "rollback_suggestion" in script.text

    style = client.get("/static/style.css?v=20260713-01")
    assert style.status_code == 200
    assert ".ops-report" in style.text
    assert ".ops-finding" in style.text
    assert ".ops-status-grid" in style.text
    assert ".ops-action-panel" in style.text
    assert ".llm-settings-btn" in style.text
    assert ".settings-modal" in style.text


def test_ops_task_workbench_assets_enforce_fact_first_recovery() -> None:
    """The existing console exposes the P1 workbench and its SSE guarantees."""
    client = build_client()

    root = client.get("/")
    assert root.status_code == 200
    assert 'id="ops-task-mode"' in root.text
    assert 'data-mode="ops-task"' in root.text
    assert 'id="ops-task-workbench"' in root.text
    assert 'id="ops-task-cancel"' not in root.text
    assert 'aria-label="故障任务详情"' in root.text
    assert 'aria-live="polite"' in root.text
    assert "/static/ops-task-workbench.js?v=20260713-01" in root.text

    workbench = client.get("/static/ops-task-workbench.js?v=20260713-01")
    assert workbench.status_code == 200
    script = workbench.text
    for event_type in (
        "task.created",
        "task.started",
        "task.input_received",
        "tool.finished",
        "task.completed",
        "task.failed",
        "task.cancelled",
    ):
        assert event_type in script
    assert "source.addEventListener(eventType" in script
    assert ".onmessage" not in script
    assert "after_seq=${cursor}" in script
    recovery = script[
        script.index("function recoverAfterDisconnect") : script.index(
            "async function cancelCurrentTask"
        )
    ]
    assert "await refreshTaskDetail(taskId, token)" in recovery
    assert recovery.index("await refreshTaskDetail(taskId, token)") < recovery.index(
        "connectEventStream(taskId, token, state.lastSequence"
    )
    assert "/evidence" in script
    assert "/cancel" in script
    assert "data_origin" in script
    assert "environment_mode" in script
    assert "降级状态" in script
    assert "thought" not in script.lower()

    app_script = client.get("/static/app.js?v=20260719-01").text
    assert "AthenaOpsTaskWorkbench?.init()" in app_script
    assert "AthenaOpsTaskWorkbench?.setActive" in app_script
    assert "elements.consoleComposer.hidden = opsTaskActive" in app_script
    assert "elements.detailAside.hidden = opsTaskActive" in app_script
    assert 'elements.detailPanel.textContent = ""' in app_script

    style = client.get("/static/style.css?v=20260713-01").text
    assert ".ops-task-layout" in style
    assert ".ops-task-degradation" in style
    assert "@media (max-width: 720px)" in style
    assert ".ops-task-workbench[hidden]" in style


def test_chat_accepts_frontend_llm_config(monkeypatch) -> None:
    """
    验证前端传入的模型配置会用于构建会话 Agent。
    """
    captured = {}

    def fake_build_agent(**kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return build_test_agent()

    monkeypatch.setattr("athena.bootstrap.build_agent", fake_build_agent)
    client = TestClient(
        create_app(
            service=AthenaWebService(
                agent_factory=build_test_agent, session_ttl_seconds=60
            )
        )
    )
    session_id = client.post("/api/sessions", json={"title": "llm"}).json()["session"][
        "session_id"
    ]

    response = client.post(
        "/api/chat",
        json={
            "session_id": session_id,
            "message": "hello",
            "llm_config": {
                "provider": "litellm",
                "model": "deepseek-chat",
                "api_key": "sk-test-key",
            },
        },
    )

    assert response.status_code == 200
    assert captured["llm_provider"] == "litellm"
    assert captured["llm_model"] == "deepseek/deepseek-chat"
    assert captured["llm_api_key"] == "sk-test-key"


def test_llm_configs_are_server_managed_and_redacted() -> None:
    client = build_client()
    assert client.get("/api/llm/configs").json() == []

    response = client.post(
        "/api/llm/configs",
        json={
            "provider": "deepseek",
            "display_name": "DeepSeek Chat",
            "model": "deepseek/deepseek-chat",
            "api_key": "sk-secret",
        },
    )
    assert response.status_code == 201
    config = response.json()
    assert config["has_api_key"] is True
    assert "api_key" not in config
    assert config["is_default"] is True

    listed = client.get("/api/llm/configs").json()
    assert listed == [config]
    assert client.delete(f"/api/llm/configs/{config['config_id']}").status_code == 204
    assert client.get("/api/llm/configs").json() == []


def test_llm_config_requires_provider_credentials() -> None:
    client = build_client()
    response = client.post(
        "/api/llm/configs",
        json={
            "provider": "openai",
            "display_name": "GPT",
            "model": "gpt-4o",
        },
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "LLM_CREDENTIAL_REQUIRED"


def test_workflow_and_benchmark_routes() -> None:
    """
    验证工作流、轨迹和 Benchmark 接口。

    功能说明：覆盖 Web Console 右侧详情和 Benchmark Tab 依赖的后端接口。
    参数说明：无。
    返回值：None。
    设计思路：这些接口不依赖真实 LLM，适合放在快速回归测试里。
    使用示例：pytest tests/test_web_console.py -k workflow
    """
    client = build_client()

    workflow_response = client.post(
        "/api/workflow/run",
        json={"task": "collect logs; validate", "workflow_type": "plan_execute"},
    )
    assert workflow_response.status_code == 200
    task_id = workflow_response.json()["task_id"]
    assert client.get(f"/api/workflow/{task_id}/status").json()["status"] == "success"
    assert client.get(f"/api/traces/{task_id}").json()["events"]

    benchmark_response = client.post("/api/benchmark/run", json={"case_set": "smoke"})
    assert benchmark_response.status_code == 200
    run_id = benchmark_response.json()["run_id"]
    assert (
        "Success Rate" in client.get(f"/api/benchmark/{run_id}/report").json()["report"]
    )


def test_web_console_delete_session_does_not_regress() -> None:
    """
    验证删除会话功能：创建后删除，会话从列表消失且再次访问返回 404。

    功能说明：覆盖 Stage 10「删除会话功能不回归」验收项，防止云运维改动破坏基础会话管理。
    参数说明：无。
    返回值：None。
    设计思路：删除是不可逆操作，必须验证删除确认、列表移除与重复删除的清晰错误。
    使用示例：pytest tests/test_web_console.py -k delete_session
    """
    client = build_client()

    session_id = client.post("/api/sessions", json={"title": "to-delete"}).json()[
        "session"
    ]["session_id"]

    # 删除前会话存在于列表中
    before = client.get("/api/sessions").json()
    assert any(item["session_id"] == session_id for item in before)

    delete_response = client.request("DELETE", f"/api/sessions/{session_id}")
    assert delete_response.status_code == 200
    assert delete_response.json()["deleted"] == session_id

    # 删除后列表不再包含该会话，且详情查询返回错误状态（会话不存在）
    after = client.get("/api/sessions").json()
    assert all(item["session_id"] != session_id for item in after)
    assert client.get(f"/api/sessions/{session_id}").status_code >= 400

    # 重复删除返回清晰的 404，而不是静默成功
    assert client.request("DELETE", f"/api/sessions/{session_id}").status_code == 404


def test_stream_chat_returns_sse_events() -> None:
    """
    验证流式对话返回 SSE 格式。

    功能说明：调用 /api/chat/stream，确认响应文本里包含 data: 和最终答案。
    参数说明：无。
    返回值：None。
    设计思路：不在测试里逐帧模拟浏览器，只确认后端确实按 SSE 文本协议输出。
    使用示例：pytest tests/test_web_console.py -k stream
    """
    client = build_client()
    session_id = client.post("/api/sessions", json={"title": "stream"}).json()[
        "session"
    ]["session_id"]

    with client.stream(
        "POST", "/api/chat/stream", json={"session_id": session_id, "message": "hello"}
    ) as response:
        assert response.status_code == 200
        body = "".join(
            response.iter_text()
        )  # 💡 学习提示：TestClient 把流式文本迭代出来，拼接后检查协议片段。

    assert "data:" in body
    assert "web ok" in body


"""
🤔 思考题：

1. 如果真实 LLM 返回格式变化，这组测试能发现吗？为什么？
2. 如果要测试无效 session_id 返回标准错误，应该新增怎样的断言？
3. 为什么测试里要覆盖 SSE，而不只测普通 /api/chat？
4. ⚡ 优化建议：未来可以增加前端端到端测试，用浏览器自动点击页面验证交互。
"""
