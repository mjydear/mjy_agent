# Athena 企业级智能云运维 Agent 代码实施计划

> 状态：Execution Plan v1.0  
> 日期：2026-07-11  
> 架构依据：[企业级架构 Proposal](enterprise_cloudops_agent_architecture.md)、附录 A/B/C 及当前代码库  
> 目标：将已批准的架构转换为可拆分 PR、可验证、可灰度、可回滚的实现清单  
> 约束：模块化单体、渐进迁移、安全优先、默认拒绝；不新增独立微服务、Kafka、Temporal、通用 Workflow DSL 或插件市场

秋招后端能力交付与面试证据的映射见
[`docs/interview/backend-capability-roadmap.md`](../interview/backend-capability-roadmap.md)。
该文档是补充路线图，不改变本文件作为源码实施索引的地位，也不表示其中的
PostgreSQL、Worker、分片或 Trace 能力已经实现。

## 1. 文档用途与边界

本文件回答“实现时先改什么、改到哪里、如何验证”，不重复解释 Proposal 中已经确定的架构原理。它是工程实施的唯一执行索引，所有新 PR 必须能对应到本文件中的任务 ID、验收条件和回滚策略。

本文件不做以下事情：

- 不覆盖或修改 `enterprise_cloudops_agent_architecture.md` 中的产品与架构决策。
- 不把历史 Demo 验收清单的完成标记视为生产成熟度；生产成熟度以当前代码、测试、迁移和发布门禁为准。
- 不预先写入全部尚未落地的源码。代码应以小 PR 落地，本文提供稳定的类型、接口、数据和行为契约。
- 不把 Mock、规则演示、进程内缓存或 `confirmed=true` 包装成生产级自治能力。

### 1.1 实施默认值

除非单独变更本文件，实施使用以下默认值：

| 决策 | 默认值 | 理由 |
|---|---|---|
| Python 版本 | 3.12 | 与 `pyproject.toml`、Dockerfile 保持一致；同步修正 README 与 package metadata |
| 发布形态 | 客户 Kubernetes 自托管优先；Compose 用于开发/试用 | 满足私网资源访问与最小权限需求 |
| 运行形态 | API 与 Worker 同镜像、不同命令 | 保持模块化单体，避免提前拆服务 |
| 前端 | 原生 ES Modules + 本地 CSS | 当前原生 Web 基础可渐进拆分，无需迁移 React/Vue |
| 首发自治 | S0/S1 自动只读；S3 代码完成后默认关闭 | 写操作必须等待持久化、审计和审批门禁 |
| 身份 | API Key/JWT 用于交互用户；Webhook 使用独立 Integration Identity | 不能让 Alertmanager 复用用户权限 |
| LLM 数据 | 默认只发送脱敏摘要，不发送 Secret 或原始大日志 | Evidence 原文留在受控存储 |
| LIVE 失败 | Fail-Closed | 不允许静默切换 Mock 或 Replay |

以下项不是可通过配置放宽的选择：S5 永久拒绝、写操作必须匹配 Plan Hash 与 Approval、Tenant/Scope 不可跨越、完整隐藏 Thought 不持久化、Skill 不自动扩大权限或自动发布代码。

## 2. 当前仓库基线

当前仓库拥有大量可复用组件，但整体仍是“具备生产方向资产的 Demo”，不能把类名、阶段名称或现有 Web 页面当作目标架构已完成的证据。

| 领域 | 可复用资产 | 实施前必须补齐的缺口 |
|---|---|---|
| Agent | `ReActAgent`、`LLMClient`、`ToolRegistry`、WorkingMemory | scratchpad/工具全量注入；直接调 ToolRegistry；保存 Thought；流式是完成后回放 |
| K8s/指标 | `tools/cloud/k8s/*`、`K8sReadOnlyDiagnoser`、Prometheus Client、结构化报告 | real 失败会回 Mock；Namespace 越权会被静默改写；没有统一 Evidence Entity |
| Workflow | 旧 `WorkflowEngine`、`FaultDiagnoseWorkflow` | 同步进程内运行；无持久状态、预算、等待审批、恢复与租约；存在 Mock 结果和直接知识写入 |
| Tool 治理 | `ToolExecutor`、PermissionManager、AuditLogger、K8s 写动作 SDK 封装 | 主 Agent 绕过治理管线；无 Capability、S0-S5、call_id、Evidence Ref 与统一输出 |
| API | FastAPI、路由、`AthenaWebService`、API Key/JWT、Scope、Idempotency、Trace ID | `AthenaWebService` 职责过大；Tenant 覆盖不完整；旧任务模型不一致 |
| 存储 | CacheBackend、Redis/内存适配、Session/Task/Benchmark Store | 无 PostgreSQL ORM、Migration、Repository、Worker Lease；Cache Key 多处缺 Tenant |
| 记忆/Skill | Knowledge、Profile、SkillLibrary、GEPA 骨架 | Skill 是进程内字典，无版本/审核/回放；生成与验证不能支持生产激活 |
| Alert | Parser、Webhook、只读诊断入口、History | 只读 `alerts[0]`、无机器身份、同步阻塞、进程内历史、无持久去重和生命周期 |
| Benchmark | Benchmark Engine、Report、kind 故障 Manifest | Web 路径使用假 runner；无 LIVE Case Loader、Oracle、配对 A/B Artifact |
| 前端 | 原生 HTML/JS/CSS、聊天与 CloudOps 视图 | 单页状态过重，围绕 Session 而非 OpsTask；Tailwind CDN 与内联图标不适合自托管 CSP |
| 部署/观测 | Dockerfile、Compose、K8s 清单、Prometheus/OTel/Alertmanager | 无 PG、Worker、Migration、Helm、备份；Ready 不能表达实际降级状态 |

### 2.1 迁移中的硬约束

1. 生产新链只使用 `athena/tools/cloud/k8s/*`；`athena/tools/builtin/k8s/*` 只保留给 legacy/Demo，不进入新 ToolRuntime。
2. 新 OpsTask 事实入口为 `/api/ops/tasks`。`/api/tasks`、`/api/workflow`、`/api/cloud-ops` 只做兼容门面，不能被重命名后伪装成新模型。
3. Phase 4 前的 Cache 或 `AsyncTaskManager` 只能作为过渡实现；不得承诺崩溃恢复、Durable Alert `202` 或多副本正确性。
4. 写操作绝不回退 legacy `confirmed=true` 链路。兼容参数只能产生弃用提示或创建 Plan，不能授权执行。
5. 旧 Redis TTL 数据不迁移为 PostgreSQL 生产事实。Phase 4 使用 fresh-install 基线；必要时只提供用户可控的知识/配置导入。
6. Docker 在 Phase 4 前固定单 API worker，避免进程内 Agent/Task 状态在多个 Uvicorn worker 之间分叉。

## 3. 目标代码边界与依赖规则

新增文件必须落在现有 `athena/` 单体包内；目录新增表示职责边界，不表示新服务或新部署单元。

```text
athena/
  bootstrap/                 # 组合根；CLI/API/Worker 共用
  application/               # OpsTask、Environment、Approval 等用例服务
  agent/
    policy/                  # PatternPolicy、PolicyAgent、契约
    context/                 # ContextManager 与确定性 Reducer
    workflow/                # Runner、状态、类型化 Workflow
  api/
    repositories/            # PostgreSQL Repository 与事务边界
    routes/                  # FastAPI Adapter
  memory/
    evidence.py              # Evidence 契约与 Store Adapter
  skills/                    # Skill Repository 与生命周期服务
  learning/                  # Curator、候选挖掘、离线回放
  tools/
    runtime.py               # ToolRuntime 治理管线
    contracts.py             # Tool V2
    bundles/                 # 静态 CapabilityBundle
    providers/               # Provider Adapter
```

依赖方向：

```text
Route/CLI/Webhook
 -> Application Service
 -> WorkflowRunner
 -> ContextManager / PolicyAgent / ToolRuntime
 -> Provider Adapter / Repository
 -> PostgreSQL / Redis / Secret Store / Evidence Content Store
```

- Route 不调用云 SDK、Repository 或 Agent Loop。
- Workflow 决定状态转换、预算、审批等待和结束条件；PolicyAgent 只能输出一个结构化 Action。
- ToolRuntime 是唯一可执行 Tool 的入口；PolicyAgent 和 Skill 不直接调用 ToolRegistry、Shell 或云 SDK。
- Repository 接口和 Provider Adapter 是真实变化点，可以使用 Protocol；普通内部类不机械抽象。
- 所有 Repository 方法从第一天显式接收 `TenantContext`，即使 Phase 1/2 暂时由 Cache Adapter 实现。

## 4. 公共领域契约

内部核心使用冻结 dataclass 或 Pydantic Model；HTTP Schema 单独放在 `athena/api/schemas.py`，不得让 Route 直接复用持久化 ORM 对象。

### 4.1 枚举与状态

```text
EnvironmentMode = LIVE | REPLAY | MOCK
DataOrigin      = live | replay | mock | document
RiskLevel       = S0 | S1 | S2 | S3 | S4 | S5
ExecutionProfile = direct_workflow | bounded_policy_loop | plan_execute

OpsTaskStatus = queued | running | waiting | succeeded | failed | cancelled
OpsTaskPhase  = validate | collect | analyze | plan | approve | execute | verify | report

ToolStatus    = succeeded | failed | rejected | timed_out
PlanStatus    = draft | approval_pending | approved | rejected | expired | executed | failed
ApprovalStatus = pending | approved | rejected | expired | revoked
SkillStatus   = draft | evaluating | review_pending | active | rolled_back | rejected
```

约束：

- 对外状态只使用 `OpsTaskStatus + OpsTaskPhase`，内部步骤不再额外泄漏成产品状态。
- `EnvironmentMode` 在 Task 创建时固定；LIVE Task 不接受 Mock/Replay 环境观测 Evidence。
- 风险取服务端计算的 `effective_risk`，模型 Confidence 从不参与授权。
- S4 默认关闭，S5 永久拒绝，租户策略只能收紧系统限制。

### 4.2 最小类型

