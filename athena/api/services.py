"""
📦 模块名称：Athena Web API 服务封装层
📍 架构位置：接口服务层核心，位于 FastAPI 路由和 Agent/Workflow/Benchmark 核心模块之间。
🎯 核心作用：管理 Web 会话、任务状态、执行轨迹和指标，并把 HTTP 请求转发给已有 Agent 能力。
🔗 依赖关系：依赖 ReActAgent、WorkflowEngine、BenchmarkEngine、RuntimeMetrics；被 athena/api/routes 下的所有路由依赖。
💡 设计思路：使用“门面模式 Facade + 依赖注入”。路由只管收发 HTTP，复杂业务统一交给 AthenaWebService。
📚 学习重点：重点看一个请求如何经过 session → task record → agent/workflow → metrics/trace → response 这条链路。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

from athena.agent import (
    ExecutorAgent,
    PlannerAgent,
    ReActAgent,
    ValidatorAgent,
    WorkflowEngine,
)
from athena.agent.base import AgentResponse
from athena.agent.workflows import FaultDiagnoseWorkflow
from athena.api.schemas import (
    BenchmarkReportResponse,
    BenchmarkRunResponse,
    ChatMessage,
    ChatResponse,
    CloudOpsMode,
    CloudOpsResponse,
    MetricsResponse,
    SessionDetail,
    SessionSummary,
    StepTrace,
    TraceResponse,
    WorkflowRunResponse,
    WorkflowStatusResponse,
)
from athena.evaluation import BenchmarkCase, BenchmarkEngine, BenchmarkReport
from athena.learning.tracer import Tracer
from athena.memory.ops_knowledge import OpsKnowledgeBase
from athena.observability.metrics import RuntimeMetrics
from athena.tools.builtin.cloud import (
    AliyunClient,
    AWSClient,
    CloudOperationResult,
    TencentCloudClient,
)
from athena.tools.builtin.k8s import K8sDiagnoser, K8sOpsTools

AgentFactory = Callable[[], ReActAgent]


class ApiServiceError(Exception):
    """
    API 服务层可预期错误。

    功能说明：把“会话不存在、任务不存在、工作流类型不支持”等业务错误包装成稳定错误码。
    参数说明：
        error_code：给前端或测试判断用的稳定错误码。
        message：给用户看的错误说明。
    返回值：异常对象本身不返回值，被 FastAPI 异常处理器捕获。
    设计思路：不要直接把内部异常暴露给浏览器，而是转换成可控的错误响应。
    使用示例：raise ApiServiceError("SESSION_NOT_FOUND", "Session not found")

    🎯 面试考点：为什么不用普通 ValueError？答案：ValueError 没有稳定业务错误码，前端难以区分错误类型。
    """

    def __init__(self, error_code: str, message: str, status_code: int = 400) -> None:
        """保存错误码和错误信息，供异常处理器读取。"""
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code  # 支持 401/403/409 等语义化 HTTP 状态


@dataclass
class WebSession:
    """
    Web 控制台里的一个独立会话。

    功能说明：保存会话 id、标题、独立 Agent 实例和聊天消息历史。
    参数说明：
        session_id：会话唯一 id。
        title：侧边栏展示的会话标题。
        agent：这个会话专属的 ReActAgent。
        created_at/updated_at：创建和最近更新时间，用于过期清理。
        messages：用户和助手的消息历史。
    返回值：数据容器，不主动返回。
    设计思路：每个会话持有自己的 Agent，能保证工作记忆互相隔离。
    使用示例：WebSession("session-1", "Demo", agent)
    """

    session_id: str
    title: str
    agent: ReActAgent
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    messages: list[ChatMessage] = field(default_factory=list)


@dataclass
class TaskRecord:
    """
    一次任务的运行状态和轨迹记录。

    功能说明：保存 chat/stream/workflow/benchmark 任务的状态、答案、步骤和错误。
    参数说明：
        task_id：任务唯一 id。
        status：running/success/failed 等状态。
        answer：最终答案，可为空。
        steps：执行轨迹，右侧详情面板会读取它。
        error：失败原因，可为空。
        created_at/updated_at：任务时间戳。
    返回值：数据容器，不主动返回。
    设计思路：把执行结果和可观测信息存在同一条记录里，前端查状态和查 trace 都能复用。
    使用示例：TaskRecord(task_id="chat-1", status="running")
    """

    task_id: str
    status: str
    answer: str | None = None
    steps: list[StepTrace] = field(default_factory=list)
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class AthenaWebService:
    """
    Web API 的业务门面。

    功能说明：统一管理会话、对话、流式输出、多 Agent 工作流、Benchmark、指标和轨迹。
    参数说明：
        agent_factory：创建 ReActAgent 的工厂函数，每个 session 调一次。
        session_ttl_seconds：会话多长时间不活跃后过期。
        tracer：可选追踪器，这里预留给后续更完整的可观测系统。
        metrics：可选指标对象，测试或外部监控可以传入共享实例。
    返回值：构造函数不返回；各业务方法返回 Pydantic Response 模型。
    设计思路：门面模式把多模块能力包成一个简单对象，路由层只需要调用 service.xxx()。
    使用示例：
        service = AthenaWebService(agent_factory=build_agent)
        session = service.create_session("demo")

    🔍 原理讲解：
    浏览器请求不会直接调用 Agent，而是先进入 route，再进入 service。
    举个例子：
    POST /api/chat → chat.py 路由 → service.chat() → session.agent.run() → ChatResponse。
    """

    def __init__(
        self,
        agent_factory: AgentFactory,
        session_ttl_seconds: int = 3600,
        tracer: Tracer | None = None,
        metrics: RuntimeMetrics | None = None,
        session_store: "SessionStore | None" = None,
        ops_knowledge: "OpsKnowledgeBase | None" = None,
        workflow_llm: "object | None" = None,
        embedding_provider: "object | None" = None,
        task_store: "TaskStore | None" = None,
        metrics_store: "MetricsStore | None" = None,
        benchmark_store: "BenchmarkStore | None" = None,
        audit_store: "object | None" = None,
    ) -> None:
        if session_ttl_seconds <= 0:
            raise ValueError("session_ttl_seconds must be positive")
        self.agent_factory = agent_factory  # 💡 学习提示：保存工厂而不是保存单个 Agent，是为了每个会话都能新建独立 Agent。
        self.session_ttl_seconds = session_ttl_seconds
        self.tracer = tracer or Tracer()
        self.metrics = metrics or RuntimeMetrics()
        # 会话持久化：默认内存后端，生产由 create_app 注入 Redis 后端的 SessionStore
        if session_store is None:
            from athena.api.session_store import SessionStore
            from athena.infra.cache import InMemoryCache

            session_store = SessionStore(
                InMemoryCache(namespace="athena"), ttl_seconds=session_ttl_seconds
            )
        self.session_store = session_store
        # 任务/评测/指标持久化：默认内存后端，生产由 create_app 注入 Redis 后端
        from athena.api.task_store import BenchmarkStore, MetricsStore, TaskStore
        from athena.infra.cache import InMemoryCache as _InMemoryCache

        _shared_cache = getattr(self.session_store, "_cache", None) or _InMemoryCache(
            namespace="athena"
        )
        self.task_store = task_store or TaskStore(
            _shared_cache, ttl_seconds=session_ttl_seconds
        )
        self.metrics_store = metrics_store or MetricsStore(_shared_cache)
        self.benchmark_store = benchmark_store or BenchmarkStore(_shared_cache)
        # 审计哈希链：默认复用共享缓存，记录敏感写操作，可校验防篡改
        if audit_store is None:
            from athena.tools.audit_chain import HashChainAuditStore

            audit_store = HashChainAuditStore(_shared_cache)
        self.audit_store = audit_store
        # 运行态 Agent 按进程缓存，重启后依据持久化的消息历史惰性重建
        self._agents: dict[str, ReActAgent] = {}
        self.ops_knowledge = ops_knowledge or OpsKnowledgeBase()
        # 工作流 LLM：注入后 Planner/Executor 走真实 LLM，缺失则规则/占位降级
        self.workflow_llm = workflow_llm
        # 评测嵌入：注入后 Benchmark 走语义评分，缺失则关键词匹配
        self.embedding_provider = embedding_provider

    def create_session(self, title: str | None = None) -> SessionDetail:
        """
        创建一个独立 Web 会话。

        功能说明：新建 session_id、创建专属 ReActAgent，并把会话保存到内存表。
        参数说明：title 是前端侧边栏展示标题；不传时使用默认标题。
        返回值：SessionDetail，包含会话元信息和空消息列表。
        设计思路：创建会话时就创建 Agent，可以保证后续对话共享该会话的工作记忆。
        使用示例：session = service.create_session("排障演示")
        """
        self.cleanup_expired_sessions()  # 💡 学习提示：创建新会话前顺手清理旧会话，避免长期运行时内存慢慢涨。
        session_id = f"session-{uuid.uuid4().hex[:12]}"  # 💡 学习提示：uuid 比自增 id 更适合 Web 场景，避免用户猜到相邻会话 id。
        stored = self.session_store.create(
            session_id, title or "New Athena Session"
        )  # 先落库，保证重启/多副本可见
        session = self._runtime_session(stored)
        self._agents[session_id] = session.agent
        return self._session_detail(session)

    def list_sessions(self) -> list[SessionSummary]:
        """
        获取所有活跃会话摘要。

        功能说明：返回侧边栏需要的轻量会话信息，不返回完整消息内容。
        参数说明：无。
        返回值：SessionSummary 列表。
        设计思路：列表接口保持轻量，避免会话多时一次性传输大量历史消息。
        使用示例：sessions = service.list_sessions()
        """
        self.cleanup_expired_sessions()
        return [
            self._summary_from_stored(stored)
            for stored in self.session_store.list()
        ]

    def get_session(self, session_id: str) -> SessionDetail:
        """Return one active session by id.

        Args:
            session_id: Session identifier from ``POST /api/sessions``.

        Returns:
            Session detail with message history.

        Raises:
            ApiServiceError: If the session does not exist or expired.
        """
        self.cleanup_expired_sessions()
        return self._session_detail(self._require_session(session_id))

    async def chat(self, session_id: str, message: str) -> ChatResponse:
        """
        执行一次同步 Agent 对话。

        功能说明：把用户消息写入会话，调用 ReActAgent.run()，记录结果、指标和任务状态。
        参数说明：
            session_id：目标会话 id。
            message：用户输入的问题或任务。
        返回值：ChatResponse，包含 task_id、answer 和步骤轨迹。
        设计思路：同步接口适合简单调用；所有运行状态仍写入 TaskRecord，方便后续查看 trace。
        使用示例：response = await service.chat(session_id, "检查服务")

        🔍 原理讲解：
        一次同步对话会经历：保存用户消息 → 创建 task record → Agent 执行 → 保存助手消息 → 更新指标。
        举个例子：
        输入 "hello" → agent.run("hello") → 输出 answer + steps → 前端展示答案和右侧轨迹。
        """
        session = self._require_session(session_id)
        task_id = self._new_task_id("chat")
        started_at = (
            time.perf_counter()
        )  # 💡 学习提示：记录开始时间放在调用 Agent 前，才能覆盖完整执行耗时。
        session.messages.append(
            ChatMessage(role="user", content=message, created_at=time.time())
        )
        record = TaskRecord(task_id=task_id, status="running")
        self.task_store.save(record)
        try:
            response = await session.agent.run(message)
            steps = self._steps_from_strings(
                response.steps
            )  # 💡 学习提示：Agent 内部 steps 是字符串，Web 前端更适合消费结构化 StepTrace。
            duration = time.perf_counter() - started_at
            self.metrics.record_task(success=True, duration_seconds=duration)
            session.messages.append(
                ChatMessage(
                    role="assistant", content=response.answer, created_at=time.time()
                )
            )
            session.updated_at = time.time()
            self._persist_session(session)  # 会话历史落库，重启/多副本可延续
            record.status = "success"
            record.answer = response.answer
            record.steps = steps
            record.updated_at = time.time()
            self.task_store.save(record)  # 任务结果落库，重启/多副本可查
            return ChatResponse(
                task_id=task_id,
                session_id=session_id,
                answer=response.answer,
                steps=steps,
            )
        except Exception as exc:
            self._record_failure(
                record, exc, started_at
            )  # 💡 学习提示：失败也要记录指标，否则成功率会被虚高。
            raise ApiServiceError("AGENT_CHAT_FAILED", str(exc)) from exc

    async def stream_chat(self, session_id: str, message: str) -> AsyncIterator[str]:
        """
        执行一次 SSE 流式 Agent 对话。

        功能说明：消费 ReActAgent.stream_run() 产生的事件，并转换成浏览器能识别的 SSE 文本块。
        参数说明：
            session_id：目标会话 id。
            message：用户输入的问题或任务。
        返回值：异步生成器，不一次性返回完整结果，而是不断 yield SSE 字符串。
        设计思路：SSE 是轻量流式协议，浏览器可以边收到边渲染，适合展示 Agent 思考过程。
        使用示例：
            async for chunk in service.stream_chat(session_id, "检查日志"):
                print(chunk)

        🎯 面试考点：为什么不用前端 setTimeout 假装打字？答案：这里从 Agent 执行层真实产出事件，前端只是展示。
        """
        session = self._require_session(session_id)
        task_id = self._new_task_id("stream")
        started_at = time.perf_counter()
        session.messages.append(
            ChatMessage(role="user", content=message, created_at=time.time())
        )
        record = TaskRecord(task_id=task_id, status="running")
        self.task_store.save(record)
        yield self._sse(
            {
                "event_type": "task",
                "task_id": task_id,
                "content": "started",
                "step_index": 0,
            }
        )  # 💡 学习提示：先发 task 事件，让前端立刻拿到 task_id。
        try:
            async for event in session.agent.stream_run(message):
                step = StepTrace(
                    step_index=event.step_index,
                    event_type=event.event_type,
                    content=event.content,
                )
                record.steps.append(step)
                if event.event_type == "final":
                    record.answer = (
                        event.content
                    )  # 💡 学习提示：只有 final 事件才写入会话历史，避免把中间思考误当成最终回复。
                    session.messages.append(
                        ChatMessage(
                            role="assistant",
                            content=event.content,
                            created_at=time.time(),
                        )
                    )
                yield self._sse(
                    {
                        "event_type": event.event_type,
                        "task_id": task_id,
                        "content": event.content,
                        "step_index": event.step_index,
                    }
                )
            duration = time.perf_counter() - started_at
            self.metrics.record_task(success=True, duration_seconds=duration)
            record.status = "success"
            record.updated_at = time.time()
            session.updated_at = time.time()
            self._persist_session(session)  # 会话历史落库，重启/多副本可延续
            self.task_store.save(record)  # 任务结果落库，重启/多副本可查
            yield self._sse(
                {
                    "event_type": "done",
                    "task_id": task_id,
                    "content": record.answer or "",
                    "step_index": len(record.steps),
                }
            )  # 💡 学习提示：done 是“流结束信号”，方便前端收尾状态。
        except Exception as exc:
            self._record_failure(record, exc, started_at)
            yield self._sse(
                {
                    "event_type": "error",
                    "task_id": task_id,
                    "content": str(exc),
                    "step_index": len(record.steps),
                }
            )

    async def run_workflow(
        self, task: str, workflow_type: str = "plan_execute", actor: str = "system"
    ) -> WorkflowRunResponse:
        """
        执行多 Agent 工作流。

        功能说明：创建 Planner/Executor/Validator 三角色工作流并执行复杂任务。
        参数说明：
            task：用户输入的复杂任务，通常可以用分号拆成多步。
            workflow_type：工作流类型，目前只支持 plan_execute。
        返回值：WorkflowRunResponse，包含任务状态、最终答案和步骤轨迹。
        设计思路：保留 workflow_type 参数，是为了未来扩展其他编排模式时不改 API 形状。
        使用示例：await service.run_workflow("收集日志; 验证修复", "plan_execute")
        """
        if workflow_type != "plan_execute":
            raise ApiServiceError(
                "WORKFLOW_TYPE_UNSUPPORTED",
                f"Unsupported workflow_type: {workflow_type}",
            )
        task_id = self._new_task_id("workflow")
        started_at = time.perf_counter()
        record = TaskRecord(task_id=task_id, status="running")
        self.task_store.save(record)
        try:
            engine = WorkflowEngine(
                PlannerAgent(llm_client=self.workflow_llm),
                ExecutorAgent(llm_client=self.workflow_llm),
                ValidatorAgent(),
            )  # 💡 学习提示：这里组装三角色，体现“规划-执行-校验”的职责拆分。
            response = await engine.run(task)
            steps = self._steps_from_strings(response.steps)
            self.metrics.record_task(
                success=True, duration_seconds=time.perf_counter() - started_at
            )
            record.status = "success"
            record.answer = response.answer
            record.steps = steps
            record.updated_at = time.time()
            self.task_store.save(record)  # 任务结果落库，重启/多副本可查
            self._audit(actor, "workflow.run", task_id, True, workflow_type)
            return WorkflowRunResponse(
                task_id=task_id, status="success", answer=response.answer, steps=steps
            )
        except Exception as exc:
            self._record_failure(record, exc, started_at)
            self._audit(actor, "workflow.run", task_id, False, str(exc))
            raise ApiServiceError("WORKFLOW_FAILED", str(exc)) from exc

    def get_task_status(self, task_id: str) -> WorkflowStatusResponse:
        """
        查询任务状态。

        功能说明：返回某个任务当前记录的状态、答案、步骤和错误。
        参数说明：task_id 是 chat/stream/workflow 生成的任务 id。
        返回值：WorkflowStatusResponse。
        设计思路：用同一个状态接口查询多种任务，前端不需要关心任务来源。
        使用示例：status = service.get_task_status("workflow-xxx")
        """
        record = self._require_task(task_id)
        return WorkflowStatusResponse(
            task_id=record.task_id,
            status=record.status,
            answer=record.answer,
            steps=record.steps,
            error=record.error,
        )

    def get_traces(self, task_id: str) -> TraceResponse:
        """
        查询任务执行轨迹。

        功能说明：返回右侧“对话轨迹”面板需要展示的 StepTrace 列表。
        参数说明：task_id 是任务 id。
        返回值：TraceResponse。
        设计思路：trace 和 status 分成两个接口，方便前端按需刷新。
        使用示例：trace = service.get_traces(task_id)
        """
        record = self._require_task(task_id)
        return TraceResponse(task_id=task_id, events=record.steps)

    def get_metrics(self) -> MetricsResponse:
        """
        获取全局运行指标。

        功能说明：统计总任务数、成功率、平均耗时、Token 消耗和错误分布。
        参数说明：无。
        返回值：MetricsResponse。
        设计思路：指标对象只记录原始数据，API 层负责聚合成前端需要的形状。
        使用示例：metrics = service.get_metrics()
        """
        total = self.metrics.task_success + self.metrics.task_failure
        average_duration = (
            sum(self.metrics.task_durations) / len(self.metrics.task_durations)
            if self.metrics.task_durations
            else 0.0
        )  # 💡 学习提示：空列表时返回 0，避免冷启动页面除零报错。
        return MetricsResponse(
            total_tasks=total,
            success_rate=self.metrics.success_rate(),
            average_duration_seconds=average_duration,
            token_usage=self.metrics_store.token_usage(),
            error_distribution=self.metrics_store.error_distribution(),
        )

    def list_audit_events(
        self, limit: int = 50, tenant_id: str | None = None
    ) -> list[dict[str, object]]:
        """返回最近的审计记录（哈希链），可按租户过滤。"""
        from dataclasses import asdict as _asdict

        store = getattr(self, "audit_store", None)
        if store is None:
            return []
        return [_asdict(record) for record in store.list(limit, tenant_id)]

    def verify_audit_chain(self) -> dict[str, object]:
        """校验审计哈希链完整性，返回 valid/checked/broken_at。"""
        store = getattr(self, "audit_store", None)
        if store is None:
            return {"valid": True, "checked": 0, "broken_at": None, "head": 0}
        return store.verify_chain()

    def ingest_alert(self, payload: dict[str, object]) -> dict[str, object]:
        """接收 Alertmanager webhook，标准化并写入审计链。"""
        from athena.integration.alert_webhook import AlertWebhookParser

        alert = AlertWebhookParser().parse(payload)
        self._audit(
            "alertmanager",
            "alert.received",
            alert.alert_name,
            alert.severity != "critical",
            f"severity={alert.severity}",
        )
        return {
            "status": "accepted",
            "alert_name": alert.alert_name,
            "severity": alert.severity,
        }

    async def run_benchmark(
        self, case_set: str = "default", actor: str = "system"
    ) -> BenchmarkRunResponse:
        """
        运行一个轻量 Benchmark 并保存报告。

        功能说明：构造可复现测试用例，调用 BenchmarkEngine，生成 Markdown 报告。
        参数说明：case_set 是测试集名称，目前用于拼接 demo 用例名。
        返回值：BenchmarkRunResponse，包含 run_id、状态和 Markdown 报告。
        设计思路：这里使用确定性 runner，避免 Web 演示依赖真实 LLM 导致报告不稳定。
        使用示例：response = await service.run_benchmark("smoke")
        """
        run_id = self._new_task_id("benchmark")

        async def runner(query: str) -> AgentResponse:
            """Benchmark 专用假 Agent，保证评测演示稳定可复现。"""
            return AgentResponse(
                answer=f"diagnosis ok: {query}", steps=["plan", "execute", "validate"]
            )

        cases = (
            BenchmarkCase(
                name=f"{case_set}-basic",
                query="check service health",
                expected_keywords=("diagnosis",),
            ),
        )
        results = await BenchmarkEngine(
            runner, embedding_provider=self.embedding_provider
        ).run_cases(cases)
        report = BenchmarkReport.from_results(results).to_markdown()
        response = BenchmarkRunResponse(run_id=run_id, status="success", report=report)
        self.benchmark_store.save(response)
        self._audit(actor, "benchmark.run", run_id, True, case_set)
        return response

    def get_benchmark_report(self, run_id: str) -> BenchmarkReportResponse:
        """
        根据 run_id 查询 Benchmark 报告。

        功能说明：从内存报告表取出之前运行过的 Benchmark 结果。
        参数说明：run_id 是 run_benchmark 返回的 id。
        返回值：BenchmarkReportResponse。
        设计思路：报告生成和报告查询分离，贴近真实异步评测系统的接口形态。
        使用示例：report = service.get_benchmark_report(run_id)
        """
        report = self.benchmark_store.get(run_id)
        if report is None:
            raise ApiServiceError(
                "BENCHMARK_NOT_FOUND", f"Benchmark run not found: {run_id}"
            )
        return BenchmarkReportResponse(
            run_id=report.run_id, status=report.status, report=report.report
        )

    def list_cloud_ops_modes(self) -> list[CloudOpsMode]:
        """
        返回 Web 控制台支持的 CloudOps 子模式。

        功能说明：提供 K8s、资源巡检、故障排查、成本优化四个场景的标题和说明。
        参数说明：无。
        返回值：CloudOpsMode 列表。
        设计思路：模式元数据由后端统一维护，避免前端和后端能力清单漂移。
        使用示例：modes = service.list_cloud_ops_modes()
        """
        return [
            CloudOpsMode(
                mode="k8s",
                title="K8s 运维",
                description="Pod/节点/事件查询与常见故障 SOP 诊断",
            ),
            CloudOpsMode(
                mode="resource",
                title="资源巡检",
                description="云实例、安全组和监控指标核查",
            ),
            CloudOpsMode(
                mode="fault",
                title="故障排查",
                description="告警到根因分析、修复建议、沙箱验证和知识沉淀",
            ),
            CloudOpsMode(
                mode="cost",
                title="成本优化",
                description="闲置资源识别、低效配置分析和优化收益预估",
            ),
        ]

    async def run_cloud_ops(
        self,
        mode: str,
        task: str = "",
        provider: str = "aliyun",
        confirmed: bool = False,
        actor: str = "system",
    ) -> CloudOpsResponse:
        """
        运行一个 CloudOps 场景并记录轨迹。

        功能说明：创建任务记录，分发到具体子模式，更新指标，并返回统一响应。
        参数说明：mode 是 k8s/resource/fault/cost；task 是用户输入；provider 是云厂商；confirmed 是高危确认标记。
        返回值：CloudOpsResponse。
        设计思路：这是 CloudOps 的服务层总入口，API 路由和前端都不需要知道每个子模式内部怎么做。
        使用示例：await service.run_cloud_ops("cost", provider="aliyun")

        🔍 原理讲解：
        这个方法像一个“任务外壳”：先建 task_id 和 TaskRecord，再把实际工作交给 _dispatch_cloud_ops。
        子模式执行完后，再统一写入状态、轨迹和指标。
        """
        task_id = self._new_task_id(f"cloud-{mode}")
        started_at = time.perf_counter()
        record = TaskRecord(task_id=task_id, status="running")
        self.task_store.save(record)
        try:
            answer, steps, data, requires_confirmation = self._dispatch_cloud_ops(
                mode, task, provider, confirmed
            )
            self.metrics.record_task(
                success=not requires_confirmation,
                duration_seconds=time.perf_counter() - started_at,
            )  # 💡 学习提示：等待人工确认不算真正成功，否则成功率会被高估。
            record.status = (
                "waiting_confirmation" if requires_confirmation else "success"
            )  # 💡 学习提示：前端靠这个状态判断当前任务是完成还是等用户确认。
            record.answer = answer
            record.steps = steps
            record.updated_at = time.time()
            self.task_store.save(record)  # 任务结果落库，重启/多副本可查
            self._audit(
                actor,
                f"cloud_ops.{mode}",
                task_id,
                not requires_confirmation,
                f"provider={provider};confirmed={confirmed}",
            )
            return CloudOpsResponse(
                task_id=task_id,
                mode=mode,
                status=record.status,
                answer=answer,
                steps=steps,
                data=data,
                requires_confirmation=requires_confirmation,
            )
        except Exception as exc:
            self._record_failure(record, exc, started_at)
            self._audit(actor, f"cloud_ops.{mode}", task_id, False, str(exc))
            raise ApiServiceError("CLOUD_OPS_FAILED", str(exc)) from exc

    async def stream_cloud_ops(
        self,
        mode: str,
        task: str = "",
        provider: str = "aliyun",
        confirmed: bool = False,
    ) -> AsyncIterator[str]:
        """
        以 SSE 形式流式输出 CloudOps 场景。

        功能说明：先复用 run_cloud_ops 得到完整结果，再按 SSE 协议逐条发送 task/step/done。
        参数说明：mode/task/provider/confirmed 与 run_cloud_ops 相同。
        返回值：异步字符串迭代器，每个字符串是一条 SSE 消息。
        设计思路：CloudOps 第一版执行很快，先用“执行后分步推送”的方式复用结果模型；未来可改成边执行边 yield。
        使用示例：async for chunk in service.stream_cloud_ops("fault"): print(chunk)

        🎯 面试考点：这是真实时序流吗？答案：目前是兼容型流式，协议是真的 SSE；后续可把子步骤执行过程改成实时 yield。
        """
        response = await self.run_cloud_ops(mode, task, provider, confirmed)
        yield self._sse(
            {
                "event_type": "task",
                "task_id": response.task_id,
                "content": f"cloud ops {mode} started",
                "step_index": 0,
            }
        )
        for step in response.steps:
            yield self._sse(
                {
                    "event_type": step.event_type,
                    "task_id": response.task_id,
                    "content": step.content,
                    "step_index": step.step_index,
                }
            )
        yield self._sse(
            {
                "event_type": "done",
                "task_id": response.task_id,
                "content": response.answer,
                "step_index": len(response.steps),
            }
        )

    def search_ops_knowledge(self, query: str) -> dict[str, object]:
        """
        检索 CloudOps 运维知识。

        功能说明：查询故障排查工作流沉淀的知识项。
        参数说明：query 是检索关键词。
        返回值：包含 query 和 items 的字典。
        设计思路：服务层统一返回 JSON 友好的 dict，路由层不需要理解知识库内部对象。
        使用示例：service.search_ops_knowledge("CrashLoop")
        """
        items = self.ops_knowledge.search(query)
        return {"query": query, "items": [item.__dict__ for item in items]}

    def _dispatch_cloud_ops(
        self, mode: str, task: str, provider: str, confirmed: bool
    ) -> tuple[str, list[StepTrace], dict[str, object], bool]:
        """
        根据 mode 分发到具体 CloudOps 子场景。

        功能说明：把统一入口请求路由到 K8s、资源、故障或成本处理函数。
        参数说明：mode 是子模式；task 是任务文本；provider 是云厂商；confirmed 是确认标记。
        返回值：answer、steps、data、requires_confirmation 四元组。
        设计思路：这里是服务层内部的“策略选择器”，每个子模式仍保持独立私有方法。
        使用示例：self._dispatch_cloud_ops("k8s", "", "aliyun", False)
        """
        if mode == "k8s":
            return self._run_k8s_ops()
        if mode == "resource":
            return self._run_resource_ops(task, provider, confirmed)
        if mode == "fault":
            return self._run_fault_ops(task)
        if mode == "cost":
            return self._run_cost_ops(provider)
        raise ApiServiceError(
            "CLOUD_MODE_UNSUPPORTED", f"Unsupported cloud ops mode: {mode}"
        )

    def _run_k8s_ops(self) -> tuple[str, list[StepTrace], dict[str, object], bool]:
        """
        执行 K8s 运维巡检场景。

        功能说明：收集集群快照，运行 K8s 诊断器，并生成前端轨迹步骤。
        参数说明：无，当前使用 Mock K8sOpsTools。
        返回值：answer、steps、data、requires_confirmation=False。
        设计思路：K8s 巡检是只读场景，不需要人工确认，但要把诊断结果结构化返回。
        使用示例：answer, steps, data, confirm = self._run_k8s_ops()
        """
        tools = K8sOpsTools()
        diagnoser = K8sDiagnoser(tools.client)
        snapshot = tools.cluster_snapshot()
        diagnoses = [
            diagnosis.__dict__ for diagnosis in diagnoser.diagnose_pods()
        ]  # 💡 学习提示：把 dataclass 转 dict，FastAPI/前端才能直接 JSON 化。
        steps = [
            StepTrace(
                step_index=1,
                event_type="collect",
                content="Collected pod, node, event, and usage snapshot",
            ),
            StepTrace(
                step_index=2,
                event_type="diagnose",
                content=f"Detected {len(diagnoses)} Kubernetes findings",
            ),
            StepTrace(
                step_index=3,
                event_type="recommend",
                content="Prioritize CrashLoopBackOff logs and ImagePullBackOff registry checks",
            ),
        ]
        answer = f"K8s 巡检完成：发现 {len(diagnoses)} 个需要关注的问题。"
        return answer, steps, {"snapshot": snapshot, "diagnoses": diagnoses}, False

    def _run_resource_ops(
        self, task: str, provider: str, confirmed: bool
    ) -> tuple[str, list[StepTrace], dict[str, object], bool]:
        """
        执行云资源巡检或高危资源操作。

        功能说明：普通任务做实例/安全组/监控检查；包含 restart/重启 时触发高危重启流程。
        参数说明：task 是用户输入；provider 是云厂商；confirmed 是人工确认状态。
        返回值：answer、steps、data、requires_confirmation。
        设计思路：用任务关键词触发高危操作是 MVP 简化，真实系统应由意图识别或结构化命令触发。
        使用示例：self._run_resource_ops("restart instance", "aliyun", False)
        """
        client = self._cloud_client(provider)
        if "restart" in task.lower() or "重启" in task:
            # 💡 学习提示：这里故意把重启走工具层 confirmed 检查，前端按钮只是交互，真正安全边界在后端工具。
            result = client.restart_instance("i-prod-api-01", confirmed=confirmed)
            steps = self._steps_from_cloud_result(result)
            return (
                result.message,
                steps,
                {"operation": result.__dict__},
                result.requires_confirmation,
            )
        instances = client.list_instances()
        security = client.check_security_groups()
        metrics = client.fetch_monitoring_metrics()
        steps = [
            StepTrace(
                step_index=1, event_type="inspect", content="Listed cloud instances"
            ),
            StepTrace(
                step_index=2,
                event_type="security",
                content="Checked security group exposure",
            ),
            StepTrace(
                step_index=3, event_type="metrics", content="Fetched monitoring metrics"
            ),
        ]
        answer = "资源巡检完成：发现 1 条高风险 SSH 暴露规则和 1 台低利用率实例。"
        return (
            answer,
            steps,
            {
                "instances": instances.data,
                "security": security.data,
                "metrics": metrics.data,
            },
            False,
        )

    def _run_fault_ops(
        self, task: str
    ) -> tuple[str, list[StepTrace], dict[str, object], bool]:
        """
        执行故障自动排查场景。

        功能说明：启动 FaultDiagnoseWorkflow，并把工作流步骤转换成 Web StepTrace。
        参数说明：task 通常是告警名，空时使用 KubePodCrashLooping。
        返回值：answer、steps、data、requires_confirmation=False。
        设计思路：故障排查工作流负责业务闭环，服务层只负责适配 API 响应格式。
        使用示例：self._run_fault_ops("KubePodCrashLooping")
        """
        workflow = FaultDiagnoseWorkflow(knowledge=self.ops_knowledge)
        result = workflow.run(task or "KubePodCrashLooping")
        steps = [
            StepTrace(step_index=index, event_type=step.name, content=step.detail)
            for index, step in enumerate(result.steps, start=1)
        ]
        return (
            result.summary,
            steps,
            {
                "run_id": result.run_id,
                "knowledge_id": result.knowledge_id,
                "steps": [step.__dict__ for step in result.steps],
            },
            False,
        )

    def _run_cost_ops(
        self, provider: str
    ) -> tuple[str, list[StepTrace], dict[str, object], bool]:
        """
        执行云成本优化分析。

        功能说明：扫描实例利用率，识别 CPU 小于 5% 的闲置资源，并估算月度节省金额。
        参数说明：provider 是云厂商标识。
        返回值：answer、steps、data、requires_confirmation=False。
        设计思路：成本优化先用简单阈值法，便于解释；未来可接账单 API 和更复杂规则。
        使用示例：self._run_cost_ops("aliyun")
        """
        client = self._cloud_client(provider)
        instances = client.list_instances().data["instances"]
        idle = [
            instance for instance in instances if float(instance["cpu"]) < 5.0
        ]  # 💡 学习提示：5% 是演示阈值，真实成本优化应结合业务低峰和实例规格。
        monthly_saving = (
            len(idle) * 320
        )  # 💡 学习提示：固定 320 元/月是估算常量，方便 demo 有清晰数字。
        steps = [
            StepTrace(
                step_index=1,
                event_type="collect",
                content="Collected instance utilization",
            ),
            StepTrace(
                step_index=2,
                event_type="analyze",
                content=f"Detected {len(idle)} idle resources",
            ),
            StepTrace(
                step_index=3,
                event_type="report",
                content=f"Estimated monthly saving: {monthly_saving} CNY",
            ),
        ]
        answer = f"成本优化完成：发现 {len(idle)} 台闲置实例，预计每月节省 {monthly_saving} 元。"
        return (
            answer,
            steps,
            {"idle_resources": idle, "estimated_monthly_saving_cny": monthly_saving},
            False,
        )

    def _cloud_client(self, provider: str) -> AliyunClient:
        """
        根据 provider 创建云厂商客户端。

        功能说明：支持 aliyun/tencent/aws 三种演示客户端。
        参数说明：provider 是云厂商字符串。
        返回值：云客户端实例，类型兼容 AliyunClient 演示接口。
        设计思路：用统一客户端接口屏蔽云厂商差异，上层 resource/cost 逻辑无需关心具体厂商。
        使用示例：client = self._cloud_client("tencent")
        """
        if provider == "tencent":
            return TencentCloudClient()
        if provider == "aws":
            return AWSClient()
        return AliyunClient()

    def _steps_from_cloud_result(self, result: CloudOperationResult) -> list[StepTrace]:
        """
        把云操作结果转换成 Web 轨迹步骤。

        功能说明：根据 CloudOperationResult 生成一条 StepTrace。
        参数说明：result 是工具层统一返回对象。
        返回值：StepTrace 列表。
        设计思路：工具层结果和 Web 轨迹模型解耦，中间由服务层做格式适配。
        使用示例：steps = self._steps_from_cloud_result(result)
        """
        status = (
            "waiting_confirmation"
            if result.requires_confirmation
            else ("success" if result.success else "failed")
        )
        return [
            StepTrace(
                step_index=1,
                event_type="cloud_operation",
                content=f"{result.operation}: {result.message}",
                status=status,
                duration_ms=result.duration_ms,
            )
        ]

    def cleanup_expired_sessions(self) -> None:
        """
        清理过期会话。

        功能说明：删除长时间未更新的 session，避免内存无限增长。
        参数说明：无。
        返回值：None。
        设计思路：MVP 用被动清理，创建/列出会话时顺便执行，不需要额外后台线程。
        使用示例：service.cleanup_expired_sessions()
        """
        # 持久化层依赖 TTL 自动过期；这里只需清理进程内失效的运行态 Agent 缓存。
        alive_ids = {stored.session_id for stored in self.session_store.list()}
        stale = [sid for sid in self._agents if sid not in alive_ids]
        for session_id in stale:  # 💡 学习提示：先收集 id 再删除，避免遍历 dict 时修改 dict。
            del self._agents[session_id]

    def _runtime_session(self, stored: "StoredSession") -> WebSession:
        """由持久化记录构建运行态 WebSession（含惰性重建的 Agent）。"""
        agent = self._agents.get(stored.session_id)
        if agent is None:
            agent = self.agent_factory()  # 每个会话独立 Agent，隔离工作记忆
        return WebSession(
            session_id=stored.session_id,
            title=stored.title,
            agent=agent,
            created_at=stored.created_at,
            updated_at=stored.updated_at,
            messages=[
                ChatMessage(role=m.role, content=m.content, created_at=m.created_at)
                for m in stored.messages
            ],
        )

    def _persist_session(self, session: WebSession) -> None:
        """把运行态会话的最新状态写回持久化存储。"""
        from athena.api.session_store import StoredMessage, StoredSession

        self.session_store.save(
            StoredSession(
                session_id=session.session_id,
                title=session.title,
                created_at=session.created_at,
                updated_at=time.time(),
                messages=[
                    StoredMessage(
                        role=m.role, content=m.content, created_at=m.created_at
                    )
                    for m in session.messages
                ],
            )
        )

    def _summary_from_stored(self, stored: "StoredSession") -> SessionSummary:
        """由持久化记录直接构建列表摘要，无需重建 Agent。"""
        return SessionSummary(
            session_id=stored.session_id,
            title=stored.title,
            created_at=stored.created_at,
            updated_at=stored.updated_at,
            message_count=len(stored.messages),
        )

    def _require_session(self, session_id: str) -> WebSession:
        """读取会话：优先进程缓存，否则从持久化存储惰性重建。"""
        stored = self.session_store.get(session_id)
        if stored is None:
            raise ApiServiceError(
                "SESSION_NOT_FOUND", f"Session not found: {session_id}"
            )
        session = self._runtime_session(stored)
        self._agents[session_id] = session.agent
        return session

    def _require_task(self, task_id: str) -> TaskRecord:
        """读取任务记录，不存在时抛出标准 API 错误。"""
        record = self.task_store.get(task_id)
        if record is None:
            raise ApiServiceError("TASK_NOT_FOUND", f"Task not found: {task_id}")
        return record

    def _session_summary(self, session: WebSession) -> SessionSummary:
        """把内部 WebSession 转成列表页使用的 SessionSummary。"""
        return SessionSummary(
            session_id=session.session_id,
            title=session.title,
            created_at=session.created_at,
            updated_at=session.updated_at,
            message_count=len(session.messages),
        )

    def _session_detail(self, session: WebSession) -> SessionDetail:
        """把内部 WebSession 转成详情页使用的 SessionDetail。"""
        summary = self._session_summary(session)
        return SessionDetail(
            **summary.model_dump(), messages=list(session.messages)
        )  # 💡 学习提示：list(...) 复制消息列表，避免外部误改内部状态。

    def _steps_from_strings(self, steps: list[str]) -> list[StepTrace]:
        """把 AgentResponse.steps 的字符串列表转换成结构化轨迹。"""
        return [
            StepTrace(step_index=index, event_type="step", content=step)
            for index, step in enumerate(steps, start=1)
        ]

    def _record_failure(
        self, record: TaskRecord, exc: Exception, started_at: float
    ) -> None:
        """统一记录失败任务的状态、错误分布和耗时指标。"""
        error_name = exc.__class__.__name__
        self.metrics_store.incr_error(error_name)
        self.metrics.record_task(
            success=False, duration_seconds=time.perf_counter() - started_at
        )
        record.status = "failed"
        record.error = str(exc)
        record.updated_at = time.time()
        self.task_store.save(record)  # 失败状态也落库，保证可查

    def _new_task_id(self, prefix: str) -> str:
        """生成带业务前缀的任务 id，方便日志中一眼看出任务类型。"""
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    def _audit(
        self, actor: str, action: str, resource: str, success: bool, detail: str = ""
    ) -> None:
        """向审计哈希链追加一条记录；审计失败不影响主流程。"""
        store = getattr(self, "audit_store", None)
        if store is None:
            return
        try:
            store.append(
                actor=actor,
                tenant_id=actor,
                action=action,
                resource=resource,
                success=success,
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - 审计是旁路，不能拖垮业务请求
            pass

    def _sse(self, payload: dict[str, object]) -> str:
        """
        把字典包装成 SSE 文本块。

        🔍 原理讲解：
        SSE 协议要求每条消息以 `data:` 开头，并用一个空行表示这条事件结束。
        举个例子：
        {"event_type": "final"} → data: {"event_type":"final"}\n\n → 浏览器流读取器解析为一条事件。
        """
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def static_directory() -> Path:
    """
    返回内置 Web Console 静态资源目录。

    功能说明：定位 athena/web/static，供 FastAPI 挂载静态文件。
    参数说明：无。
    返回值：Path 对象。
    设计思路：用 __file__ 相对路径，避免依赖当前工作目录。
    使用示例：directory = static_directory()
    """
    return Path(__file__).parent.parent / "web" / "static"


"""
🤔 思考题：

1. 如果会话要跨进程保存，你会把 sessions/tasks 从 dict 换成什么存储？
2. 当前 stream_chat 的错误是作为 SSE error 事件返回；同步 chat 则抛 ApiServiceError。这两种方式为什么不同？
3. 如果 Benchmark 要评测真实 Agent，而不是确定性 runner，需要在哪里替换？
4. 如果同时有很多用户请求，内存字典和单进程指标会有什么局限？
5. ⚡ 优化建议：未来可以把 TaskRecord 持久化，并为 stream_chat 增加首事件延迟指标。
"""
