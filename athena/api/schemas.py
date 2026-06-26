"""
📦 模块名称：Athena Web API 请求/响应模型
📍 架构位置：接口契约层，位于前端 JSON 请求和后端服务逻辑之间。
🎯 核心作用：用 Pydantic 定义所有 API 的输入输出格式，保证前后端数据结构稳定。
🔗 依赖关系：依赖 pydantic.BaseModel；被 routes 和 AthenaWebService 共同依赖。
💡 设计思路：使用“数据契约”模式。路由收到 JSON 后先转成模型，服务层返回模型，避免到处传散乱 dict。
📚 学习重点：重点看 Field 如何做最小校验，以及 Response 模型如何对应前端页面需要的数据。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """
    标准错误响应。

    功能说明：所有 API 错误都返回同样的 error_code 和 message。
    参数说明：error_code 是稳定错误码；message 是用户可读说明。
    返回值：Pydantic 模型，可被 FastAPI 自动转成 JSON。
    设计思路：统一错误结构后，前端不用分别解析很多种异常格式。
    使用示例：ErrorResponse(error_code="SESSION_NOT_FOUND", message="Session not found")
    """

    error_code: str
    message: str


class SessionCreateRequest(BaseModel):
    """
    创建会话请求。

    功能说明：描述 POST /api/sessions 接收的数据。
    参数说明：title 是可选会话标题，最多 80 个字符。
    返回值：请求模型本身不返回，由路由传给服务层。
    设计思路：标题可选，前端不传也能创建会话，降低第一次使用的门槛。
    使用示例：SessionCreateRequest(title="排障演示")
    """

    title: str | None = Field(
        default=None, max_length=80
    )  # 💡 学习提示：max_length 在边界层限制输入，避免超长标题影响侧边栏布局。


class ChatMessage(BaseModel):
    """
    会话里的一条聊天消息。

    功能说明：保存用户或助手消息，供前端重新渲染历史对话。
    参数说明：role 是 user/assistant；content 是消息内容；created_at 是时间戳。
    返回值：Pydantic 数据对象。
    设计思路：消息历史只保存最小字段，避免把 Agent 内部状态直接暴露给前端。
    使用示例：ChatMessage(role="user", content="hello", created_at=123.0)
    """

    role: str
    content: str
    created_at: float


class SessionSummary(BaseModel):
    """
    会话列表项。

    功能说明：给左侧会话列表使用的轻量摘要。
    参数说明：session_id 是唯一 id；message_count 用于展示消息数量。
    返回值：会话摘要模型。
    设计思路：列表页不返回 messages，减少无意义的数据传输。
    使用示例：SessionSummary(...)
    """

    session_id: str
    title: str
    created_at: float
    updated_at: float
    message_count: int


class SessionDetail(SessionSummary):
    """
    会话详情。

    功能说明：在 SessionSummary 基础上增加完整消息历史。
    参数说明：messages 是该会话的聊天记录。
    返回值：详情模型。
    设计思路：继承 SessionSummary 可以避免重复声明 session_id/title 等字段。
    使用示例：SessionDetail(**summary.model_dump(), messages=[])

    🎯 面试考点：为什么这里用继承？答案：详情响应确实是摘要响应的扩展，继承能表达这种“is-a + 增量字段”关系。
    """

    messages: list[ChatMessage]


class SessionCreateResponse(BaseModel):
    """
    创建会话响应。

    功能说明：POST /api/sessions 成功后返回新会话详情。
    参数说明：session 是刚创建的 SessionDetail。
    返回值：响应模型。
    设计思路：用包裹对象 `{session: ...}`，未来如果要加 welcome_message 等字段更容易兼容。
    使用示例：SessionCreateResponse(session=session_detail)
    """

    session: SessionDetail


class ChatRequest(BaseModel):
    """
    对话请求。

    功能说明：同步对话和流式对话共用同一份请求体。
    参数说明：session_id 指定会话；message 是用户输入。
    返回值：请求模型。
    设计思路：两个接口只差返回方式，不差输入结构，所以复用同一个模型。
    使用示例：ChatRequest(session_id="session-xxx", message="检查日志")
    """

    session_id: str = Field(
        min_length=1
    )  # 💡 学习提示：min_length=1 能在进入业务逻辑前挡住空 id。
    message: str = Field(min_length=1)


class StepTrace(BaseModel):
    """
    执行轨迹中的一步。

    功能说明：描述右侧轨迹面板展示的一条事件或步骤。
    参数说明：step_index 是顺序号；event_type 是事件类型；content 是展示内容；status/duration_ms 为后续扩展预留。
    返回值：轨迹步骤模型。
    设计思路：即使当前很多 duration_ms 是 0，也先保留字段，前端展示结构会更稳定。
    使用示例：StepTrace(step_index=1, event_type="step", content="plan")
    """

    step_index: int
    event_type: str
    content: str
    status: str = "success"
    duration_ms: float = 0.0


class ChatResponse(BaseModel):
    """
    同步对话响应。

    功能说明：返回一次 Agent 对话的完整结果。
    参数说明：task_id 用于后续查 trace；session_id 指向会话；answer 是最终答案；steps 是执行步骤。
    返回值：响应模型。
    设计思路：回答和轨迹一起返回，适合普通 HTTP 请求一次性展示。
    使用示例：ChatResponse(task_id="chat-1", session_id="s1", answer="ok", steps=[])
    """

    task_id: str
    session_id: str
    answer: str
    steps: list[StepTrace]


class WorkflowRunRequest(BaseModel):
    """
    多 Agent 工作流运行请求。

    功能说明：描述启动 workflow 所需参数。
    参数说明：session_id 当前可选预留；task 是任务文本；workflow_type 是工作流类型。
    返回值：请求模型。
    设计思路：保留 session_id 是为了未来让工作流也能写入某个会话历史。
    使用示例：WorkflowRunRequest(task="收集日志; 验证")
    """

    session_id: str | None = None
    task: str = Field(min_length=1)
    workflow_type: str = "plan_execute"


class WorkflowRunResponse(BaseModel):
    """
    工作流完成响应。

    功能说明：返回多 Agent 工作流执行后的最终结果。
    参数说明：task_id 是任务 id；status 是状态；answer 是汇总答案；steps 是每步轨迹。
    返回值：响应模型。
    设计思路：和 ChatResponse 类似，但额外带 status，适合以后扩展异步运行状态。
    使用示例：WorkflowRunResponse(task_id="workflow-1", status="success", answer="ok", steps=[])
    """

    task_id: str
    status: str
    answer: str
    steps: list[StepTrace]


class WorkflowStatusResponse(BaseModel):
    """
    任务状态查询响应。

    功能说明：查询 workflow/chat/stream 等任务当前状态。
    参数说明：error 只在失败时存在；steps 默认空列表。
    返回值：响应模型。
    设计思路：默认 Field(default_factory=list) 避免多个响应对象共享同一个列表。
    使用示例：WorkflowStatusResponse(task_id="t1", status="running")
    """

    task_id: str
    status: str
    answer: str | None = None
    steps: list[StepTrace] = Field(
        default_factory=list
    )  # 💡 学习提示：可变默认值不要写成 []，否则多个实例可能共享同一列表。
    error: str | None = None


class TraceResponse(BaseModel):
    """
    轨迹查询响应。

    功能说明：返回一个任务的完整执行事件列表。
    参数说明：task_id 是任务 id；events 是轨迹事件。
    返回值：响应模型。
    设计思路：单独定义 TraceResponse，让前端右侧面板可以独立刷新。
    使用示例：TraceResponse(task_id="t1", events=[])
    """

    task_id: str
    events: list[StepTrace]


class MetricsResponse(BaseModel):
    """
    全局运行指标响应。

    功能说明：给性能指标 Tab 展示统计数据。
    参数说明：total_tasks 是任务总数；success_rate 是 0..1 小数；error_distribution 是错误类型计数。
    返回值：响应模型。
    设计思路：后端返回原始数值，百分比格式化交给前端，便于图表组件复用。
    使用示例：MetricsResponse(total_tasks=1, success_rate=1.0, ...)
    """

    total_tasks: int
    success_rate: float
    average_duration_seconds: float
    token_usage: int
    error_distribution: dict[str, int]


class BenchmarkRunRequest(BaseModel):
    """
    Benchmark 启动请求。

    功能说明：指定要运行的评测集名称。
    参数说明：case_set 是评测集标识，默认 default。
    返回值：请求模型。
    设计思路：先保留 case_set，未来可映射到不同测试用例目录。
    使用示例：BenchmarkRunRequest(case_set="smoke")
    """

    case_set: str = "default"


class BenchmarkRunResponse(BaseModel):
    """
    Benchmark 运行响应。

    功能说明：返回一次评测运行的 id、状态和报告。
    参数说明：run_id 是评测运行 id；report 是 Markdown 文本。
    返回值：响应模型。
    设计思路：MVP 直接返回报告，未来也可以先返回 running，再异步查询报告。
    使用示例：BenchmarkRunResponse(run_id="benchmark-1", status="success", report="# Report")
    """

    run_id: str
    status: str
    report: str


class BenchmarkReportResponse(BaseModel):
    """
    Benchmark 报告查询响应。

    功能说明：根据 run_id 返回已保存的评测报告。
    参数说明：run_id 是评测运行 id；status 是运行状态；report 是 Markdown 文本。
    返回值：响应模型。
    设计思路：单独的查询响应让“运行”和“查看报告”两个动作可以解耦。
    使用示例：BenchmarkReportResponse(run_id="benchmark-1", status="success", report="# Report")
    """

    run_id: str
    status: str
    report: str


class CloudOpsMode(BaseModel):
    """
    CloudOps Web 子模式元数据。

    功能说明：描述前端“云运维模式”里可选择的一个子能力。
    参数说明：mode 是机器用标识；title 是页面显示标题；description 是能力说明。
    返回值：Pydantic 模型，FastAPI 会自动转成 JSON。
    设计思路：把模式清单做成 API 契约，前端不用硬猜后端支持哪些 CloudOps 能力。
    使用示例：CloudOpsMode(mode="k8s", title="K8s 运维", description="Pod 诊断")
    """

    mode: str
    title: str
    description: str


class CloudOpsRequest(BaseModel):
    """
    CloudOps 场景运行请求。

    功能说明：描述前端触发一次云运维任务所需的参数。
    参数说明：mode 是子模式；task 是用户任务；provider 是云厂商；confirmed 表示是否已人工确认高危操作。
    返回值：请求模型本身不返回，由路由传给服务层。
    设计思路：四个子模式共用同一个请求模型，MVP 阶段接口更简单；差异参数以后可逐步拆分。
    使用示例：CloudOpsRequest(mode="resource", task="restart instance", confirmed=True)

    🎯 面试考点：为什么 confirmed 放在请求体里？答案：它是一次操作的安全状态，必须随请求传到后端，不能只存在前端按钮状态里。
    """

    mode: str = Field(min_length=1)
    task: str = ""
    provider: str = "aliyun"
    confirmed: bool = False


class CloudOpsResponse(BaseModel):
    """
    CloudOps 场景运行响应。

    功能说明：返回任务 id、状态、答案、轨迹、结构化数据和是否需要确认。
    参数说明：steps 给右侧轨迹面板用；data 放场景专属结构化结果；requires_confirmation 驱动前端确认按钮。
    返回值：响应模型，FastAPI 自动序列化为 JSON。
    设计思路：把人读的 answer 和机器读的 data 分开，既适合页面展示，也适合后续自动化处理。
    使用示例：response.requires_confirmation 判断是否弹出“确认执行”。

    💡 学习提示：data 使用 dict[str, object]，是为了避免递归 JSONValue 在 Pydantic v2 里生成 schema 时递归过深。
    """

    task_id: str
    mode: str
    status: str
    answer: str
    steps: list[StepTrace]
    data: dict[str, object] = Field(default_factory=dict)
    requires_confirmation: bool = False


"""
🤔 思考题：

1. 如果前端需要分页展示会话，SessionSummary 应该增加哪些字段？
2. 为什么请求模型和响应模型不直接用 dict？
3. 如果要支持 WebSocket，ChatRequest 还能复用吗？
4. ⚡ 优化建议：StepTrace 未来可以增加 tool_name、arguments、result 字段，让工具调用展示更完整。
"""