```python
@dataclass(frozen=True)
class ActionDecision:
    action: str
    arguments: dict[str, JSONValue]
    reason_code: str
    confidence: float | None

@dataclass(frozen=True)
class Evidence:
    id: str
    tenant_id: str
    task_id: str
    type: str
    source: str
    data_origin: str
    summary: str
    content_ref: str | None
    content_hash: str
    observed_at: datetime
    collected_at: datetime

@dataclass(frozen=True)
class ToolSpecV2:
    name: str
    version: str
    domain: str
    input_schema: dict[str, JSONValue]
    output_schema: dict[str, JSONValue]
    required_capabilities: tuple[str, ...]
    risk_level: str
    readonly: bool
    idempotent: bool
    timeout_seconds: float

@dataclass(frozen=True)
class ToolCallV2:
    call_id: str
    task_id: str
    tenant_id: str
    tool_name: str
    arguments: dict[str, JSONValue]

@dataclass(frozen=True)
class ToolResultV2:
    status: str
    summary: str
    data: JSONValue | None
    evidence_refs: tuple[str, ...]
    error_code: str | None
    retryable: bool
```

`OpsTaskState` 至少保存目标、Environment/Scope、Tenant Policy Snapshot、已验证 Facts、Hypotheses、Action History、Budget、Execution Profile、Skill Version、Lease 与 `state_version`。原始 Prompt、完整 Thought 和完整 Tool Observation 不属于 TaskState。

### 4.3 Task Event 与 Trace

`TaskEvent` 是新的持久领域事件事实源，字段至少包括：

```text
task_id, tenant_id, sequence, event_type, phase
payload_redacted, reason_code, evidence_ids
created_at, trace_id
```

推荐事件类型：

```text
task.created
task.updated
decision.recorded
tool.started
tool.finished
evidence.created
approval.required
task.completed
task.failed
```

现有 `learning.Tracer`、Prometheus 和旧 API `StepTrace` 通过 Adapter 投影 `TaskEvent`，不再建立第四套 Trace。Decision Trace 只保存 Action、脱敏参数、reason_code、Evidence Ref、模型使用量和耗时，不保存隐藏 Thought 或未脱敏 Prompt。

### 4.4 统一错误码

新增错误码按领域前缀维护：

```text
TASK_*        状态、预算、取消、租约和恢复
EVIDENCE_*    来源、内容、脱敏和读取失败
TOOL_*        Schema、Scope、Risk、Timeout 与副作用
PLAN_*        Hash、过期、前置条件和幂等
APPROVAL_*    权限、状态、过期与重复审批
ENV_*         连接、Scope、Mode 与凭证
ALERT_*       Schema、Integration、Fingerprint 与生命周期
SKILL_*       生命周期、Benchmark 与兼容性
```

错误码必须稳定、可测试、可观测。HTTP 文本不得携带 Secret、完整日志、Kubeconfig、Token 或内部堆栈。

## 5. PR 与完成规则

每个任务按下列模板写入 PR 描述和本文件的完成证据栏：

```text
ID / 状态 / 依赖
目标行为
架构引用
复用与新增文件
公开 API、数据与配置变化
安全、Tenant、审计与可观测性
测试与验收命令
兼容、迁移与回滚
完成证据与残余风险
```

单个 PR 只实现一个可验证行为。不得在同一 PR 中同时做目录大迁移、数据库切换、前端重写和业务能力扩张。

所有任务的最低完成定义：

- 代码遵守第 3 节依赖方向，且没有 Route/Agent 绕过治理层。
- 增加成功路径与拒绝路径测试；安全关键分支不能用 Coverage Omit 回避。
- 新行为有 Error Code、Metric、Trace 和必要 Audit Event。
- Tenant、Scope、Secret、Data Origin 与审计失败策略已明确。
- 配置默认值、兼容路径、Feature Flag 与回滚方式已记录。
- 不宣称未实测的性能收益或生产成熟度。

## 6. 实施总路径

```text
Phase 0  基线、严格 LIVE、契约和 CI
   -> Phase 1  CrashLoop 只读垂直链路
   -> Phase 2  Environment 与任务式前端
   -> Phase 3  Plan / Approval / 受控写动作
   -> Phase 4  PostgreSQL / Worker / Tenant / Durable Alert
   -> Phase 5  Capability、Skill 治理、部署与正式发布
```

关键依赖顺序：

```text
Production Profile / Strict LIVE
 -> Core Contracts / Legacy Structured Trace
 -> EvidenceStore / ToolRuntime
 -> ContextManager / PatternPolicy / PolicyAgent
 -> WorkflowRunner / CrashLoop OpsTask
 -> Environment / Secret / Tenant Boundary
 -> OperationPlan / Approval
 -> PostgreSQL / Worker Lease / Recovery
 -> Durable Alertmanager
 -> Skill Offline Evaluation / Capability Extension / Release
```

Benchmark Case/Oracle、前端设计系统、Alert Parser 单元改造和部署清单可以并行，但都不得阻塞上述核心依赖或假设未完成的持久化能力。

## 7. Phase 0：基线、严格 LIVE 与契约

### P0-01 工具链与 CI 基线

| 项目 | 说明 |
|---|---|
| 状态 | DONE |
| 依赖 | 无 |
| 目标 | 让格式、类型、快速测试和 Coverage 在 CI 中真实运行，并统一 Python 版本和开发依赖 |

修改 `pyproject.toml`、`setup.py`、`README.md`、`docs/guides/development.md`、`Dockerfile` 与 `docker-compose.yml`；渐进新增 `requirements-dev.txt` 与 `.github/workflows/ci.yml`。运行时依赖与质量工具分离：Black、isort、mypy、pytest-cov 等进入开发依赖，不能依赖开发者机器的偶然安装。Phase 4 前 API 容器固定一个 Uvicorn worker；多 worker 与进程内 Task/Agent 状态不兼容。

CI 首期 Job：

```text
quality       black --check / isort --check-only / mypy
unit_api      普通 pytest + JUnit + Coverage
integration   Redis Service Container 下的现有集成测试
```

`live_k8s`、Benchmark、负载和未来 PostgreSQL 并发 Job 不进入每个 PR 的快速路径；它们先定义 Marker 和受保护触发条件。修正 `pyproject.toml`、`setup.py`、README 三处的 Python 版本描述为 3.12。

验收：

```text
black --check .
isort --check-only .
mypy athena examples tests
pytest -m "not integration and not live_k8s and not benchmark"
pytest --cov=athena --cov-report=term-missing
```

回滚：CI Job 可以临时标记非 required，但不得删除格式、类型或安全测试；运行时镜像不安装开发依赖。

### P0-02 Production Profile 与 Strict LIVE

| 项目 | 说明 |
|---|---|
| 状态 | IN_PROGRESS |
| 依赖 | P0-01 |
| 目标 | 显式区分 Demo 与 Production，保证 LIVE 观测失败不会产出 Mock 成功结果 |

修改 `athena/config.py`、`config.yaml`、`athena/tools/cloud/k8s/client.py`、`athena/tools/cloud/prometheus.py`、`athena/api/services.py` 和对应测试。新增逻辑 Profile：

```text
demo        显式允许 MOCK/内存 fallback
production  认证、Scope、依赖与 fallback policy 不合规时无法 Ready
```

K8s/Prometheus 调用增加 `fallback_policy=allow_mock|fail_closed` 或语义等价字段。`LIVE`、Benchmark、告警诊断和生产 OpsTask 必须使用 `fail_closed`；真实连接、认证、权限、超时或网络失败应返回结构化错误及实际 `data_origin`，而不是调用 Mock。

`_parse_k8s_namespace()` 在 Namespace 不属于 Allowlist 时返回 `ENV_SCOPE_DENIED`，不得改写为默认或首个白名单 Namespace。`/readyz` 扩展为返回 `configured_backend`、`active_backend`、`status`、`reason_code`；生产 Profile 对关键依赖的降级必须 Not Ready。

验收：

- 显式 MOCK 模式仍能运行 Demo。
- `mode=LIVE + fail_closed` 下任一 K8s 失败不会调用 Mock Client。
- Cloud Status 由实际调用来源生成，不能只读取配置字符串。
- 越权 Namespace、Prometheus 失联和 K8s 403 均有稳定错误码。

### P0-03 核心契约与结构化 Legacy Trace

| 项目 | 说明 |
|---|---|
| 状态 | DONE |
| 依赖 | P0-02 |
| 目标 | 固定新链的输入输出边界，同时以旁路方式采集旧链基线 |

渐进新增：

```text
athena/agent/policy/contracts.py
athena/agent/workflow/state.py
athena/memory/evidence.py
athena/tools/contracts.py
athena/observability/task_events.py
```

首期仅实现第 4 节的类型、序列化、状态合法性和 Redaction Helper。不要在此任务中引入 PostgreSQL、完整 Workflow 或前端。

修改 `ReActAgent` 加入可选 Observer。Observer 从 LLM usage、ToolResult 和步骤边界发布脱敏结构化 Event；legacy 行为及返回格式保持兼容。旧 `ReActDecision.thought` 可以留在 legacy 进程内调试对象中，但不得写入 `TaskEvent`，不得进入普通 API、前端或训练/Skill 语料。

验收：

- `ActionDecision`、`Evidence`、`ToolSpecV2`、`OpsTaskState` 对非法枚举、缺 Tenant、非法状态转换有单元测试。
- Observer 不改变 legacy ReAct 的 Tool 调用顺序和最终答案。
- Trace 可计算 LLM 调用数、Token、Tool 耗时和失败次数。
- 任意持久或 API Event 中不包含 Thought、Authorization、Secret 或原始 Prompt。

### P0-04 PatternPolicy 与 Feature Flag

| 项目 | 说明 |
|---|---|
| 状态 | IN_PROGRESS |
| 依赖 | P0-03 |
| 目标 | 用确定性代码选择最小执行策略，允许旧新链并行比较 |

渐进新增 `athena/agent/policy/pattern_policy.py`，修改 `config.py`、组合根和测试配置。配置：

```text
agent.execution_mode = legacy_react | policy_workflow
```

`PatternPolicy.select()` 的输入固定为 `task_type`、风险、必需 Capability、估算步骤、Evidence Fan-out、预算与当前置信度；输出为 Execution Profile 与受控 Modifier。V1 只允许：

```text
direct_workflow
bounded_policy_loop
plan_execute

parallel_read_collection
max_one_reflection
skill_guidance
```

Modifier 不是独立执行引擎。模型不能自由组合范式，也不能选择高于服务端允许范围的 Tool、风险或并发。

