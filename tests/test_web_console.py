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