验收：相同输入总是得到相同 Profile；未知任务和不足 Scope 均收敛为有界只读或人工升级；Feature Flag 可以任务级回退 `legacy_react`，但不能回退 Strict LIVE、Scope 或 Risk 校验。

### P0-05 LIVE Benchmark 骨架

| 项目 | 说明 |
|---|---|
| 状态 | IN_PROGRESS |
| 依赖 | P0-02、P0-03 |
| 目标 | 让真实 Kubernetes Case 可以验证旧链基线，不把 Web 假 runner 当作能力报告 |

渐进新增：

```text
athena/evaluation/live_k8s.py
scripts/run_live_benchmark.py
benchmarks/k8s-live/schemas/
benchmarks/k8s-live/suites/
benchmarks/k8s-live/cases/
```

复用 `deploy/kind-demo/workloads/`，但每个 Case/Variant 使用独立 Namespace。Case 用 `case.yaml` 声明 Manifest、稳定条件、Ground Truth Oracle、必需 Evidence、禁止副作用和预算；Runner 动态发现 Case，不在代码中固定数量。

首期只包装 `legacy_react` runner。`policy_workflow` 未可运行前，报告必须显示“无配对数据”，禁止制造 A/B 结论。任何 `mock/replay/unknown` 环境 Evidence、Setup 失败、集群不等价或 Cleanup 失败均为无效基础设施结果，不能计入 Agent 通过/失败率。

验收：kind/Staging 中能够创建隔离 Case、等待真实故障、输出脱敏 Artifact、finally 清理并验证；报告记录代码、Case、模型、Tool、Skill 与集群快照版本。

### P0-06 Alert 入口安全修复

| 项目 | 说明 |
|---|---|
| 状态 | IN_PROGRESS |
| 依赖 | P0-02、P0-03 |
| 目标 | 修复当前 Webhook 的明显安全与事实边界，不提前承诺 Durable Alert 处理 |

修改 `athena/integration/alert_webhook.py`、`athena/api/routes/alerts.py`、`athena/api/services.py`、`tests/test_alerts_webhook.py`。首期只完成：

- 解析所有 Alert Item 的内部批量模型；不再忽略 `alerts[1:]`。
- 对 JSON 类型、Payload 边界、Label/Annotation 脱敏建立校验。
- 删除缺失 Alert Name 时伪造 CrashLoop 的生产行为；简化 Payload 仅在显式 Demo 模式支持。
- 禁止 Namespace 静默替换、LIVE 到 Mock 混入、Mock 结果进入生产 Knowledge。
- 修正 critical Alert 的 `alert.received` 审计成功语义。

本任务不引入 PostgreSQL Receipt/Event，也不返回 Durable `202`。现有同步 `200 processed` 保留为兼容 Demo 行为，直到 Phase 4。

### Phase 0 退出条件

- CI 有真实运行入口，快速测试和 Redis Integration 可以分别执行。
- LIVE/Mock 来源不可混淆，Scope 越权明确失败。
- 领域契约和 Task Event 可独立测试。
- legacy 仍可生成结构化基线指标。
- kind 真实 Case 能完成旧链单链路 Benchmark。

## 8. Phase 1：CrashLoop 只读垂直链路

### P1-01 ToolRuntime V1 与 Tool V2 Adapter

| 项目 | 说明 |
|---|---|
| 状态 | IN_PROGRESS |
| 依赖 | P0-03、P0-04 |
| 目标 | 让所有新链 Tool 调用经过相同治理管线 |

渐进新增 `athena/tools/runtime.py`，修改 `athena/tools/registry.py` 与 `athena/tools/cloud/k8s/tools.py`。现有 ToolRegistry 保留；V2 Adapter 将旧 Tool 描述、参数和结果适配为第 4 节契约。

唯一执行管线：

```text
Resolve Tool
 -> Validate input schema
 -> Tenant/Environment/Scope check
 -> Capability and effective-risk check
 -> Timeout/Retry/Circuit policy
 -> Execute Provider Adapter
 -> Normalize ToolResultV2
 -> Persist/redact Evidence
 -> TaskEvent + Audit
```

`PolicyAgent` 不得直接 `ToolRegistry.invoke()`。ToolRuntime 只暴露 ContextManager 已筛选、服务端允许的 Tool。只读 Tool 可自动运行；写 Tool 在 Phase 1 不能注册到新链。

验收：非法 Schema、未知 Tool、Scope 拒绝、超时、可重试错误、Secret Redaction 和 Audit 失败均有成对测试。Tool 结果大于 Context 预算时只返回摘要与 Evidence Ref。

### P1-02 TaskState 与 Evidence 过渡存储

| 项目 | 说明 |
|---|---|
| 状态 | IN_PROGRESS |
| 依赖 | P1-01 |
| 目标 | 在数据库到位前也让事实状态不依赖 Web 进程对象 |

修改 `athena/api/task_store.py`，渐进新增 `TaskStateRepository` 和 `EvidenceStore` 的 Cache Adapter。接口从首期携带 `TenantContext`、`state_version`、TTL 和 `content_ref`；不要复用 WorkingMemory 或 scratchpad 作为事实来源。

小型脱敏摘要可以在 Cache 中存储；日志、K8s JSON 和附件正文只保存受控内容引用与哈希。此实现是过渡层，不承诺 API/Worker 重启恢复或多副本锁正确性；Phase 4 使用相同接口替换为 PostgreSQL。

验收：Task、Evidence、取消标记和 Event 可在 Web 断线后查询；不同 Tenant 的 Cache Key 无法碰撞；摘要失败不会阻塞任务，原始 Evidence 不直接进入 Prompt。

### P1-03 ContextManager V1

| 项目 | 说明 |
|---|---|
| 状态 | IN_PROGRESS |
| 依赖 | P1-02 |
| 目标 | 以规则 Reducer 构造最小、可审计的单轮上下文 |

渐进新增：

```text
athena/agent/context/manager.py
athena/agent/context/reducers.py
```

构建顺序固定：身份/Tenant/风险策略 -> 目标/Environment/预算 -> 已验证 Facts/近期 Action -> 当前 Evidence 摘要 -> Knowledge -> Active Skill -> 允许的 Profile 偏好。低优先级内容不得覆盖高优先级约束。

Reducer V1 必须：折叠重复日志、提取错误码与 Stack 指纹、压缩 K8s 大对象、保留 Prometheus 聚合和查询窗口、保留 `resource_id/time_range/error_code/evidence_id`。最新目标、Scope、时间范围与安全策略永不压缩。ContextManager 只暴露本轮 Capability 对应的 Tool Schema。

验收：同一 Evidence 输入生成确定性摘要；注入日志中的 Prompt 指令不会改变约束；压缩前后 Token、被折叠行数和遗漏字段进入 Metric/Test。

### P1-04 PolicyAgent 与 WorkflowRunner

| 项目 | 说明 |
|---|---|
| 状态 | IN_PROGRESS |
| 依赖 | P1-01、P1-03 |
| 目标 | 用有界单 Action 决策替代自由字符串驱动的 Agent Loop |

渐进新增：

```text
athena/agent/policy/agent.py
athena/agent/workflow/runner.py
athena/agent/workflow/crashloop.py
```

`PolicyAgent` 的输出必须经 Pydantic/Schema 校验为 `ActionDecision`，并且 Action 必须在 Context 的 `available_actions` 中。非法 JSON 最多允许一次受预算 Repair；仍失败则进入 `rules_only`、`unknown` 或 `workflow.escalate`，不得复用 ReAct 的“非法 JSON 作为 final answer”兜底。

`WorkflowRunner.tick()` 每次最多一次模型决策和一次 Action；由 Runner 负责状态迁移、预算、重试、取消、等待输入、等待审批和完成判断。CrashLoop Workflow 首期只执行 Workload/Pod/Event/Log/Metric 只读收集与报告。

```python
async def tick(task_id: str) -> OpsTaskState:
    state = task_repository.load_for_update(task_id)
    workflow.validate_transition(state)
    context = context_manager.build(state)
    decision = await policy_agent.decide(context)
    result = await tool_runtime.invoke(state, decision)
    next_state = workflow.reduce(state, decision, result)
    task_repository.save(next_state)
    event_repository.append(next_state.last_event)
    return next_state
```

验收：Workflow 处理预算耗尽、重复无进展、取消、Tool 失败、Evidence 冲突和规则降级；无法用模型输出绕过 Scope/Risk；每个 Root Cause 引用 Evidence ID。

### P1-05 OpsTask 应用服务、API 与真实 SSE

| 项目 | 说明 |
|---|---|
| 状态 | IN_PROGRESS |
| 依赖 | P1-02、P1-04 |
| 目标 | 将用户可见的主对象从 Chat Session 转为可恢复的 OpsTask |

渐进新增 `athena/application/ops_task_service.py` 和 `athena/api/routes/ops_tasks.py`；修改 `schemas.py`、`server.py` 与 `AthenaWebService`。首期 API：

```text
GET    /api/ops/tasks
POST   /api/ops/tasks
GET    /api/ops/tasks/{id}
POST   /api/ops/tasks/{id}/cancel
POST   /api/ops/tasks/{id}/input
GET    /api/ops/tasks/{id}/events
GET    /api/ops/tasks/{id}/evidence
GET    /api/ops/tasks/{id}/report
```

创建、取消和补充输入使用 Idempotency Key；所有接口使用 TenantContext 与 Scope。SSE 以写入时产生的 `TaskEvent.sequence` 为事实，格式为 `id/event/data`，支持 `Last-Event-ID` 或 `after_seq` 重连。任务详情 API 始终是真实来源，SSE 只是增量通知。

保留 `/api/tasks`、`/api/workflow`、`/api/cloud-ops`，由兼容门面委托或返回弃用信息；不要修改已有 URL 的语义。

### P1-06 CrashLoop 页面与渐进前端连接

在当前静态前端中先增加最小 OpsTask 页面数据适配，不进行完整模块拆分。显示 Environment Mode、Task Phase、阶段事件、Evidence 摘要、数据来源、降级状态和取消按钮；不显示 Thought。

只有后端具有 `GET /api/ops/tasks/{id}` 和 Event/Evidence 事实接口后才显示入口。WebSocket 不新增，继续使用 SSE。断线后先拉取详情再从最后 sequence 续订。

### P1-07 新旧链路 LIVE 配对 Benchmark

将 `policy_workflow` Runner 接入 P0-05 的配对机制。在相同模型、Temperature、Capability、预算、语言和等价 Ground Truth 下比较：

```text
root-cause accuracy
required evidence recall
unsupported claim rate
tool/LLM calls
input/output tokens
time to first evidence / diagnosis
write attempt / namespace escape / non-live evidence
```

规则诊断器只能作为校准基线，不能标作 legacy Agent。安全违规为零容忍；质量、延迟和成本门槛来自版本化 Suite Policy，而非硬编码数字。

### P1-08 Alert 到只读 OpsTask 的内部验证

已知 CrashLoop Alert 可以经 Parser 转换为同一条只读 Workflow。若暂时使用 `AsyncTaskManager`，响应必须显式携带：

```text
durability=process_local
```

该路径只能用于 Demo/内部验证，不能返回 Durable `202`、不能启用生产 Alertmanager Receiver、不能承诺进程重启恢复。

### Phase 1 退出条件

- CrashLoop 结论仅引用 LIVE Evidence，日志大正文不直接进入 Prompt。
- ToolRuntime 覆盖 Schema、Scope、Risk、Timeout、Evidence、Redaction 与 Audit。
- API 断线后可查询 Task 最终状态，SSE 不再是完整执行后的回放。
- `policy_workflow` 与 `legacy_react` 可以在真实 Case 中生成配对报告。
- 新链没有任何 K8s 写 Tool，Alert 内部验证不具备生产 Durable 语义。

## 9. Phase 2：Environment、Secret 与任务式前端

### P2-01 Environment 领域与连接服务

| 项目 | 说明 |
|---|---|
| 状态 | NOT_STARTED |
| 依赖 | P1-05 |
| 目标 | 让用户显式管理已授权的 Kubernetes、Prometheus 和 LLM 连接，而不是依赖全局配置猜测 |

渐进新增：

```text
athena/application/environment_service.py
athena/api/routes/environments.py
```

首期 Environment 字段：

```text
id, tenant_id, name, type, provider, mode
scope, credential_ref, capabilities, status
last_checked_at, created_at, updated_at
```

`mode` 为 `LIVE|REPLAY|MOCK`，创建后不能在同一 OpsTask 中改变。Scope 至少表达 Cluster、Namespace、Resource Kind 与时间窗口边界。Capability 是服务端声明的 `k8s.workload.read`、`k8s.logs.read`、`metrics.query` 等，不允许前端任意填写。

API：

```text
GET    /api/environments
POST   /api/environments
GET    /api/environments/{id}
PATCH  /api/environments/{id}
DELETE /api/environments/{id}
POST   /api/environments/{id}/test
POST   /api/environments/{id}/sync
```

`test` 返回脱敏连接状态、实际 backend、能力、Scope 校验与错误码，不返回 credential。删除已被 Task/Plan 引用的 Environment 时先停用，保留审计链。

### P2-02 SecretStore 与模型配置迁移

| 项目 | 说明 |
|---|---|
| 状态 | NOT_STARTED |
| 依赖 | P2-01 |
| 目标 | 消除 LLM API Key 明文存 Cache 的生产风险，并为云凭证提供统一引用 |

在现有包内渐进新增 `SecretStore` Protocol 和本地加密实现；修改 `llm_config_store.py`、模型配置 API、配置与日志 Redaction。存储原则：

```text
数据库/Cache 元数据：credential_ref、masked suffix、provider、状态
Secret Store：密文或外部 Secret/KMS/Vault 引用
浏览器/API 响应：永不返回 Secret 明文
```

本地自托管使用配置注入的主密钥加密；Kubernetes 使用 Secret/KMS/Vault Adapter。没有主密钥、生产 Credential 或脱敏策略时，Production Profile 不可 Ready。旧 Redis 明文 Key 只迁移一次并删除旧键，不做无限期双写。

模型配置 API 在现有 `/api/llm/configs` 基础上增加 Tenant、测试连接、默认切换、停用和密钥轮换；所有查询按 Tenant Filter，Secret 只以 `****suffix` 展示。

### P2-03 Tenant 与现有 API 收口

| 项目 | 说明 |
|---|---|
| 状态 | NOT_STARTED |
| 依赖 | P2-01、P2-02 |
| 目标 | 在 PostgreSQL 到位前就固定跨租户访问边界，避免后期大规模返工 |

检查并改造 Session、Chat、Trace、Workflow Status、Task、Knowledge、LLM Config、Audit、Alert History 与 Benchmark Report 查询。每个 Route 必须：

- 使用 `require_tenant` 或等价依赖。
- 将 TenantContext 传到 Service/Store，不从 Query 参数接受目标 Tenant。
- 返回 403/404 的策略保持一致，不泄漏其他 Tenant 是否存在资源。
- 在 Cache Key、列表索引和 Idempotency Key 中包含 Tenant。

`/api/audit/events?tenant_id=` 不允许由普通调用者覆盖当前 Tenant；跨 Tenant 审计只允许明确的系统管理员 Scope。鉴权关闭、`roles=*`、空 Namespace Allowlist 等宽松行为只允许 Demo Profile，Production Profile Fail-Closed。

### P2-04 前端模块化基础

| 项目 | 说明 |
|---|---|
| 状态 | NOT_STARTED |
| 依赖 | P1-05、P2-01 |
| 目标 | 在不迁移框架的前提下，把 UI 从聊天单页演进为任务控制台 |

按以下目录从现有 `index.html`、`app.js`、`style.css` 迁移，迁移期间入口文件只做装配：

```text
athena/web/static/
  core/api.js
  core/router.js
  core/store.js
  pages/overview.js
  pages/operations.js
  pages/alerts.js
  pages/connections.js
  pages/audit.js
  pages/model-settings.js
  components/status-badge.js
  components/task-timeline.js
  components/evidence-panel.js
  components/dialog.js
  components/empty-state.js
  styles/tokens.css
  styles/layout.css
  styles/components.css
  styles/pages.css
```

使用 Hash Router：

```text
#/overview
#/operations
#/operations/{task_id}
#/alerts
#/connections
#/audit
#/settings/models
```

`appState` 仅保存身份、路由、全局健康；`taskStore`、`connectionStore`、`sessionStore` 分领域保存事实缓存。页面刷新或 SSE 重连必须从 API 重建状态，不能把 Task 事实存进 LocalStorage。

移除 Tailwind CDN，使用本地 CSS Token；按钮图标采用已打包的 Lucide 资源。Secret、完整 Evidence、认证 Header、原始 Prompt 不进入 DOM、LocalStorage 或前端错误提示。

### P2-05 总览、连接与智能运维工作台

实现顺序：

1. App Shell、导航、全局健康、非 LIVE 水印和空状态。
2. `连接环境` 与 Environment 测试表单，先支持 Kubernetes、Prometheus、LLM Provider。
3. 总览展示最近 Task、告警、依赖与 Environment 健康。
4. 智能运维工作台展示 Context Bar、Task 时间线、Evidence/风险 Inspector、预算和取消动作。
5. 告警记录页面展示来源状态、处理状态、关联 Task、Evidence 和重跑入口。
6. 审计、模型设置；Approval/Skill/Tool 管理只有后端生命周期到位后开放。

每个页面都必须覆盖 `loading`、`empty`、`forbidden`、`degraded`、`error`、SSE 断线和最终状态。REPLAY/MOCK 始终展示水印；手机端只支持查看任务、告警、审批，不承载复杂连接配置。

### P2-06 首次使用向导

向导步骤固定：

```text
连接 Kubernetes
 -> 配置 LLM
 -> 可选连接 Prometheus
 -> 运行首次只读巡检
 -> 进入控制台
```

按钮使用 `保存并测试`、`跳过`、`运行只读巡检`。没有 Environment 时总览主操作是 `连接环境`；没有 LLM 时只禁用依赖 LLM 的诊断/聊天，健康、审计、Environment 与告警查询仍可用。

### Phase 2 退出条件

- 用户可在 UI 中完成连接配置、连接测试和首次只读巡检。
- Environment、Task、模型配置和审计查询经过 Tenant/Scope 过滤。
- Secret 不在任何浏览器存储或 API 响应中出现。
- 前端围绕 OpsTask 展示，非 LIVE 与降级状态持续可见。

## 10. Phase 3：OperationPlan、Approval 与受控 S3

### P3-01 OperationPlan 与 Plan Hash

| 项目 | 说明 |
|---|---|
| 状态 | NOT_STARTED |
| 依赖 | P1-01、P1-05、P2-01 |
| 目标 | 让人类批准不可变计划，而不是批准自然语言或 `confirmed=true` |

渐进新增：

```text
athena/application/approval_service.py
athena/api/routes/operation_plans.py
athena/api/routes/approvals.py
```

修改 `athena/tools/cloud/k8s/actions.py`、`schemas.py`、`server.py`。`OperationPlan` 最小内容：

```text
id, tenant_id, task_id, version, plan_hash
operation, target, canonical_arguments, effective_risk
impact_scope, preconditions, dry_run
verification_rule, rollback_plan, expires_at
```

`plan_hash` 由稳定 canonical JSON 计算。变更目标、参数、风险、Dry Run、验证或回滚任一项，都必须创建新版本并使旧 Approval 失效。`rollout restart` 将固定 `restartedAt` 值写入 Plan；`scale` 固定目标 replicas，保证重试幂等。

### P3-02 Approval、权限与 API

Approval 必须绑定：

```text
tenant_id + user_id + plan_id + plan_hash + expiry
```

API：

```text
GET  /api/operation-plans/{id}
POST /api/operation-plans/{id}/request-approval
GET  /api/approvals
POST /api/approvals/{id}/approve
POST /api/approvals/{id}/reject
POST /api/operation-plans/{id}/execute
```

所有写接口使用 Idempotency Key。审批用户需有独立 Scope；Alert Integration、Skill 和模型本身没有 Approval 权限。重复批准、跨 Tenant、过期、拒绝、撤销、Plan Hash 不匹配或资源版本变化均返回稳定错误码。

### P3-03 受控执行与副作用恢复

首期只实现 `k8s.rollout.restart` 与 `k8s.workload.scale`。ToolRuntime 在执行前再次校验：Tenant、Environment、Namespace、Capability、effective risk、Plan Hash、Approval、过期、资源版本、前置条件和 Idempotency Key。

渐进新增 `tool_effects` 记录：

```text
call_id, tenant_id, task_id, plan_hash, status
started_at, completed_at, result_ref, postcondition_status
```

网络调用成功但 Checkpoint 未写入时，恢复 Worker 必须先做确定性 Post-condition 检查，不能盲目重复写操作。写前、写后、拒绝和恢复均写 Audit；Audit Store 不可用时写操作 Fail-Closed。

### P3-04 前端审批与写操作边界

Task Inspector 增加 `处置计划`、`影响与风险`、`审批` 标签页。用户只可查看不可变 Plan、Dry Run、影响范围、验证和回滚说明；操作按钮只在状态和 Scope 合法时显示。

`confirmed=true` 在兼容 API 中只返回 Plan 创建提示或 `CONFIRMATION_DEPRECATED`，永远不执行新写操作。S4 V1 关闭；S5 不显示、不可配置、不可通过 API 调用。

### Phase 3 退出条件

- 任一 Plan 内容变化都使旧 Approval 失效。
- 重复执行请求不产生重复副作用。
- 无 Approval 的 S3 无法经任何 Route、Tool 或恢复链路执行。
- 删除 Namespace/PVC、RBAC、Secret 修改继续被永久/默认阻断。
- 在 Phase 4 的 PostgreSQL、恢复和审计门禁完成前，生产 S3 Flag 始终为 false。

## 11. Phase 4：PostgreSQL、Worker、Tenant 与 Durable Alert

### P4-01 数据库、依赖与 Migration 基线

| 项目 | 说明 |
|---|---|
| 状态 | NOT_STARTED |
| 依赖 | Phase 1/2 契约稳定 |
| 目标 | 以 PostgreSQL 替代 Cache 作为任务、审批、审计与治理事实库 |

当前仓库虽然声明 SQLAlchemy，但没有 Engine、ORM、PostgreSQL Driver 或 Alembic。新增运行依赖 `asyncpg`、`alembic`、`cryptography`，新增 `migrations/`，并建立 SQLAlchemy 2 异步 Session Factory。数据库 URL 只通过配置/Secret 注入，不能写入 Git。

Migration 采用 Expand-first：

```text
add schema/table/index
 -> deploy code that reads old + new where required
 -> backfill only explicitly supported metadata
 -> switch reads/writes
 -> remove old compatibility after retention window
```

不要求将旧 Cache TTL Session/Task/Alert 变成生产事实；新版本以 fresh install 为基线。对用户可见的知识、Runbook、模型配置可提供受控导入/导出，而非隐式迁移。

### P4-02 PostgreSQL Schema 与索引

首批表按职责分组：

```text
Identity/config
  environments
  secrets_metadata
  model_configs
  alert_integrations

Conversation/knowledge
  conversation_sessions
  conversation_messages
  knowledge_documents
  knowledge_versions
  experience_records

Task/evidence
  ops_tasks
  task_events
  evidences
  tool_effects

Change governance
  operation_plans
  approvals

Learning
  skill_definitions
  skill_versions

Alert
  alert_receipts
  alert_events
  alert_instances

Audit/evaluation
  audit_heads
  audit_events
  benchmark_runs
  benchmark_case_results
```

`ops_tasks` 至少包含：

```text
id, tenant_id, workflow_type, objective, environment_id
status, phase, state_version, execution_profile
budget_json, policy_snapshot_json, config_snapshot_json
trigger_type, trigger_ref, skill_version_id
lease_owner, lease_expires_at, lease_generation
checkpoint_version, next_run_at, attempt_count
created_by, created_at, updated_at
```

`task_events` 使用 `(tenant_id, task_id, sequence)` 唯一索引；`evidences` 使用 `(tenant_id, task_id, id)`；所有外键、唯一键和查询条件都包含 Tenant。必要索引包括待领取任务、Task 时间线、Environment 状态、Active Skill、Alert Canonical Fingerprint、未过期 Approval 和 Audit Head。

大 Evidence 正文存 `content_ref`，关系表保存摘要、哈希、来源、观察时间与保留策略。数据库不存原始 Secret、完整 Prompt 或隐藏 Thought。

### P4-03 Repository 与事务边界

渐进新增 `athena/api/repositories/`，以领域划分 Repository，不建设通用万能 DAO：

```text
environment_repository.py
task_repository.py
evidence_repository.py
plan_repository.py
approval_repository.py
skill_repository.py
alert_repository.py
audit_repository.py
```

每个 public 方法显式接收 `TenantContext`。Route 不能向 Repository 传裸 `tenant_id` 以外的数据来模拟跨租户访问。

事务边界：

```text
T1 Task command: Task 状态/事件/幂等记录原子写入
T2 Tool effect: effect 开始记录 -> 外部调用 -> post-condition -> result/checkpoint
T3 Plan/Approval: Plan + hash 或 Approval 终态原子写入
T4 Alert receipt: Receipt + Event + 审计意图原子写入
T5 Alert consume: Instance 投影 + OpsTask create/reuse + checkpoint 原子写入
```

写副作用不承诺 exactly-once；承诺 `at-least-once delivery + idempotent effect`。过期 Worker 使用 `lease_generation` 或 `state_version` 提交结果时必须被拒绝。

### P4-04 Worker、Lease 与恢复

| 项目 | 说明 |
|---|---|
| 状态 | NOT_STARTED |
| 依赖 | P4-01、P4-03 |
| 目标 | 让 API/Worker 可重启、可扩展，正确性不依赖进程内 Task/Agent |

渐进新增 `athena/application/task_worker.py`，CLI 增加 Worker 启动命令。API 与 Worker 使用同一镜像：

```text
athena web       API/SSE
athena worker    领取 lease、执行 tick、保存 checkpoint
```

Worker 通过 `FOR UPDATE SKIP LOCKED` 或条件更新领取 `queued/next_run_at` Task；续租时更新 generation；崩溃后 Lease 到期由其他 Worker 接管。每个 ToolCall 使用稳定 `call_id`；写 Tool 同时绑定 `plan_hash`。API 不持有唯一 Agent、Session、Approval 或 Task 状态。

SSE 从 `task_events` 查询历史再订阅通知。Redis Pub/Sub 仅用于降低延迟，消息丢失后客户端必须从 `sequence` 回放数据库 Event。

### P4-05 Audit、Tenant 与生产 Ready

`HashChainAuditStore` 的 Cache TTL 和并发语义不能作为生产审计事实。Phase 4 使用 `audit_heads(tenant_id)` 行锁追加 append-only `audit_events`：

```text
hash = sha256(previous_hash + canonical_audit_payload)
```

每租户序号缺失、Hash 断裂、并发 fork 都使 Verify 失败。安全/治理行为进入 Hash Chain；高频 Task 生命周期保留在 `task_events`，不把 Hash Chain 当事件总线。写操作审计不可用时 Fail-Closed。

Production `/readyz` 至少检查认证、PostgreSQL、Secret Store、审计策略、所需 Environment 依赖和 fallback policy。输出 configured/active backend 与 degraded reason；Production 不能在 Redis/Vector/K8s/Secret 关键依赖退化时继续标记 Ready。

### P4-06 Bootstrap 与 AthenaWebService 拆分

渐进新增：

```text
athena/bootstrap/agent_factory.py
athena/bootstrap/application.py
athena/application/chat_service.py
athena/application/ops_task_service.py
athena/application/environment_service.py
athena/application/approval_service.py
athena/application/audit_query_service.py
```

CLI、API、Worker 都从 Bootstrap 获得配置、Repository、Tool Bundle、LLM、SecretStore 和服务实例。`AthenaWebService` 在兼容期只做委托，不再创建长期正确性依赖的进程内 Agent Map。拆分按调用方迁移，禁止一次性移动整个文件。

### P4-07 Durable Alertmanager 闭环

| 项目 | 说明 |
|---|---|
| 状态 | NOT_STARTED |
| 依赖 | P4-01、P4-04、P4-06 |
| 目标 | 将 Alertmanager 从同步 Demo 迁移为机器认证、持久受理、可恢复的只读 Task Trigger |

在 `api/auth.py` 中增加 `IntegrationPrincipal`，而非复用交互用户 API Key。Integration Token Hash/证书映射固定：

```text
integration_id -> tenant_id + environment_id + allowed_scope=alerts:ingest
```

生产凭证只绑定 LIVE Environment；Demo MOCK Integration 必须独立配置与水印。Tenant/Environment 绝不从 Alert Labels 推导。

Webhook 处理：

```text
Authenticate Integration
 -> enforce body/type limit
 -> parse/normalize every Alert item
 -> canonical fingerprint after tenant/environment mapping
 -> T4 persist receipt + events + audit intent
 -> 202 Accepted
 -> Worker T5 updates instance and creates/reuses readonly OpsTask
```

Canonical Fingerprint 由稳定 Alert Name、Namespace、资源身份和版本化稳定 Labels 计算。Payload `fingerprint` 仅作来源提示，不能直接作为唯一键。V1 只做精确 Fingerprint 去重：一个 AlertInstance 同时最多一个活动 OpsTask；`groupKey` 仅用于列表归组/Coalesce，不实现跨告警智能关联。

Alert Event 是 Trigger Provenance，不是诊断 Evidence。Annotation 只能作为低优先级、不可信上下文；不抓取外部 URL、不执行其中指令、不计作根因证据。firing/resolved 生命周期、乱序、重复投递、等待 Mapping、Scope Block 和 Task 状态分别记录，不复制一套新的 OpsTask 状态机。

只有 Receipt/Event 事务已完成时返回 Durable `202`。数据库拒绝时返回可重试非 2xx；一旦持久化成功，即使队列拥塞也返回 `202` 并由内部背压处理，避免 Alertmanager 重投造成重复。

### P4-08 S3 发布门禁

Phase 4 完成后，只有以下条件同时通过才可以为指定 Tenant/Environment 开启 S3：

- Plan/Approval/Tool Effect/Audit 全部持久化。
- Worker Lease、恢复、幂等与过期 Worker 拒绝测试通过。
- Repository 和 API 的 Tenant/Scope 隔离测试通过。
- Environment LIVE Scope、Credential 和 `fail_closed` 检查通过。
- Feature Flag 显式开启，并保留可观察 Canary。

### Phase 4 退出条件

- API/Worker 重启后 Task、Alert、Approval 和 Evidence 元数据可恢复。
- 多 API 副本不依赖粘性 Session 或本地内存正确性。
- 两个 Tenant 不能互查 Task、Evidence、Environment、Model Config、Skill、Audit 或 Alert。
- 重复 Alert 只产生一个活动 Task；持久化后才返回 202。
- 未批准的 S3 无法通过 API、Tool、恢复或 Alert 链路执行。

## 12. Phase 5：Capability、Skill 治理与正式发布

### P5-01 Capability Bundle 与第二 Workflow

| 项目 | 说明 |
|---|---|
| 状态 | NOT_STARTED |
| 依赖 | Phase 4 |
| 目标 | 新 Provider/场景通过注册接入，不修改 WorkflowRunner 核心循环 |

渐进新增静态 `CapabilityBundle`：

```text
KubernetesReadBundle
KubernetesChangeBundle
PrometheusBundle
LogSearchBundle
KnowledgeBundle
```

新 Provider 实现 Connection Validator、声明 Capability 的 Tool Adapter、注册 Bundle、增加契约测试并在 Environment 页面暴露字段。首个扩展 Workflow 选择 PodPending、ImagePull 或 Service Reachability 中实际 Benchmark 价值最高的一类。V1 不引入运行时第三方插件、在线安装或 YAML DSL。

### P5-02 Skill 生命周期与离线自进化

Skill 迁移到 `skill_definitions`、`skill_versions`，持久化 Manifest、版本、状态、来源 Task、Benchmark 报告、Owner 和 Active Pointer。运行时检索顺序：

```text
Tenant/Status/Environment/Capability/Risk hard filter
 -> semantic candidate retrieval
 -> freshness/utility/confidence rerank
 -> policy compatibility
 -> limited summary injection
```

只有满足“成功、证据完整、无安全违规、来自多个相似样本或人工发起”的轨迹才能生成 Draft。现有 Generator、Validator、Curator 只作为候选骨架，必须补：静态 Schema/Capability/Risk 校验、离线 Replay、固定 Benchmark、人工审核、灰度 Active 和原子回滚。

任务启动时固定 `skill_version_id`；Draft/Evaluating/Review Pending 永远不能被生产召回。Skill 只能提供流程建议，不能创建 Tool、Script、权限或网络能力。

### P5-03 日志 Provider 与 Script 边界

实现一个真实日志 Provider（按客户环境选择），通过 `logs.search` Capability 接入，不修改 Runner。原始日志先脱敏、分片、外置，再向 ContextManager 提供摘要和 Evidence Ref。

`scripts/` 只用于开发、压测、索引和部署，永远不注册为 Agent Tool。K8s V1 优先使用 SDK，不提前建设 ScriptRunner。只有后续 Capability 真实依赖经过签名的运行时脚本时，才实现同步隔离 Runner；模型生成的 Shell/Python 永远不能直接生产执行。

### P5-04 Docker Compose、Helm 与运维手册

Compose 目标服务：

```text
postgres
redis
athena-api
athena-worker
migration (one-shot)
```

API/Worker 同镜像、不同 command；单容器单进程，水平扩缩容通过副本完成。健康检查使用 `/healthz`，就绪使用 `/readyz`。

Helm Chart 或等价 K8s 发布资产必须包含 API Deployment、Worker Deployment、Migration Job、Secret 引用、ServiceAccount/RBAC、PDB、HPA、NetworkPolicy、Ingress、Environment values 和 Evidence 存储配置。统一 Compose、Kubernetes 和 Alertmanager 中的服务名，不能继续混用 `athena`、`athena-app`、`athena-api`。

交付备份、恢复、升级、回滚、Secret 轮换、RBAC 最小权限和故障排查 Runbook。Migration 使用向后兼容策略，不依赖破坏性 Down Migration。

### P5-05 发布 Benchmark 与安全门禁

完成 Alertmanager `firing -> LIVE Evidence -> diagnosis -> resolved` 端到端场景。Nightly/Release 在受控 kind/Staging 执行 LIVE Case、并发、负载和 A/B Benchmark；所有 Artifact 脱敏并绑定 commit、Case、模型、Tool、Skill 与集群版本。

正式发布前至少满足 Proposal 第 27 节、附录 A/B/C 的门禁和本文件第 19 节验收矩阵。

### Phase 5 退出条件

- 新 Provider 可通过 Bundle 接入且不修改 Runner。
- Draft Skill 不能生产召回；Active/回滚版本行为可复现。
- 新环境可按 Compose 或 Helm 文档完成安装、迁移、初始化、巡检与升级。
- LIVE Benchmark、Alert E2E、安全扫描和备份恢复报告可随 Release 生成。

## 13. API、SSE 与兼容实现规格

### 13.1 通用 API 规则

- 所有新业务 Route 使用 `TenantContext`、Scope、Trace ID 和统一错误响应。
- 所有创建、取消、审批、执行、Webhook 受理等命令 API 要求 Idempotency Key；相同 Tenant + Key + request hash 返回同一结果。
- 请求/响应 Schema 使用 Pydantic；内部契约、ORM Entity 与 HTTP Schema 分离。
- 响应仅包含脱敏参数、摘要和引用；Evidence 内容通过授权详情接口按需读取。
- 写命令的审计失败策略显式声明；S3/S4 始终 Fail-Closed。
- 所有列表使用 Tenant Filter、稳定排序与游标分页；不能仅用 `limit` 扫描全局内存索引。

### 13.2 Environment API

| Endpoint | Scope | 幂等 | 审计 | 关键行为 |
|---|---|---|---|---|
| `GET /api/environments` | `environment:read` | 否 | 否 | 仅返回当前 Tenant，可按 type/status 过滤 |
| `POST /api/environments` | `environment:write` | 是 | 是 | 保存 Scope/credential_ref，不存 Secret 明文 |
| `GET/PATCH/DELETE /api/environments/{id}` | `environment:read/write` | PATCH/DELETE 是 | 写操作是 | 删除转停用，已引用资源不可物理删除 |
| `POST .../{id}/test` | `environment:test` | 是 | 是 | 连接测试、能力/Scope/实际来源，不泄露凭证 |
| `POST .../{id}/sync` | `environment:sync` | 是 | 是 | 仅同步允许范围元数据，受预算限制 |

创建/修改输入包括名称、类型、Provider、Mode、Scope、Credential Reference 与 Capability 请求；服务端计算允许 Capability，客户端不能直接提升权限。`LIVE` 连接测试失败时返回 `ENV_CONNECTION_FAILED`，不切 Mock。

### 13.3 OpsTask API

| Endpoint | Scope | 行为 |
|---|---|---|
| `POST /api/ops/tasks` | `ops:run` | 创建 Task、固定 Environment/Policy/Skill Snapshot、返回 task id 与初始状态 |
| `GET /api/ops/tasks` | `ops:read` | 游标分页，支持 status/phase/environment/time 过滤 |
| `GET /api/ops/tasks/{id}` | `ops:read` | Task 事实详情、预算、降级、引用数量，不返回 Thought |
| `POST /api/ops/tasks/{id}/cancel` | `ops:cancel` | 写取消请求并由 Runner 在安全点处理 |
| `POST /api/ops/tasks/{id}/input` | `ops:run` | 满足 WAITING_INPUT，记录脱敏输入 Event |
| `GET /api/ops/tasks/{id}/events` | `ops:read` | SSE 或增量 Event，支持重连 |
| `GET /api/ops/tasks/{id}/evidence` | `ops:read` | Evidence 摘要和授权引用 |
| `GET /api/ops/tasks/{id}/report` | `ops:read` | 结构化诊断/验证报告 |

`POST /api/ops/tasks` 请求至少包含 `objective`、`environment_id`、可选时间范围与场景。服务端从 Environment 获取 Tenant、Scope、Mode、Capability；客户端提交的 Namespace/资源只能进一步收窄，不能扩大。

### 13.4 Task SSE 协议

SSE Response：

```text
id: <task event sequence>
event: task.updated | evidence.created | approval.required | task.completed | task.failed
data: <redacted JSON>
```

服务器读取 `Last-Event-ID` 或 `after_seq`，先从 `task_events` 补发，再订阅增量通知。若 sequence 已超过保留窗口，返回 `EVENT_CURSOR_EXPIRED` 并让客户端重新拉取 Task Detail。客户端不能因为连接关闭取消后台 Task；取消只能通过显式命令 API。

### 13.5 Plan 与 Approval API

| Endpoint | Scope | 前置条件 |
|---|---|---|
| `GET /api/operation-plans/{id}` | `plan:read` | Tenant 与 Task/Plan Link 匹配 |
| `POST .../request-approval` | `plan:request` | Plan 是可审批、未过期、Dry Run 合法 |
| `GET /api/approvals` | `approval:read` | Tenant Filter、可按状态/计划查询 |
| `POST /api/approvals/{id}/approve` | `approval:approve` | 用户与 Scope、Plan Hash、有效期匹配 |
| `POST /api/approvals/{id}/reject` | `approval:approve` | Approval 仍为 pending |
| `POST /api/operation-plans/{id}/execute` | `operation:execute` | 有效 Approval、Plan Hash、前置条件与 Risk 全通过 |

Approval 响应不接受由前端传入的风险等级、Hash 或 Target 覆盖服务端记录。执行 API 再次加载 Plan，而不是相信请求 Body 中的 Operation。

### 13.6 Skill、Alert 与兼容 API

Skill：

```text
GET  /api/skills
GET  /api/skills/{id}
GET  /api/skills/{id}/versions
POST /api/skills/{id}/versions/{version}/evaluate
POST /api/skills/{id}/versions/{version}/submit-review
POST /api/skills/{id}/versions/{version}/approve
POST /api/skills/{id}/versions/{version}/reject
POST /api/skills/{id}/versions/{version}/rollback
```

Alert：

```text
POST /api/alerts/webhook
GET  /api/alerts
GET  /api/alerts/{instance_id}
GET  /api/alerts/{instance_id}/events
POST /api/alerts/{instance_id}/diagnose
```

`/api/alerts/webhook` 仅接受 Integration Principal。`/api/alerts/history` 在兼容期委托持久化查询；用户查询/重跑使用 Tenant Scope。没有确认、分派、静默和解决的后端状态机前，不提供对应 UI/API。

兼容策略：

| 旧入口 | 保留方式 | 禁止事项 |
|---|---|---|
| `/api/chat`、`/api/chat/stream` | 保持普通聊天；逐步接 Tenant/Trace | 不进入生产写操作链 |
| `/api/workflow/*` | 兼容旧 Workflow 查询 | 不作为新 OpsTask 事实来源 |
| `/api/tasks/*` | 兼容旧 chat/workflow Task | 不重命名为 OpsTask |
| `/api/cloud-ops/*` | 委托或返回迁移提示 | `confirmed=true` 不可授权执行 |
| `/api/alerts/history` | 映射新 Alert 查询 | 不继续读进程内列表 |

## 14. 存储、租约与数据迁移实现细节

### 14.1 Repository 接口最小集合

```python
class TaskRepository(Protocol):
    async def create(self, tenant: TenantContext, state: OpsTaskState) -> OpsTaskState: ...
    async def get(self, tenant: TenantContext, task_id: str) -> OpsTaskState | None: ...
    async def load_for_update(self, tenant: TenantContext, task_id: str) -> OpsTaskState: ...
    async def save(self, tenant: TenantContext, state: OpsTaskState) -> None: ...
    async def lease_next(self, worker_id: str, now: datetime) -> OpsTaskState | None: ...

class EvidenceStore(Protocol):
    async def put(self, tenant: TenantContext, evidence: Evidence, content: bytes | None) -> Evidence: ...
    async def list_for_task(self, tenant: TenantContext, task_id: str) -> list[Evidence]: ...

class PlanRepository(Protocol):
    async def create_immutable(self, tenant: TenantContext, plan: OperationPlan) -> OperationPlan: ...
    async def get_for_execution(self, tenant: TenantContext, plan_id: str) -> OperationPlan: ...
```

Phase 1 Cache Adapter 与 Phase 4 PostgreSQL Adapter 实现同一接口。接口不接收全局 `tenant_id: str`，避免调用者绕开身份校验。

### 14.2 Lease 与乐观锁

Worker 领取条件：

```text
status in (queued, running)
and next_run_at <= now
and (lease_expires_at is null or lease_expires_at < now)
```

领取时写入 `lease_owner`、`lease_expires_at`、递增 `lease_generation`。保存 Checkpoint 时必须匹配 `state_version + lease_generation`；否则返回 `TASK_STALE_WORKER`。取消请求不强杀 SDK 调用，而是写事件和状态标记，Runner 在 Action 边界停止后续步骤。

### 14.3 Secret、Evidence 与保留

- Secret 只由 SecretStore 解析为任务时间最小权限凭证，使用后清除临时内存。
- Evidence 原始内容按 Tenant 保留策略保存；摘要、Hash 和引用可保留更久。
- Conversation、Profile、Knowledge、Skill、Task、Audit 使用各自 TTL/归档策略，不通过同一个 Cache TTL 管理。
- 删除使用 Tombstone 后异步清理关系数据、向量索引、对象内容和缓存；Legal Hold 阻止自动删除。

### 14.4 数据迁移顺序

```text
M1 environment/task/event/evidence schema
M2 plan/approval/tool effect schema
M3 conversation/knowledge metadata + secret/model config schema
M4 skill schema
M5 alert receipt/event/instance schema
M6 audit head/event + benchmark result schema
```

每个 Migration 必须包含升级验证、兼容窗口说明和回滚策略。发布不依赖 destructive down migration；回滚通过旧代码仍能读取新增列/表、Feature Flag 关闭新写入完成。

## 15. 配置、依赖与部署规格

### 15.1 新配置登记

| 配置 | 阶段 | 默认 | Production 规则 |
|---|---|---|---|
| `runtime.profile` | P0 | `demo` | `production` 启动检查严格门禁 |
| `ops.kubernetes.fallback_policy` | P0 | Demo `allow_mock` | LIVE 必须 `fail_closed` |
| `agent.execution_mode` | P0 | `legacy_react` | 灰度启用 `policy_workflow` |
| `database.url` | P4 | 空 | 发布必填 |
| `worker.lease_ttl_seconds` | P4 | 配置化 | 不小于单 tick 最大执行窗口 |
| `secret.master_key_ref` | P2/P4 | 空 | 本地加密模式必填 |
| `alert.integrations` | P4 | 禁用 | 绑定 Tenant/Environment/Scope |
| `ops.s3_enabled` | P3/P4 | false | 所有门禁通过后显式打开 |

环境变量仅覆盖明确登记字段。生产启动时不允许 CORS `*`、认证关闭、默认主密钥、缺失数据库、缺失审计/Secret 策略或 LIVE fallback 不合规。

### 15.2 依赖变更

运行依赖新增：`asyncpg`、`alembic`、`cryptography`。开发依赖新增/固定：`black`、`isort`、`mypy`、`pytest-cov`。保持运行镜像不安装质量工具。所有依赖锁定、扫描并记录升级策略。

### 15.3 Compose 与 Kubernetes

Compose 使用 PostgreSQL、Redis、API、Worker 和一次性 Migration。API/Worker 同镜像不同命令；生产禁止用单一容器的多 Uvicorn worker 承载进程内 Task 正确性。

Kubernetes 发布至少提供：API Deployment、Worker Deployment、Migration Job、Service、Ingress、Secret/Config 引用、ServiceAccount/RBAC、PDB、HPA、NetworkPolicy、PVC/对象存储配置和按环境覆盖的 values。K8s ServiceAccount 只授予 Environment Scope 所需的读权限；写 Capability 单独授予、默认不绑定。

## 16. 前端实现规格

### 16.1 状态与交互规则

- Task Detail API 是事实来源；SSE、缓存与路由只是视图加速。
- 每个 Task 页面显示 Environment Mode、数据来源、Task Phase、风险、预算和 Evidence 引用。
- 非 LIVE 页面持续水印；LIVE 失联显示 `evidence unavailable`，不可显示 Mock 成功。
- API 401/403/404/409/422/429/5xx 映射为可理解状态，不展示后端堆栈或 Secret。
- 弹窗支持焦点锁定、Esc、键盘操作；SSE 更新区域使用 `aria-live`。
- 卡片圆角不超过 8px；表格、时间线和分栏优先；不创建营销式卡片首页。

### 16.2 页面与 API 映射

| 页面 | 事实 API | 核心组件 | 允许动作 |
|---|---|---|---|
| 总览 | Environment/Task/Alert/Health | 状态摘要、最近任务、依赖健康 | 发起诊断/连接环境 |
| 智能运维 | OpsTask Detail/Event/Evidence/Report | Context Bar、TaskTimeline、Inspector | 创建、取消、补充输入、导出报告 |
| 告警记录 | Alert List/Detail/Events | 状态表、关联 Task、Evidence 引用 | 查看/重跑/创建后续任务 |
| 资源与连接 | Environment List/Test/Sync | 连接表、测试结果、Scope | 添加、测试、停用、轮换 |
| 审计 | Audit Query/Verify | 时间线、筛选、完整性状态 | 查看、导出 |
| 模型设置 | Model Config API | Provider 表、测试/轮换对话框 | 添加、测试、设默认、停用 |

Approval、Skills、Tools、Knowledge 与 Memory 页面只有对应 API/Scope/生命周期完成后加入导航。移动端以查看 Task/Alert/Approval 为主，复杂连接表单只在桌面端提供。

## 17. Benchmark、测试与 CI 实现规格

### 17.1 测试目录与 Marker

现有平铺测试逐步迁移，新测试从第一天进入分层目录：

```text
tests/
  fakes/
  fixtures/k8s/
  unit/{agent,context,tools,workflow,skills,security,api}/
  integration/{api,redis,persistence,k8s_live,prometheus_live,security}/
  concurrency/{task_leases,idempotency,sse,alerts}/
  benchmark/{contracts,scorers,regression}/
  load/
```

登记并强制使用：

```text
unit
integration
concurrency
benchmark
k8s_live
security
slow
```

共享 Fixture 只构造依赖与隔离资源；Fake LLM 返回有序结构化结果并记录调用参数。测试不能共享可变 Agent、Session、Task 或全局 Tenant。

### 17.2 四层测试门禁

| 层级 | 覆盖范围 | 外部依赖 | 触发时机 |
|---|---|---|---|
| Unit | Contract、Reducer、Risk、状态机、Fingerprint、Schema | Fake/Stub，无公网 | 本地与每个 PR |
| Integration | FastAPI、Repository、Redis/PG、鉴权、SSE、Provider 边界 | 受控真实依赖 | PR 基础集成/受保护环境 |
| Concurrency/Load | Lease、Idempotency、Approval、SSE、告警风暴、池饱和 | 真实 API 进程与依赖 | 定时/发布前/手动 |
| LIVE Benchmark | 真故障、Evidence Oracle、新旧配对、安全和成本 | 专用 kind/Staging + 模型 | Nightly/Release |

零容忍断言：跨 Tenant、未审批写、S5 执行、Secret 泄漏、LIVE 混入 Mock/Replay、非法 Skill 激活。质量和性能阈值从版本化 Baseline/Suite Policy 读取，不散落在测试代码。

### 17.3 专项测试清单

K8s/Prometheus：

- Input Schema、分页/截断、空结果、403/404/超时、Scope 和 Redaction。
- LIVE `fail_closed`；显式 MOCK 才能产生 Mock Evidence。
- 环境 Evidence 类型/来源/时间可追溯，Prometheus 缺失只能生成 Partial。
- S3 验证 Plan、Approval、Idempotency、Post-condition、Rollback 建议与 Audit。

推理/Context：

- 非法 JSON、未知 Action、缺参数、LLM 超时、重复 Action、预算耗尽与取消。
- available_actions 外的 Action 必须拒绝。
- 压缩保留目标、Scope、Evidence ID 和错误码；日志 Prompt Injection 无法改变策略。
- Decision Trace 不含 Thought/Secret/raw prompt。

Workflow/Worker：

- 每一种合法/非法状态转换、WAITING_INPUT、WAITING_APPROVAL、取消、恢复。
- 同时领取同一 Task 只有一个有效 Lease；过期 Worker 结果被拒绝。
- Tool 成功但 Checkpoint 未写入后，恢复路径验证 post-condition。
- SSE 重连不丢持久 Event，慢消费者不造成无界内存。

Skill：

- Draft/Evaluating/Review Pending 无法被生产召回。
- Tenant/Environment/Capability/Risk Filter 在语义检索之前执行。
- Benchmark 安全失败、Tool 不兼容或来源未脱敏时不能激活。
- Active Pointer 回滚原子，运行中 Task 固定版本。

Alert/Approval：

- 批量 Payload、认证、Canonical Fingerprint、重复/乱序 firing/resolved。
- Receipt 持久化失败返回非 2xx；持久化后 202；重复投递不重复 Task。
- 旧 Approval 因 Plan 变更、过期、拒绝或 Scope 变化而无法执行。

### 17.4 LIVE Benchmark 执行

`benchmarks/k8s-live/cases/*/case.yaml` 定义每个 Case 的 Manifest、稳定条件、Oracle、必需 Evidence、禁止副作用和预算。运行命令：

```text
python scripts/run_live_benchmark.py \
  --suite benchmarks/k8s-live/suites/core-readonly.yaml \
  --setup-context <controlled-context> \
  --agent-context <readonly-context> \
  --variants legacy_react,policy_workflow
```

Setup 与 Agent 使用不同最小权限身份。Runner 只允许显式登记的 Cluster Fingerprint、Namespace Prefix 和 Manifest 类型；`finally` 以 Owner Label 清理并重新查询确认。测试故障修复/清理由 Harness Setup/Cleanup 身份执行，Athena 全程只读。

Artifact：

```text
artifacts/benchmarks/{run_id}/
  run.json
  environment.json
  report.json
  report.md
  cases/{case_id}/{variant}/response.json
  cases/{case_id}/{variant}/trace.json
  cases/{case_id}/{variant}/score.json
```

报告输出 Root Cause、Required Evidence、Unsupported Claim、安全事件、Token/Tool/延迟分布、环境无效率和配对差值。LLM Judge 只能辅助表达质量，不能覆盖确定性 Oracle 或安全失败。

### 17.5 CI 矩阵

```text
PR / Push
  quality
  unit_api
  integration_redis

Phase 4 后 PR
  integration_postgres
  migration_upgrade

Scheduled / Release / Manual
  target_runtime_security
  live_k8s
  concurrency
  load
  benchmark
```

Artifact 包括 JUnit、Coverage、脱敏 Trace、负载指标、Benchmark JSON/Markdown、Manifest 与 Evidence Hash。必需 Job 的 `not_run/skipped` 不等于通过。Release Candidate 必须关联同一 Commit 的安全、LIVE 和 Benchmark 结果。

## 18. Alertmanager、学习与写操作专项流程

### 18.1 Alert 生命周期与降级

标准化 Event 至少含：

```text
receipt_id, integration_id, tenant_id, environment_id
source_fingerprint, canonical_fingerprint, fingerprint_version
source_status, ordering_status, alert_name, severity
resource_hints, labels, annotations, starts_at, ends_at
payload_hash, validation_status, error_code
```

`AlertReceipt` 与 `AlertEvent` 是不可变 Trigger Provenance；`AlertInstance` 是当前 firing/resolved 投影。处理状态在查询层由 Mapping、Task 和错误码组合，不复制 OpsTask 状态机。

降级矩阵：

| 故障 | Webhook | Worker/Task | 禁止 |
|---|---|---|---|
| PG 不可用 | 可重试非 2xx | 不创建内存 Receipt | 返回 202 |
| Worker 崩溃 | 已持久化仍 202 | Lease 到期接管 | 重复 Task |
| K8s LIVE 失联 | 已受理 | 重试后 Partial/Failed | Mock 或变更计划 |
| Prometheus 不可用 | 已受理 | K8s 只读诊断并标证据缺失 | 伪造指标 |
| LLM 不可用 | 已受理 | 已知场景 rules_only；未知升级 | 假装确认根因 |
| Audit 投影不可用 | Receipt 保留意图 | 按策略等待/只读 | 任何写操作 |

容量门禁只能在 T4 持久化前拒绝。Receipt/Event 一旦成功提交，入口必须返回 202，后续排队、退避、重试、限流和 Coalesce 均由 Worker 处理。

### 18.2 自进化闭环

```text
Capture Task/Action/Evidence/outcome
 -> Curate/redact/deduplicate
 -> mine repeated successful patterns
 -> create Skill Draft
 -> static validation
 -> offline replay + benchmark
 -> human review
 -> active pointer switch
 -> observe and rollback
```

Curator 是慢路径，失败不影响原 Task 结果。失败轨迹进入反例库，但不直接产生可执行 Skill。任何自动生成的 Script/Tool/权限/网络策略只可输出变更建议或 PR 草稿，不能注册运行时能力。

### 18.3 写操作终态与人工接管

执行失败时 Runner 记录：Plan、Approval、Tool Effect、资源版本、Post-condition、Evidence 和 Error Code。可回滚时创建后续受控 Plan；不可安全回滚时进入人工升级，而不是让模型尝试第二个未批准写操作。

用户可在 Task 页面查看风险、影响、Dry Run、验证、回滚建议和审计记录。人类批准的是精确 Plan Version，不是聊天文本或 Agent Confidence。

## 19. 灰度、回滚与最终验收

### 19.1 灰度顺序

```text
Unit/Integration
 -> local kind
 -> staging LIVE
 -> single-tenant canary
 -> default enable
```

每个新 Profile、Provider、Skill 和 Alert Integration 均有独立 Feature Flag/Active Pointer。Canary 记录质量、成本、延迟、拒绝率、审计完整性和安全事件，再扩大范围。

### 19.2 回滚规则

- `policy_workflow` 可以任务级回退到 `legacy_react`。
- 未完成前端入口可隐藏，但不删除已持久 Task/Evidence。
- Alert Integration 可停用，Worker 停止领取新 Alert 但保留 Receipt/Task 历史。
- S3 Flag 可关闭；已执行副作用不能通过删除数据库记录“回滚”。
- Skill 回滚原子切换 Active Version Pointer；运行中 Task 保持固定版本。
- 数据库发布只回滚应用读写路径，不依赖破坏性 schema 回滚。

Strict LIVE、Tenant 隔离、Plan Hash、Approval、Secret Redaction 和 S5 拒绝不允许作为回滚开关关闭。

### 19.3 最终验收矩阵

| 领域 | 必须证明的结果 | 证据 |
|---|---|---|
| Environment | 用户可配置并测试自身 K8s/Prometheus；Secret 不回显 | API/前端集成测试、脱敏检查 |
| 诊断 | 至少一类故障由 Evidence 驱动完成只读诊断 | controlled LIVE Benchmark 报告 |
| 可信度 | Finding/Conclusion 可追溯 Evidence；Thought 未持久化 | Task Detail、Trace、数据扫描 |
| 安全 | LIVE 失联不切 Mock；Scope/Tenant 不可绕过 | fail-closed、隔离与拒绝测试 |
| 写操作 | S3 绑定 Plan/Approval/Idempotency/Verify | 安全与并发集成测试 |
| 恢复 | API/Worker 重启后 Task 可恢复 | Lease/Checkpoint 故障注入测试 |
| Alert | 批量、认证、去重、firing/resolved 与关联 Task 可追溯 | durable Alert Integration + LIVE E2E |
| Skill | Draft 不可运行；Active/rollback 可复现 | 生命周期/Replay/Benchmark 测试 |
| 部署 | 新环境完成安装、迁移、巡检、升级 | Compose/Helm 演练与 Runbook |
| 发布 | 指标、审计、Benchmark、备份恢复均有 Artifact | Release Candidate Evidence |

## 20. 明确延期项

以下能力继续延期，只有满足 Proposal 第 25 节门槛后才重新评估：

```text
微服务拆分
Kafka / Temporal
Workflow DSL 与拖拽编辑器
插件市场与在线第三方代码
多 Agent 群体自治
在线 Script 编辑器
自动发布 Script/Tool/Skill
通用告警关联图谱
通用 CMDB
React/Vue 迁移
S4 生产自动化
任何 S5 相关能力（S5 永久拒绝）
```

## 21. 立即可领取的首批 PR

1. `P0-01`：统一 Python 3.12、开发依赖和基础 CI。
2. `P0-02`：Strict LIVE、实际来源标记与 Namespace 拒绝。
3. `P0-03`：核心契约与 legacy 脱敏结构化 Trace。
4. `P0-04`：PatternPolicy 与 `legacy_react|policy_workflow` Flag。
5. `P0-05`：LIVE Benchmark Case Loader/Oracle/Artifact 骨架。
6. `P0-06`：Alert Parser 批量化与当前 Demo 安全修复。

这六个 PR 完成并通过相应门禁后，才开始 P1 ToolRuntime、Evidence、ContextManager 和 CrashLoop OpsTask 主链。

## 22. 架构追溯矩阵

| 架构 Proposal/附录 | 实现任务 | 验收依据 |
|---|---|---|
| 第 3、6、7、8 节：职责域、状态机、Policy Agent | P0-03/04、P1-04/05 | Contract/Workflow 测试、Task Event、OpsTask API |
| 第 9、10 节：Context、Memory、Evidence | P1-02/03、P2-02、P4-02 | Reducer 回归、Evidence Ref、Tenant/Retention 测试 |
| 第 11、14、15 节：Tool、扩展、安全 | P1-01、P3-01/02/03、P5-01 | ToolRuntime、Plan/Approval、S0-S5 测试 |
| 第 12、13 节：Skill、Script 治理 | P5-02/03 | Replay、审核、版本固定、Script 边界测试 |
| 第 16、17 节：领域模型、恢复与扩容 | P4-01/02/03/04 | Migration、Lease、重启与多 Tenant 测试 |
| 第 18 节：可观测与降级 | P0-02/03、P1-07、P4-05 | Trace/Metric、Fallback、Ready 结果 |
| 第 19、20、21 节：产品、前端、API | P1-05/06、P2-01 至 P2-06、P3-04 | API Contract、SSE、前端集成测试 |
| 第 23、24、27 节：部署、阶段、发布 | P0-01、P4-01/04、P5-04/05 | Compose/Helm、Migration、Release Artifact |
| 附录 A：LIVE Benchmark | P0-05、P1-07、P5-05、17.4 | Case/Oracle/配对报告/清理验证 |
| 附录 B：工程测试体系 | P0-01、17.1 至 17.5 | CI Job、Marker、JUnit/Coverage/安全门禁 |
| 附录 C：Alertmanager 闭环 | P0-06、P1-08、P4-07、18.1 | 认证、Receipt、去重、firing/resolved LIVE E2E |
