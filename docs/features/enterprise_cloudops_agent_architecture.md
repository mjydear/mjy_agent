# Athena 企业级智能云运维 Agent 架构与产品演进方案

> 状态：Proposal v1.1  
> 日期：2026-07-11  
> 适用范围：Athena 当前代码库从 Agent Demo 演进为可发布、自托管的企业级云运维产品  
> 核心原则：任务优先、证据驱动、确定性治理、模型只做策略判断、安全默认拒绝、渐进演进不过度设计

v1.1 变更：纳入九条企业 Agent 规则，并修正为五个职责域、三个最小执行 Profile、S0-S5 风险模型、结构化 Decision Trace、生产 Fallback Matrix 和无状态扩容边界。

## 1. 执行摘要

Athena 的目标不再是“能够调用工具的聊天机器人”，而是：

> 面向企业 SRE、运维工程师和研发团队的智能云运维控制台。系统用确定性 Workflow 管理权限、上下文、执行、审批、验证和审计，用 Policy Agent 处理诊断假设、证据选择和下一步动作决策。

产品主对象应从 `Session / Chat` 转变为：

- `Environment`：用户授权管理的云环境、集群和数据源。
- `OpsTask`：一次可跟踪、可暂停、可恢复的运维任务。
- `Evidence`：日志、事件、指标和资源快照等诊断证据。
- `OperationPlan`：不可变、可审批、可验证的变更计划。
- `Approval`：绑定具体计划版本的一次授权。
- `AuditEvent`：不可抵赖的操作记录。

普通聊天可以保留，但它只是一种输入方式，不再承担整个产品的信息架构。

第一条生产级垂直链路选择：

```text
Kubernetes CrashLoopBackOff 只读诊断
```

选择它的原因：

1. 当前项目已经具备 K8s 只读工具、诊断器、报告和测试基础。
2. 只读场景可以先验证智能诊断，不把模型决策风险和生产写操作风险混在一起。
3. 故障包含资源状态、事件、日志和指标，足以验证 Context Manager 与证据模型。
4. 成功后可自然扩展到 `rollout restart` 和 `scale` 的审批执行闭环。

## 2. 重要事实校正

用户提供的案例描述了 Thought、Action、Iteration 三个独立节点造成九次节点流转的问题，但 Athena 当前实现并不是这种 DAG。

当前 `ReActAgent.run()` 每轮执行：

```text
组装 Prompt -> 一次 LLM 决策 -> 一次 Tool 调用 -> 追加 scratchpad
```

因此不能直接声称把“三节点合并为一节点”会得到 30% 优化。Athena 的真实问题是：

1. `scratchpad` 每轮追加完整 Thought、Action、Observation，并在后续轮次重复发送。
2. 对话 Memory 和任务 scratchpad 存在语义重叠，容易重复注入上下文。
3. 所有 Tool 描述每轮全量进入 Prompt，没有按任务能力筛选。
4. 大日志、大 JSON 和错误堆栈直接成为 Observation，缺少证据外置和结构化压缩。
5. `stream_run()` 当前是等待完整执行后回放步骤，不是真正逐步流式。
6. 云运维、聊天、Workflow 和 Benchmark 通过一个大型应用服务聚合，领域边界逐渐模糊。

所以本方案不照抄案例数字，而是先建立基线，再用实测数据证明优化效果。

## 3. 设计目标与非目标

### 3.1 设计目标

1. 对常见云运维故障形成证据驱动的诊断闭环。
2. 确定性逻辑不交给模型，模型只负责动态策略选择。
3. 新增 Provider、Tool 或 Workflow 时不修改核心执行循环。
4. 所有生产写操作必须经过计划、审批、幂等执行、验证和审计。
5. 支持单企业自托管，并为未来多租户托管保留正确的数据边界。
6. API 与 Worker 可重启，任务状态不能依赖某个 Web 进程内对象。
7. 前端围绕任务、证据、风险和结果组织，而不是围绕聊天气泡组织。
8. 能用当前模块渐进改造，不进行一次性重写。

### 3.2 明确非目标

首个可发布版本不做：

- 微服务拆分。
- 通用低代码 DAG 平台。
- 拖拽 Workflow 编辑器。
- 插件市场和在线安装第三方代码。
- 多 Agent 群体自治。
- 知识图谱。
- 全自动生产写操作。
- 通用 CMDB。
- 自定义 Dashboard Builder。
- Kafka、事件溯源或 Temporal。
- 每租户独立数据库。
- 为了“企业级”立刻迁移 React/Vue。
- 未经 Benchmark 证明的性能百分比。

这些能力只有在真实业务规模出现后再引入。

### 3.3 九条输入规则的采纳结论

| 规则 | 结论 | Athena 中的落地方式 |
|---|---|---|
| 分层解耦 | 采纳但修正 | 使用五个职责域；安全与可观测是横切平面，不是假装位于最底部的单层 |
| 可控自治 | 完全采纳 | 只读自动、写操作计划与审批、默认拒绝、全链路审计 |
| 可观测与可解释 | 采纳但拒绝完整 Thought | 保存 Evidence、Action、reason_code、输入引用和耗时，不保存隐藏推理文本 |
| 四层记忆 | 采纳为产品认知模型 | 内部仍分离 TaskState、Evidence、TenantConfig 和 Profile，避免一个万能 Memory Store |
| 容错降级 | 采纳但生产 Fail-Closed | LLM 可降级规则诊断；真实集群失联不得静默切 Mock |
| 可演进闭环 | 完全采纳 | 执行、评估、候选生成、回放、人工审核、激活与回滚 |
| 安全护栏 | 完全采纳 | 使用 S0-S5、Scope 白名单、沙箱、永久禁止动作 |
| 无状态扩容 | 完全采纳 | API/Policy 实例无状态，任务、会话、Evidence、Skill 和租约外置 |
| 范式组合 | 采纳“按需选择”，拒绝“全部必用” | Workflow 根据任务类型与风险选择最小执行策略，不把范式数量作为先进性指标 |

### 3.4 五个职责域与两个横切平面

“五层”可以作为逻辑视图，但层级必须保持正确依赖方向：

```text
1. Interface Adapters
   Web / API / CLI / Alert Webhook / Scheduler
                |
2. Application Orchestration
   OpsTaskService / WorkflowRunner / Approval
                |
3. Agent Strategy Runtime
   PatternPolicy / ContextManager / PolicyAgent
                |
4. Capability Runtime
   ToolRuntime / Capability Bundle / Provider Adapter / ScriptRunner
                |
5. State and Learning
   TaskState / Evidence / Conversation / Knowledge / Skill / Curator

横切平面 A：Security and Governance
横切平面 B：Observability and Evaluation
```

安全、审计和 Trace 必须覆盖每个职责域，不能只在调用链末端补一层。不允许上层反向依赖 Web、CLI 或具体云 SDK；Protocol 只用于 LLM、Repository、Provider Adapter、Event Sink 等真实变化点，不为每个内部类机械创建接口。

### 3.5 执行范式选择，而不是范式堆叠

用户列出的内容实际包含六类主要模式，并非必须同时运行的“九大范式”。Athena 使用确定性的 `PatternPolicy` 选择最小策略：

| 场景 | 默认策略 | 附加策略 |
|---|---|---|
| 普通知识问答 | Direct Answer / Knowledge Search | 无 |
| 简单只读查询 | Direct Tool Workflow | 无需 ReAct |
| 未知只读故障诊断 | Policy Loop | 有界 ReAct 式 Action 选择 |
| 多阶段长任务 | Plan-and-Execute | 每阶段仍由 Workflow 校验 |
| 相互独立的证据采集 | Bounded Parallel Collection | 仅并行只读 Tool，不并行写操作 |
| 证据冲突或验证失败 | Bounded Reflection | 最多有限次数，不是每轮反思 |
| Skill 优化 | Evaluator-Optimizer | 仅后台慢路径 |
| 高风险复杂变更 | Role-separated Plan/Execute/Validate | 首期是逻辑角色，不默认启动多个 LLM Agent |
| 普通聊天兼容 | Legacy ReAct | 不进入生产写操作链路 |

V1 只实现三个执行 Profile：

```text
direct_workflow       无模型或单次确定性 Tool 流程
bounded_policy_loop   有界策略决策循环
plan_execute          长任务的计划与分阶段执行
```

`parallel_collection`、`max_one_reflection` 和 `skill_guidance` 是受控 Modifier，不是新的执行引擎；Evaluator-Optimizer 只在离线慢路径运行；V1 不实现 Hierarchical Multi-Agent。

选择输入只包括：

```text
task_type
risk_level
required_capabilities
estimated_steps
evidence_fanout
latency/token budget
current confidence
```

约束：

- `PatternPolicy` 是传统代码策略，不让模型自由拼装执行架构。
- Reflection 只在低置信度、证据冲突、Tool 失败或结果验证失败时触发。
- 并行化只用于相互独立、幂等、只读的证据采集，并设置并发、超时和取消预算。
- Planner、Executor、Validator 首期是职责角色，不等于三个都调用 LLM。
- 只有 Benchmark 证明独立 Agent 角色带来稳定收益，才升级为 Hierarchical Multi-Agent。
- 前端不让普通用户选择 ReAct、Reflection 等内部范式，只在任务元数据中展示实际执行策略。

## 4. 参考项目的借鉴边界

本方案参考 Claude Code、OpenClaw、OpenManus 和 Hermes Agent 的公开设计，但不会复制其产品边界。

| 项目 | 值得借鉴 | Athena 不应照搬 |
|---|---|---|
| Claude Code | 紧凑工具循环、Plan 与执行分离、权限模式、按需加载上下文、Hooks/MCP 扩展、清晰工具状态 | 编码助手对本机文件的高自治权限；终端作为唯一产品形态 |
| OpenClaw | Gateway 控制面、本地优先、自托管、会话与 Workspace 隔离、Onboarding、Skills、默认安全策略 | 大量消息渠道、个人助理全能力、主会话直接拥有宿主机完整权限 |
| OpenManus | 显式 Agent State、BaseAgent/ToolCallAgent 分层、ToolCollection、Flow 与 Agent 分离 | “通用 Agent 解决所有任务”、不稳定的多 Agent Flow、宽泛工具权限 |
| Hermes Agent | 可插拔 Context Engine、Token 使用跟踪、压缩阈值、头尾保护、Tool Result 压缩、Skills 与长期记忆 | 自动生成后立即启用 Skill、高自治并行子 Agent、把通用个人记忆直接带入生产运维 |

### 4.1 从 Claude Code 借鉴

- 用户看到的是“计划、工具调用、结果、权限请求”，而不是完整 Chain-of-Thought。
- 只在需要时加载目录说明、工具和上下文，不把所有能力一次性塞入 Prompt。
- 计划阶段与执行阶段分开，执行权限可被明确限制。
- 扩展通过清晰契约进入，不在核心循环增加业务 `if/else`。

### 4.2 从 OpenClaw 借鉴

- Gateway 是控制面，不等于 Agent 本身。
- 首次使用必须有引导式配置，而不是把用户扔进一个空聊天框。
- 环境、会话和能力需要隔离。
- 自托管应是首个发布形态，先部署到客户自己的网络和集群内。

### 4.3 从 OpenManus 借鉴

- Agent 有显式状态和生命周期，而不是靠隐式字符串推动流程。
- Tool Collection 与 Flow 分离。
- Planner/Flow 可以组织确定性步骤，ToolCall Agent 只处理动态选择。

OpenManus README 将多 Agent 版本标为不稳定，因此 Athena 不把多 Agent 作为首期主线。

### 4.4 从 Hermes Agent 借鉴

- Context 管理应成为独立接口，而不是散落在 Agent Loop 中。
- 基于真实 Token Usage 决定压缩时机，粗估只用于调用前保护。
- 历史 Tool Result 应压缩为可用摘要，而非简单截断。
- 压缩摘要必须标记为历史参考，最新用户目标始终是当前真相。
- Skill 是可治理的程序性记忆，不是未经审核的生产代码。

### 4.5 参考资料

- Claude Code：https://github.com/anthropics/claude-code
- Claude Code Docs：https://code.claude.com/docs/en/overview
- OpenClaw：https://github.com/openclaw/openclaw
- OpenClaw Architecture：https://docs.openclaw.ai/concepts/architecture
- OpenManus：https://github.com/FoundationAgents/OpenManus
- Hermes Agent：https://github.com/NousResearch/hermes-agent

## 5. 当前架构评估

### 5.1 可复用资产

Athena 已经具备以下良好基础，不应推翻：

- `ReActAgent`：独立于 Web/API 的工具调用循环。
- `LLMClient` Protocol 与 LiteLLM 网关。
- Retry、熔断和 fallback。
- `ToolRegistry` 与统一 `ToolResult`。
- Working Memory、Profile、Long-term 和 Skill Memory。
- K8s 只读工具与诊断报告。
- Redis/InMemory Cache 抽象。
- Vector Store 与 Embedding 降级。
- API Key/JWT/RBAC、限流、幂等和审计。
- Prometheus、OpenTelemetry、Trace 和 Metrics。
- FastAPI + 原生 Web Console 的低部署成本。

### 5.2 必须解决的结构问题

#### A. 应用服务职责过多

当前 `AthenaWebService` 同时负责 Session、Chat、Workflow、CloudOps、Benchmark、Trace、Metrics、告警、知识库、Agent 生命周期和模型配置。

它应逐步拆成：

```text
ChatService
OpsTaskService
EnvironmentService
ModelConfigService
ApprovalService
AuditQueryService
```

现有 `AthenaWebService` 暂时保留为兼容门面，只做委托。

#### B. 组合根双中心

CLI 中的 `build_agent()` 被 API 反向依赖。应迁移到公共 bootstrap：

```text
athena/bootstrap/agent_factory.py
athena/bootstrap/application.py
```

CLI、API 和 Worker 都依赖 bootstrap，API 不再依赖 CLI。

#### C. 进程状态与持久化状态混合

Session 可以进入 Redis，但 Agent 实例和后台 Task 仍驻留进程内。多副本下，同一 Session 落到不同实例可能得到不同 WorkingMemory。

最终正确性必须来自持久化 `OpsTaskState`，进程内 Agent 只能是性能优化，不能是正确性的必要条件。

#### D. Cache 被当作数据库

Session、Task、Metrics、Audit 和 LLM Config 的持久化语义不同，不能长期只依赖同一个简单 KV 接口。

发布版应明确：

- PostgreSQL：租户、连接、任务、计划、审批、审计索引和配置元数据。
- Redis：缓存、限流、幂等、短期锁和事件通知。
- Secret Store：LLM 和云凭证密文。
- 对象存储或文件存储：大型证据和报告附件。

## 6. 目标架构

```text
Web / CLI / Webhook / Scheduler
              |
              v
        Request Gateway
 Auth | Tenant | Rate Limit | Idempotency | Trace
              |
              v
        OpsTaskService
 创建任务 | 查询状态 | 取消 | 补充信息 | 发起审批
              |
              v
        WorkflowRunner
 Prepare -> Decide -> Execute -> Reduce -> Complete
              |
              v
         PatternPolicy
 Direct | Bounded Policy | Plan-Execute
 Modifiers: Parallel Read | Max-1 Reflect | Skill
              |
              +----------------------+
              |                      |
              v                      v
       ContextManager          Governance Pipeline
  最小决策上下文            AuthZ | Risk | Audit | Budget
              |                      |
              v                      |
         PolicyAgent                 |
       输出一个结构化 Action          |
              |                      |
              +----------+-----------+
                         v
                    ToolRuntime
 Schema | Timeout | Retry | Execute | Normalize | Verify
                         |
                         v
                 Capability Bundles
 Kubernetes | Prometheus | Logs | Cloud | Knowledge
                         |
                         v
                  Provider Adapters
```

### 6.1 核心职责

#### Request Gateway

- 解析身份和 `TenantContext`。
- 请求限流和幂等。
- 创建 Trace ID。
- 不处理领域业务。

#### OpsTaskService

- 创建和读取任务。
- 校验任务所有权。
- 接收取消、补充信息和审批操作。
- 不直接调用云 SDK。

#### WorkflowRunner

- 驱动确定性状态机。
- 负责预算、重试、审批等待和失败恢复。
- 决定何时调用 Policy Agent。
- 决定何时完成、失败或请求人工输入。

#### PatternPolicy

- 根据任务类型、风险、能力和预算选择最小执行策略。
- 决定是否允许 Plan、有限 Reflection 或只读并行采集。
- 不处理具体 Tool 参数，不动态生成代码。
- 选择结果写入 TaskState，保证任务可复现。

#### ContextManager

- 从任务状态、Evidence 和历史 Action 构造本轮最小上下文。
- 管理 Token 预算和压缩。
- 选择本轮可见的 Tool 子集。
- 不执行 Tool，不决定权限。

#### PolicyAgent

- 根据当前状态选择一个 Action。
- 输出结构化结果。
- 不直接执行动作。
- 不决定用户权限和生产风险策略。

#### ToolRuntime

- Tool 查找和输入 Schema 校验。
- Capability 与 RBAC 校验。
- 风险策略检查。
- 超时、重试和熔断。
- Tool 执行和输出标准化。
- Evidence 持久化、结果脱敏和审计。

## 7. Workflow 状态机

### 7.1 最小内部状态

```text
PREPARING
   |
   v
DECIDING <----------------------+
   |                            |
   v                            |
EXECUTING -> REDUCING -----------+
   |             |
   |             +-> COMPLETED
   |             +-> FAILED
   |
   +-> WAITING_INPUT
   +-> WAITING_APPROVAL
   +-> CANCELLED
```

不要把每个内部细节都暴露为产品状态。对外只需要：

```text
status: queued | running | waiting | succeeded | failed | cancelled
phase:  validate | collect | analyze | plan | approve | execute | verify | report
```

### 7.2 每轮算法

```python
async def tick(task_id: str) -> TaskState:
    state = repository.load_for_update(task_id)
    workflow.validate_transition(state)
    context = context_manager.build(state)
    decision = await policy_agent.decide(context)
    result = await tool_runtime.invoke(state, decision)
    next_state = workflow.reduce(state, decision, result)
    repository.save(next_state)
    event_bus.publish(next_state.last_event)
    return next_state
```

核心约束：

- 一次 `tick` 最多产生一次模型决策和一次 Action。
- Workflow 决定能否继续，不依赖模型输出特殊退出字符串。
- 所有状态转换可持久化、可审计、可恢复。
- 前端断开不取消后台任务。

## 8. Policy Agent 契约

### 8.1 决策输出

```json
{
  "action": "k8s.logs.read",
  "arguments": {
    "environment_id": "env_prod_shanghai",
    "namespace": "payment",
    "pod": "payment-api-7d9",
    "tail_lines": 300
  },
  "reason_code": "CRASHLOOP_LOG_EVIDENCE_REQUIRED",
  "confidence": 0.91
}
```

不得持久化或向普通用户展示完整 Thought。可观测信息只保留：

- `reason_code`
- 决策摘要
- 选择的 Action
- 输入参数脱敏摘要
- Confidence，仅作为诊断信号，不作为授权依据
- 模型、Token、耗时和重试次数

### 8.2 系统 Action

模型可主动选择的系统行为也统一成 Action：

- `knowledge.search`
- `evidence.load`
- `analysis.deepen`
- `workflow.ask_user`
- `workflow.request_approval`
- `workflow.finish`
- `workflow.escalate`

但以下能力绝不能成为可绕过的 Tool：

- 身份认证
- 权限校验
- 参数 Schema 校验
- 风险阻断
- Secret 脱敏
- 审计
- Token 压缩
- 状态迁移合法性校验

更准确的原则不是“万物皆 Tool”，而是：

> 所有模型可选择的主动行为统一为 Action；所有必须执行的治理逻辑固化在 Workflow 和 ToolRuntime 中。

## 9. Context Manager V1

### 9.1 任务上下文模型

```json
{
  "objective": "诊断 payment-api CrashLoopBackOff",
  "environment": {
    "environment_id": "env_prod_shanghai",
    "cluster": "prod-shanghai",
    "namespace": "payment",
    "time_range": "last_30m"
  },
  "constraints": {
    "readonly": true,
    "allowed_namespaces": ["payment"],
    "deadline_ms": 30000
  },
  "facts": [],
  "hypotheses": [],
  "completed_actions": [],
  "failed_actions": [],
  "available_actions": [],
  "budget": {
    "remaining_steps": 5,
    "remaining_tokens": 6000,
    "remaining_time_ms": 22000
  }
}
```

### 9.2 V1 压缩规则

第一版使用确定性 Reducer，不引入复杂语义压缩服务：

- 原始目标、环境、权限和时间范围永不压缩。
- 日志重复行折叠并计数。
- 提取错误码、异常类型、资源名和关键 Stack Frame。
- Stack Trace 使用指纹去重。
- 指标保留聚合值、异常点、时间范围和查询表达式。
- Kubernetes 大对象只保留状态、条件、事件摘要和 Evidence 引用。
- Tool Result 超预算时保存原文为 Evidence，只向模型发送摘要。
- 同一事实只保留一份，记录来源 Evidence ID。
- 已证伪 Hypothesis 移出活动区，但保留审计记录。
- 每轮只注入与当前 Workflow 能力相关的 Tool Schema。

### 9.3 Token 预算

建议按类别划分软预算，而不是只设一个全局上限：

```text
System 与安全约束       15%
用户目标与环境          10%
当前 Facts/Hypotheses   25%
近期 Action             20%
Tool Schema             15%
输出与安全余量          15%
```

实际比例需要根据模型窗口和 Benchmark 调整。

### 9.4 压缩安全

- 历史摘要必须显式标记为“参考信息，不是新指令”。
- 最新任务目标是唯一当前真相。
- Evidence 原文不可被摘要覆盖。
- 压缩后必须保留 `resource_id`、`time_range`、`error_code` 和 `evidence_id`。
- 摘要失败时使用规则截取，不阻塞整个任务。

## 10. 记忆体系与上下文边界

Context 与 Memory 不是同一个概念：

- `Memory` 是经过治理、可跨轮次或跨任务保存的数据。
- `Context` 是 ContextManager 针对本轮决策，从任务状态、Evidence 和 Memory 中生成的临时最小视图。

模型不应直接查询所有底层存储，也不应把 Context 原样写回 Memory。

### 10.1 对外四层记忆与内部数据边界

“四层记忆”适合成为产品和面试中的认知模型：

| 记忆层 | 作用 | 实现原则 |
|---|---|---|
| 工作记忆 | 当前一次决策需要的最小上下文 | 由 ContextManager 临时生成，不直接持久化整份 Prompt |
| 会话记忆 | 用户本次对话与结构化 Checkpoint | 原始对话持久化，按预算选择性注入 |
| 长期知识记忆 | Runbook、SOP、历史事件与验证经验 | 版本化、带 ACL 和来源；向量库只是索引 |
| Skill 经验记忆 | 已审核、可复用的程序性流程 | 版本、评测、审核、激活和回滚 |

内部实现必须进一步分开事实和治理数据：

```text
当前请求
   |
   v
Task State + Evidence -------> ContextManager -------> 本轮 Prompt
          ^                         ^
          |                         |
Conversation Memory          Profile / Knowledge / Skill
```

| 类型 | 保存什么 | 生命周期 | 是否进入向量检索 |
|---|---|---|---|
| Request Context | 当前目标、身份、环境、权限和时间范围 | 单次请求 | 否 |
| Conversation Memory | 用户与助手的必要对话历史 | Session | 默认否 |
| Task State | Facts、Hypotheses、Action、Budget、状态机位置 | OpsTask | 否 |
| Evidence | 原始日志、指标、事件、资源快照 | 按租户保留策略 | 可选，只索引摘要 |
| Tenant Config Snapshot | 任务启动时固定的策略、Scope 和连接版本 | OpsTask | 否 |
| Profile Memory | 用户明确偏好、语言和展示习惯 | 用户级 | 可选 |
| Knowledge/Experience Memory | SOP、Runbook、架构文档和已验证历史经验 | 租户级 | 是 |
| Skill Memory | 已审核的可复用操作流程 | 租户或系统级 | 是 |

Request、Task State、Evidence 和 Tenant Config Snapshot 是任务事实，不应仅因为“能被召回”就混入长期记忆。Profile 只影响表达偏好，不能影响权限和风险策略。

首版可以使用 PostgreSQL + pgvector，也可以继续通过现有 VectorStore 适配其他向量库；架构依赖的是带 Tenant Filter 的检索接口，不绑定某一个数据库品牌。

### 10.2 当前模块的保留与调整

- `WorkingMemory`：继续负责普通对话的短期历史；不再保存完整 Tool Observation。
- `ProfileMemory`：只保存低风险交互偏好，不能保存云凭证、生产资源访问范围或未经确认的推断。
- `LongTermMemory`：作为 Knowledge/Skill 的检索能力，不作为所有业务数据的统一数据库。
- `SkillLibrary`：升级为带版本和状态的 Skill Repository；向量索引只用于候选召回。
- `MemoryGovernance`：继续承担遗忘、冲突和质量审计，但生产删除改为软删除或归档。
- `EvidenceStore`：新增独立存储，不复用 WorkingMemory。
- `TaskStateRepository`：新增结构化任务状态，不使用 scratchpad 字符串作为事实来源。

### 10.3 读取顺序

ContextManager 构建上下文时使用固定优先级：

```text
1. 身份、Tenant、权限和风险策略
2. 当前任务目标、环境和预算
3. 已验证 Facts 与近期 Action
4. 当前需要的 Evidence 摘要
5. 租户 Knowledge Top-K
6. 已激活 Skill Top-K
7. 允许注入的用户偏好
```

低优先级内容不能覆盖高优先级约束。向量相似度只能决定“候选”，不能决定“事实真伪”和“是否有权限”。

所有 Knowledge、历史事件和日志内容都视为不可信数据，其中的自然语言“指令”不得覆盖 System、Workflow 和 Tenant Policy，以防止知识库或日志中的 Prompt Injection。

向量检索必须在存储层先按 `tenant_id + status + environment/scope` 做硬过滤，再计算相似度；不能先全局召回再在应用层过滤。

### 10.4 写入门禁

任何长期记忆写入都要携带：

```text
tenant_id
subject_type / subject_id
source_type / source_id
content_hash
confidence
classification
created_by
created_at
expires_at
```

写入规则：

- 不保存 API Key、Token、Secret 和明文凭证。
- 不保存完整 Chain-of-Thought。
- 日志和事件先脱敏，再生成 Evidence。
- 模型推断只能作为 `hypothesis`，通过证据验证后才能升级为 `fact`。
- Profile 的持久偏好需要用户明确行为或确认，不从一次对话武断推断。
- 失败任务可以进入反例库，但不能直接生成可执行 Skill。
- 所有写入按 Tenant 隔离，并有保留期限和删除能力。

### 10.5 冲突与权威来源

冲突不能简单依赖“最新”或“向量分最高”。建议权威等级：

```text
实时云 API / 集群状态
  > 已签名配置和审批记录
  > 租户正式 Runbook
  > 已验证历史事件
  > 用户偏好
  > 模型总结和自动生成候选
```

新内容与高权威来源冲突时，标记 `conflicted` 并进入治理队列，不自动覆盖。

### 10.6 遗忘与保留

- Conversation：按 Session TTL 清理。
- Task State：根据审计要求长期保留或归档。
- 原始 Evidence：短期保存，摘要和哈希可以更长保留。
- Profile：用户可查看、修改和删除。
- Knowledge：版本化更新，旧版本标记 superseded。
- Skill：不能物理覆盖；创建新版本，旧版本可回滚。
- 被安全事件关联的记录进入 Legal Hold，不参与自动删除。

### 10.7 Context 与 Memory 的反馈闭环

```text
执行产生 Trace/Evidence
 -> Curator 脱敏与归一化
 -> 提取候选 Fact / Knowledge / Skill
 -> 质量与权限门禁
 -> 写入相应 Repository
 -> 后续 ContextManager 按需召回
```

Curator 是慢路径，不能阻塞用户请求。写入失败也不能改变原任务结果。

长期 Experience 的最小治理字段为：

```text
id, tenant_id, kind, scope, subject, summary
source_task_id, evidence_ids, confidence, status
importance, utility_score, last_used_at
content_hash, version, sensitivity, expires_at
```

一次召回不会提高 Utility；只有任务验证成功或人工采纳后才更新。删除采用 Tombstone 后异步物理删除，并同时清理关系数据、向量索引、对象存储和缓存。

## 11. Tool V2 与 ToolRuntime

### 11.1 兼容演进

保留当前 `ToolRegistry`，增加 V2 元数据和适配器，不一次重写所有 Tool。

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    domain: str
    description: str
    input_schema: dict
    output_schema: dict
    required_capabilities: tuple[str, ...]
    risk_level: str
    readonly: bool
    idempotent: bool
    timeout_seconds: float
    provider_types: tuple[str, ...]
```

```python
@dataclass(frozen=True)
class ToolCallV2:
    call_id: str
    task_id: str
    tenant_id: str
    tool_name: str
    arguments: dict
```

```python
@dataclass(frozen=True)
class ToolResultV2:
    status: str
    summary: str
    data: dict | list | None
    evidence_refs: tuple[str, ...]
    error_code: str | None
    retryable: bool
```

### 11.2 ToolRuntime 管线

```text
Resolve Tool
 -> Validate Schema
 -> Check Tenant Scope
 -> Check Capability/RBAC
 -> Evaluate Risk Policy
 -> Apply Timeout/Retry
 -> Execute Adapter
 -> Normalize Result
 -> Persist Evidence
 -> Redact Secrets
 -> Append Audit Event
```

任何 Tool 都不能绕开这条管线直接被 Policy Agent 调用。

### 11.3 命名规范

推荐使用稳定的领域命名：

```text
k8s.workload.get
k8s.pod.list
k8s.events.list
k8s.logs.read
k8s.rollout.restart
k8s.workload.scale
metrics.promql.query
logs.search
knowledge.search
workflow.finish
workflow.request_approval
```

Tool 名称不包含具体云厂商，Provider 差异由 Adapter 处理。

## 12. Skill 与自进化治理

### 12.1 当前自进化能力的真实成熟度

Athena 已有实验性 GEPA 骨架：

```text
TraceEvent
 -> ComplexityEvaluator
 -> SkillGenerator
 -> SkillValidator
 -> SkillLibrary
```

但当前还不是生产闭环：

- 主执行链尚未完整发布结构化学习事件。
- `CuratorDaemon` 默认任务只裁剪事件，没有实际生成 Memory 或 Skill。
- 复杂度主要由步骤、工具数量和外部难度计算，不代表任务成功或经验有价值。
- `SkillGenerator` 可以接收失败轨迹，缺少生产级候选准入门禁。
- `SkillValidator` 当前验证的是固定沙箱语句和文本结构，没有真实回放 Skill。
- `SkillLibrary` 是进程内字典与向量索引，没有 Tenant、版本、状态、审批和回滚。
- 示例代码存在生成后直接加入 SkillLibrary 的路径，只适合 Demo。

因此文档和产品中应把它称为“自进化实验模块”，直到完成下述治理闭环。

### 12.2 Skill 的定义

Skill 是经过审核的程序性知识：描述某类任务何时适用、需要哪些能力、建议采用哪些步骤以及如何验证。

Skill 不是：

- Tool 的替代品。
- 任意 Shell/Python 代码。
- Workflow 的权限规则。
- 一次成功轨迹的原样复制。
- 获得更高权限的通道。

运行时关系：

```text
Workflow 规定硬边界
Skill 提供可复用过程建议
PolicyAgent 选择下一 Action
ToolRuntime 执行受治理 Tool
ScriptRunner 仅执行 Tool 内部已批准脚本
```

Skill 只能影响“建议怎么做”，不能改变“允许做什么”。

### 12.3 Skill Manifest

```yaml
id: k8s-crashloop-diagnosis
version: 1.2.0
tenant_scope: tenant-a
status: active
description: Diagnose Kubernetes CrashLoopBackOff using read-only evidence
triggers:
  - kubernetes.crashloopbackoff
required_capabilities:
  - k8s.workload.read
  - k8s.events.read
  - k8s.logs.read
risk_ceiling: S1
input_schema: {}
output_schema: {}
procedure: []
validation_rules: []
source_runs: []
benchmark_suite: k8s-crashloop-v1
content_hash: sha256:...
owner: sre-platform
```

Manifest 进入关系数据库，Skill 正文作为版本化内容保存；向量库只保存可检索摘要和 Skill ID。

### 12.4 生命周期

```text
DRAFT
  -> EVALUATING
  -> REVIEW_PENDING
  -> ACTIVE
  -> ROLLED_BACK

EVALUATING / REVIEW_PENDING
  -> REJECTED
```

状态含义：

- `DRAFT`：自动生成或人工创建，不能进入生产决策上下文。
- `EVALUATING`：正在离线回放和安全检查。
- `REVIEW_PENDING`：达到机器门槛，等待 Owner 审核。
- `ACTIVE`：可被指定 Tenant/Environment 召回。
- `ROLLED_BACK`：因回归或风险撤下，保留历史复现和审计。
- `REJECTED`：未通过评测或人工审核。

任何状态都不允许原地覆盖内容。修改产生新版本。

首版不实现独立 Shadow 状态。需要线上对比时，以不影响 Action 的观测开关运行；只有出现稳定流量和自动灰度需求后，再扩展完整发布状态机。

### 12.5 自进化闭环

```text
1. Capture
   采集脱敏后的 Task、Action、Evidence、结果和人工反馈

2. Curate
   去除失败噪声、重复尝试、Secret 和租户特有敏感信息

3. Mine
   从多个相似成功任务提取候选模式，而非只看一次轨迹

4. Generate Draft
   生成结构化 Skill Manifest 与 Procedure

5. Static Validate
   校验 Schema、Capability、风险上限和禁止动作

6. Offline Benchmark
   在固定用例上与基线比较准确率、成本和安全性

7. Human Review
   Owner 审核适用范围、步骤、证据和风险

8. Activate
   按 Tenant/Environment 灰度启用

9. Observe and Roll Back
    持续比较质量；越过退化阈值立即回退旧版本
```

### 12.6 候选准入条件

只有同时满足以下条件，轨迹才可以生成候选 Skill：

- 任务最终成功，且结果通过确定性验证或人工采纳。
- 没有权限拒绝、安全违规或未审批写操作。
- 关键结论有 Evidence 支撑。
- 不是依赖偶然网络状态或临时资源 ID 的一次性流程。
- 工具失败率和重试次数没有超过门槛。
- 至少多个相似成功样本，或由人工显式发起提炼。

复杂度高只说明“值得分析”，不能说明“值得学习”。失败轨迹进入反例库，用于避免重复错误，不生成可执行 Skill。

### 12.7 Benchmark 与发布门禁

Skill 发布需要比较：

```text
root-cause accuracy
required evidence recall
invalid action rate
tool call count
token usage
latency
security violations
human acceptance rate
```

最低门禁建议：

- 安全违规必须为 0。
- 禁止动作调用必须为 0。
- 准确率不得低于当前 Active 版本。
- 成本或延迟不能越过租户配置上限。
- 固定回归集全部通过。

具体百分比由真实基线确定，不在架构里写死。

### 12.8 在线召回与使用

```text
Tenant/Status/Environment/Capability 硬过滤
 -> 语义 Top-K 召回
 -> Freshness/Utility/Confidence 重排
 -> Policy 兼容检查
 -> 最多加载少量 Skill 摘要
 -> 命中后按需加载完整正文
```

Agent 使用一次 Skill 不代表 Skill 正确。只有任务验证成功或人工采纳后，才提高 `utility_score`。

### 12.9 可进化与不可自动进化的边界

允许自动形成候选：

- Skill Procedure。
- Knowledge/Experience 摘要。
- Context Reducer 规则建议。
- Tool 选择顺序建议。
- 模型路由策略建议。

不允许自动发布：

- Tool 实现代码。
- Shell/Python Script。
- Workflow 权限与风险规则。
- RBAC、Namespace Scope 和审批策略。
- Secret、网络和沙箱策略。
- 生产写操作能力。

这些内容可以生成 Pull Request 或变更建议，但必须经过代码评审、测试和正式发布。

### 12.10 Skill 管理界面

在后端生命周期完成后，增加 `设置 > Agent 能力 > Skills`：

列表字段：

```text
名称 | 版本 | Scope | 状态 | 风险上限 | Benchmark | 最近使用 | Owner
```

操作按钮按状态显示：

- Draft：`查看来源`、`运行评测`、`删除候选`
- Review Pending：`审核`、`拒绝`
- Active：`停用`、`回滚`
- Rolled Back：`查看历史`、`恢复指定版本`

普通运维用户只能查看任务实际使用了哪个 Skill；管理员和 Skill Owner 才能管理生命周期。

### 12.11 最小持久化与测试

首版只需要两张表，不单独拆 Skill 微服务：

```text
skill_definitions
  id, tenant_id, key, name, active_version_id

skill_versions
  id, skill_id, version, status, spec_json, checksum
  source_task_ids, benchmark_report_json
  created_by, approved_by, created_at, activated_at
```

任务开始时固定 `skill_version_id`，运行中不随新版本激活或回滚而漂移。回滚只原子切换 `active_version_id`，并清理召回缓存。

必须覆盖：

- Draft 不可被运行时召回。
- Tenant 之间不可见。
- 非法状态转换被拒绝。
- Benchmark 安全失败时不能提交审核。
- Tool 版本或 Capability 不兼容时不能激活。
- 运行中任务使用固定版本。
- 回滚原子切换并留下审计。
- 来源轨迹已经脱敏。

## 13. Script 执行模型

### 13.1 Tool、Skill、Workflow 与 Script 的边界

| 概念 | 职责 | 模型能否直接创建并执行 |
|---|---|---|
| Workflow | 确定性状态、权限、审批和验证流程 | 否 |
| Skill | 可复用过程建议和经验 | 只能创建 Draft |
| Tool | Agent 可选择的稳定能力 API | 否，只能调用已注册 Tool |
| Script | Tool 内部受控的实现制品 | 否 |

Agent 永远不能把生成的一段代码直接交给生产 Shell。

### 13.2 两类脚本目录

```text
scripts/
  开发、压测、索引和部署辅助脚本
  永远不注册为 Agent Tool

athena/tools/runtime_scripts/
  随版本发布、经过审核的运行时脚本包
  只能由固定 Tool Adapter 通过 ScriptRunner 调用
```

当前 `scripts/load_test.py` 和 `scripts/index_benchmark.py` 属于开发脚本，不能进入 Agent Action 空间。

### 13.3 Script Package

每个运行时 Script 具有不可变清单：

```text
script_id
version
entrypoint
interpreter
argument_schema
output_schema
risk_level
required_capabilities
allowed_network_hosts
timeout_seconds
cpu_limit
memory_limit
max_output_bytes
content_hash
signature
owner
```

Tool 只能引用已注册的 `script_id + version`，不能传入任意命令字符串或替换 entrypoint。

### 13.4 ScriptRunner

```text
Resolve signed package
 -> verify hash/signature
 -> validate typed arguments
 -> check Tenant/Tool/Plan permission
 -> create ephemeral workspace
 -> inject minimum scoped credentials
 -> execute without shell parsing
 -> enforce CPU/Memory/Time/Output/Network limits
 -> collect structured result and artifacts
 -> redact and audit
 -> destroy workspace
```

生产实现要求：

- 默认使用参数数组执行，不使用 `create_subprocess_shell` 拼接字符串。
- 非 root 用户运行。
- 根文件系统只读，临时目录有容量限制。
- 默认禁止网络，只开放清单中声明的目标。
- 环境变量使用 allowlist，凭证按任务临时注入。
- 输出必须有上限，大结果进入 Evidence Store。
- S3/S4 Script 仍需 OperationPlan 和 Approval。

当前 `SecuritySandbox` 可以继续承担本地测试和受限 Python 验证；生产云运维 Script 建议使用独立容器或 Kubernetes Job 作为隔离边界。首期只需实现同步容器 Runner，不建设通用远程执行平台。

当前基于命令字符串、正则黑名单和可执行文件 allowlist 的 Shell 校验只能作为纵深防御，不能作为生产安全边界；生产 Runner 不接收模型生成的命令字符串。

### 13.5 ScriptResult

```json
{
  "status": "succeeded",
  "summary": "deployment state collected",
  "data": {},
  "artifacts": ["evidence://task-123/workload.json"],
  "exit_code": 0,
  "error_code": null,
  "retryable": false,
  "duration_ms": 842
}
```

Script stdout 不是直接进入 Prompt 的 Observation。Tool Adapter 将 ScriptResult 转换成结构化 `ToolResultV2`。

### 13.6 供应链治理

- Script 与应用镜像一起构建和扫描。
- 使用锁定依赖和可复现构建。
- 清单、内容哈希和签名进入发布制品。
- 启动或调用前验证哈希。
- Script 版本与 Tool 版本绑定。
- 审计记录具体 Script 版本和哈希。
- 安全漏洞可以按版本快速 Revocation。

V1 不允许用户上传任意 Script，不提供在线脚本编辑器。真实客户需求出现后，再设计受限的租户脚本仓库和签名发布流程。

### 13.7 自进化与 Script 的关系

自进化系统可以建议：

- 某个现有 Script 的参数模板。
- 新 Script 的 Draft 代码或 Pull Request。
- Script 的失败模式和测试用例。

但不能：

- 自动签名。
- 自动加入运行时注册表。
- 自动扩大网络、文件或云权限。
- 自动在生产环境执行。

## 14. 开闭原则与能力扩展

### 14.1 Capability Bundle

第一版使用静态 Python 注册，不做动态插件市场：

```python
class CapabilityBundle(Protocol):
    name: str

    def register(self, registry: ToolRegistry) -> None:
        ...
```

示例：

```text
KubernetesReadBundle
KubernetesChangeBundle
PrometheusBundle
LogSearchBundle
KnowledgeBundle
```

Workflow 依赖 Capability：

```text
k8s.workload.read
k8s.logs.read
metrics.query
```

而不是依赖 `AliyunClient`、`TencentClient` 或某个 SDK 类。

### 14.2 新 Provider 接入

接入一个新 Provider 只需要：

1. 实现 Connection Validator。
2. 实现所声明 Capability 的 Tool Adapter。
3. 注册 Capability Bundle。
4. 增加契约测试。
5. 在 Environment 配置页暴露相应字段。

核心 Workflow 和 Policy Agent Loop 不修改。

### 14.3 Workflow 形式

首期 Workflow 使用类型化 Python 类：

```python
class CrashLoopDiagnosisWorkflow(OpsWorkflow):
    required_capabilities = (
        "k8s.workload.read",
        "k8s.logs.read",
    )
```

暂时不设计 YAML DSL。只有同时满足以下条件才考虑声明式 DSL：

- 已经有至少 10 个稳定 Workflow。
- 多个 Workflow 出现明确、重复的结构。
- 非研发用户确实需要编辑。
- 版本、校验、迁移和回滚需求已经清楚。

## 15. 云运维安全模型

### 15.1 风险等级

| 等级 | 示例 | 默认策略 |
|---|---|---|
| S0 | 本地元数据、帮助文档、无环境访问的知识查询 | 自动执行 |
| S1 | 指定 Scope 内查询 Pod、事件、日志和指标 | 自动执行并审计 |
| S2 | 集群级宽查询、成本较高的采集、Dry Run 和变更建议 | 限制预算；只生成计划 |
| S3 | Scope 明确、可验证、可回滚的 Restart 和 Scale | 不可变计划 + 一次有效审批 |
| S4 | 生产配置修改、跨资源变更、影响面较大的 Rollback | V1 默认关闭；未来加强审批 |
| S5 | 删除 Namespace/PVC、修改 RBAC/Secret、批量破坏操作 | 永久硬拦截，不允许策略覆盖 |

V1 只开放 S0-S3。风险等级由 ToolSpec 与服务端策略共同确定，模型不能降低等级，租户策略只能收紧不能放宽系统硬限制。

有效风险不是 Tool 上的单一静态字段，而是服务端纯函数计算：

```text
effective_risk = max(
  tool_base_risk,
  argument_scope_risk,
  resource_count_risk,
  environment_sensitivity,
  blast_radius_risk
)
```

V1 将该计算放在 ToolRuntime 中，不引入通用策略引擎。

### 15.2 不可变 OperationPlan

当前 `confirmed: true` 不能证明用户批准的是同一份计划。发布前必须改为：

```text
Generate immutable OperationPlan
 -> calculate plan_hash
 -> persist plan_id + plan_hash
 -> Approval binds tenant/user/plan_hash/expiry
 -> execute(plan_id)
 -> re-check permission + approval + hash
 -> execute idempotently
 -> verify post-condition
 -> rollback or escalate on failure
```

计划内容至少包含：

```text
target environment
resource identity
operation and exact arguments
risk level
impact scope
preconditions
dry-run result
verification rule
rollback plan
expiry
```

### 15.3 默认拒绝

- 没有 Environment 授权则拒绝。
- 没有 Namespace Scope 则拒绝。
- Tool 未声明风险等级则拒绝执行。
- 写操作没有 OperationPlan 则拒绝。
- S3/S4 没有匹配计划哈希的有效 Approval 则拒绝。
- S4 在 V1 默认拒绝。
- S5 永久拒绝。
- 计划过期或资源版本变化则要求重新生成和审批。

### 15.4 Environment Mode 与真实隔离边界

```text
LIVE    连接真实环境，Evidence 来源必须为 live
REPLAY  使用固定历史证据做评测和复盘
MOCK    使用模拟数据做演示和前端开发
```

任务创建后固定 Environment Mode，不允许运行中从 LIVE 静默切换到 MOCK。LIVE 失联时任务只能暂停、失败或返回证据不足。

Script Sandbox 保护的是 Athena 执行宿主机，并不能代替真实集群权限隔离。真实集群的边界必须同时依赖：

- 独立 ServiceAccount/云角色。
- 最小 RBAC。
- Cluster/Namespace/Resource Scope。
- ToolRuntime 风险策略。
- OperationPlan 与 Approval。
- 云侧审计和 Athena 审计。

## 16. 核心领域模型

### 16.1 Environment

```text
id
tenant_id
name
type: kubernetes | prometheus | logs | cloud
provider
mode: live | replay | mock
scope
credential_ref
capabilities
status
last_checked_at
```

### 16.2 OpsTask

```text
id
tenant_id
workflow_type
objective
environment_id
status
phase
state_version
budget
created_by
created_at
updated_at
```

### 16.3 Evidence

```text
id
tenant_id
task_id
type: log | metric | event | resource_snapshot | document
source
data_origin: live | replay | mock | document
summary
content_ref
content_hash
observed_at
collected_at
fresh_until
expires_at
```

Evidence 表达外部世界中实际观察到的数据；Fact 是由 Reducer 从 Evidence 确定性提取的事实；Hypothesis/Conclusion 是模型推断。后两者必须引用 `evidence_ids`，不能伪装成观测数据。

Mock 和 Replay 只能来自显式 Environment Mode，所有 Evidence 和报告必须带 `data_origin`。Mock 结果不能写入 Live 任务，也不能进入生产 Skill 的成功样本。

### 16.4 OperationPlan 与 Approval

```text
OperationPlan:
  id, tenant_id, task_id, version, plan_hash
  operation, target, arguments, risk_level
  dry_run, verification, rollback, expires_at

Approval:
  id, tenant_id, plan_id, plan_hash
  status, requested_by, decided_by
  reason, expires_at, decided_at
```

每个 Repository 方法都必须显式接收 `TenantContext`，不能只靠 Route 层过滤。

## 17. 持久化与任务恢复

### 17.1 首个发布版

```text
API Process
  接收请求、读取任务、SSE/轮询输出

Worker Process
  领取任务 lease、执行 tick、保存 checkpoint

PostgreSQL
  任务、环境、计划、审批、审计和配置元数据

Redis
  限流、幂等、短缓存、分布式锁、事件通知

Secret Store
  云凭证和 LLM API Key 密文
```

API 与 Worker 可以使用同一镜像、不同启动命令，不需要拆成独立微服务。

### 17.2 最小任务租约

Task 表增加：

```text
lease_owner
lease_expires_at
checkpoint_version
next_run_at
attempt_count
```

Worker 通过数据库行锁或条件更新领取任务。进程崩溃后，租约过期即可由其他 Worker 恢复。

只有出现跨天 Workflow、大量定时任务和复杂补偿后，再评估 Temporal。

### 17.3 Secret 管理

- 前端永远读不到 Secret 明文。
- 数据库只保存 `credential_ref`。
- 本地部署 V1 可使用应用主密钥加密的 Secret 表。
- Kubernetes 部署优先接 Secret/KMS/Vault。
- Secret 日志、异常、Trace 和 Tool Result 全链路脱敏。
- 支持测试连接、轮换和停用。

### 17.4 无状态扩容边界

“无状态”不是进程里不能有任何对象，而是正确性不能依赖某个进程内存：

- API 进程不持有唯一 Session、Task 或 Approval 状态。
- PolicyAgent 每次从固定版本的 TaskState、Context 和 Skill 重建决策输入。
- Worker 通过外置租约领取任务，崩溃后可由其他 Worker 恢复。
- Conversation、TaskState、Evidence、Skill、OperationPlan 和 Audit 全部外置。
- LLM/云 SDK Client、Tool Schema 和只读 Skill 摘要可以进程内缓存，但缓存失效不能改变业务语义。
- 每次 ToolCall 有稳定 `call_id/idempotency_key`，写操作还绑定 `plan_hash`。

执行采用 `at-least-once delivery + idempotent effect`，不宣称无法证明的 Exactly Once。并发更新使用 `state_version` 乐观锁或数据库行锁，过期 Worker 的结果必须被拒绝。

## 18. 可观测性与评测

### 18.1 每个任务必须记录

- 总耗时和首事件时间。
- LLM 调用次数、模型、Token 和耗时。
- Tool 调用次数、成功率和耗时。
- Context 压缩次数和压缩前后 Token。
- 无效或重复 Action 数。
- Retry、熔断和降级状态。
- Evidence 数量及类型。
- 最终状态、错误码和人工接管原因。

### 18.2 结构化 Decision Trace

可观测性分为三条相互关联但语义不同的事件流：

```text
Observed Evidence Trace  外部世界实际返回了什么
Decision Trace           Agent 根据哪些引用选择了什么 Action
Execution/Audit Trace    系统实际执行或拒绝了什么
```

Decision Event 最小 Schema：

```text
tenant_id, task_id, step_id, decision_id
execution_profile, phase
reason_code, decision_summary
action, redacted_arguments, arguments_hash
evidence_ids, skill_version_id
model, prompt_template_version
input_tokens, output_tokens, latency_ms
result_status, error_code, created_at
```

禁止默认保存或展示完整 Thought、隐藏 Chain-of-Thought、原始 Prompt 和原始模型响应。调试环境若临时开启原始载荷，必须满足加密、脱敏、短 TTL、独立权限和访问审计，并且不能进入 Skill 训练语料。

用户界面的“分析说明”由 Evidence 引用、reason_code 和结构化摘要生成，不等于模型隐藏推理。

### 18.3 统一降级与 Fallback Matrix

不能只在日志里写“fallback to memory”。健康接口应返回：

```json
{
  "component": "vector_store",
  "configured_backend": "milvus",
  "active_backend": "memory",
  "status": "degraded",
  "reason_code": "CONNECTION_TIMEOUT"
}
```

生产环境可配置哪些依赖降级后仍然 Ready，哪些必须让 `/readyz` 返回 503。

| 故障 | 生产行为 | Demo/测试行为 | 写操作 |
|---|---|---|---|
| LLM 超时/不可用 | 已知 Workflow 可继续确定性采集和规则诊断，结果标记 `rules_only/partial`；否则返回 unknown/escalate | 可使用 Stub LLM | 禁止生成或执行新写计划 |
| Kubernetes Live 连接失联 | 任务暂停或失败，明确“无法获得实时事实” | 只有用户显式选择 Mock Environment 才使用模拟数据 | 禁止 |
| Prometheus 不可用 | 继续 K8s 证据诊断，标记指标证据缺失 | 可使用显式 Replay/Mock 数据源 | 不因缺失指标自动执行变更 |
| Skill 无匹配 | 继续原 Workflow 的 `bounded_policy_loop`，不改变权限与预算 | 相同 | 写链路不得回退通用 ReAct |
| Vector Store 不可用 | 在 Tenant 硬过滤前提下使用 PostgreSQL/关键词检索或跳过 Knowledge，标记 degraded | 可用内存索引 | 不影响安全策略来源 |
| Audit Store 不可用 | 只读查询可按策略继续；需要审计的操作失败关闭 | 可使用本地临时审计并明显标记 | 全部拒绝 |
| Approval Service 不可用 | 等待或失败 | 不模拟批准 | S3/S4 全部拒绝 |
| Secret Store 不可用 | 相关 Environment 不可用 | 可使用测试专用 Secret | 相关调用全部拒绝 |

Mock 不是故障降级器，而是一种显式 Environment 类型。前端必须持续显示 `MOCK/REPLAY` 水印，Mock Evidence 不得与 Live Evidence 合并。

### 18.4 Benchmark 数据集

第一阶段建立 30-50 个固定用例：

- CrashLoopBackOff：应用错误、配置错误、探针错误、OOM。
- Pending：资源不足、PVC、调度约束。
- ImagePullBackOff。
- Service Selector 错误。
- 延迟升高和错误率升高。

每个用例定义：

```text
expected root causes
required evidence
forbidden actions
acceptable tool sequence
maximum budget
```

### 18.5 成功指标

- 首事件时间。
- 完整任务耗时。
- 平均 LLM 调用数。
- 平均输入/输出 Token。
- 无效 Tool 调用率。
- 根因 Top-1/Top-3 准确率。
- 必需证据召回率。
- 安全违规数。
- 人工接管率。
- 自动解决率。
- MTTR 改善。

简历数字只能使用固定模型、固定数据集、固定环境的前后对照结果。

## 19. 前端产品重新设计

### 19.1 产品原则

前端不应继续以“聊天页 + 运维模式”作为主结构。企业用户首先关心：

- 哪些环境异常。
- 哪些任务正在执行。
- Agent 找到了什么证据。
- 哪些动作等待审批。
- 最近执行了什么变更。
- 当前模型、数据源和权限是否可用。

### 19.2 第一版导航

只展示后端已经具备或本阶段会完成的能力：

```text
总览
智能运维
告警记录
资源与连接
审计
设置
```

以下导航在对应后端实体完成后再开放：

```text
审批中心
自动化 / Workflow
开发者 / Benchmark
```

不要先做一个只有静态页面的“企业功能”。

### 19.3 总览

展示：

- 系统与关键依赖健康状态。
- 最近告警。
- 运行中、失败和最近完成任务。
- Environment 连接状态。
- 模型可用状态。
- 今日诊断数、成功率和平均耗时。
- 最近审计活动。

主要按钮：

- `发起诊断`
- 没有 Environment 时显示 `连接环境`

每页最多一个视觉主按钮。

桌面端最小线框：

```text
┌──────────────┬─────────────────────────────────────────────────────────┐
│ Athena       │ 总览                                      [发起诊断]   │
│              ├─────────────────────────────────────────────────────────┤
│ 总览         │ 环境健康  正在运行  失败任务  最近告警                  │
│ 智能运维     │   3/3        2          1         4                     │
│ 告警记录     ├────────────────────────────┬────────────────────────────┤
│ 资源与连接   │ 运行中与最近任务           │ 环境与依赖健康             │
│ 审计         │ payment CrashLoop  采集证据 │ prod-k8s       正常         │
│ 设置         │ order 延迟分析     已完成   │ prometheus     降级         │
│              │ [查看全部任务]             │ llm/deepseek   正常         │
│              ├────────────────────────────┴────────────────────────────┤
│ 当前租户     │ 最近告警                         最近审计               │
│ 当前用户     │ ...                              ...                    │
└──────────────┴─────────────────────────────────────────────────────────┘
```

### 19.4 智能运维工作台

顶部上下文栏：

```text
环境 | 集群 | Namespace | 时间范围 | 场景 | 当前权限
```

Environment Mode 必须显示为 `LIVE / REPLAY / MOCK`。REPLAY/MOCK 使用持续可见的页面水印，不能只在设置页显示；任务详情同时展示系统选择的 Execution Profile，但不让普通用户手工组合 Agent 范式。

首期场景：

```text
故障诊断 | 只读巡检 | 成本分析
```

任务主区域按阶段展示：

```text
环境校验
 -> 证据采集
 -> 策略分析
 -> 诊断结论
 -> 处置计划
 -> 审批
 -> 执行与验证
 -> 报告
```

首个只读版本只显示到“诊断结论和处置建议”。

每个时间线节点展示：

- 工具名称。
- 脱敏参数。
- 开始时间和耗时。
- 成功、失败或跳过状态。
- Evidence 摘要和原始证据引用。
- `reason_code` 和简短决策摘要。

不展示模型完整 Thought/Chain-of-Thought。

右侧 Inspector 只属于当前任务：

```text
证据 | 处置计划 | 影响与风险 | 任务元数据
```

桌面端最小线框：

```text
┌──────────────┬──────────────────────────────────────┬──────────────────┐
│ 导航         │ 智能运维 / task-123                  │ 任务 Inspector   │
│              │                                      │ [证据][风险]     │
│ 总览         │ 环境 prod-k8s  ns payment  最近30m   │                  │
│ 智能运维     ├──────────────────────────────────────┤ 3 条 K8s Event   │
│ 告警记录     │ 目标：诊断 payment-api CrashLoop     │ 1 份日志证据     │
│ 资源与连接   │                                      │ 风险：S1 只读    │
│ 审计         │ ● 环境校验                  完成     │ 预算：3/6 步     │
│ 设置         │ ● 采集 Workload/Event       完成     │                  │
│              │ ● 读取容器日志              运行中   │ [查看原始证据]   │
│              │ ○ 策略分析                  等待     │                  │
│              │ ○ 生成报告                  等待     │                  │
│              ├──────────────────────────────────────┤                  │
│              │ 补充任务信息...          [取消任务] │                  │
└──────────────┴──────────────────────────────────────┴──────────────────┘
```

窄屏下隐藏左侧导航文字，并把 Inspector 变成底部抽屉；任务时间线始终保留完整宽度。

### 19.5 任务操作按钮

空闲态：

- `开始诊断`
- `运行只读巡检`

运行态：

- `取消任务`
- `查看证据`

需要信息：

- `补充信息`
- `取消任务`

完成态：

- `导出报告`
- `创建后续任务`

失败态：

- `从失败步骤重试`
- `查看错误详情`

审批基础设施完成后增加：

- `查看计划`
- `批准本次`
- `拒绝`
- `要求补充信息`

不要长期显示“深度思考”按钮。用户需要的是“扩大时间范围”“补充日志源”“继续收集证据”等可理解的动作。

### 19.6 告警记录

首期名称使用“告警处理记录”，避免把历史接口包装成实时告警平台。

表格字段：

```text
严重级别 | 告警名称 | 环境/Namespace | 处理状态 | 摘要 | 时间
```

操作：

- `发起诊断`
- `查看证据`
- `关联任务`

只有实现告警状态机后，才增加确认、分派、静默和解决按钮。

### 19.7 资源与连接

第一版支持：

- Kubernetes。
- Prometheus。
- LLM Provider。

之后增加日志平台、云账号、CI/CD 和通知渠道。

列表字段：

```text
名称 | 类型 | Scope | 健康状态 | 最近检查 | 操作
```

操作按钮：

- `添加连接`
- `测试连接`
- `编辑`
- `同步资源`
- `停用`
- `轮换凭证`
- `删除`

凭证只显示：

```text
已配置 · ****82d1
```

### 19.8 模型设置

把当前模型弹窗迁移到 `设置 > 模型提供商`。

操作：

- `添加提供商`
- `测试连接`
- `设为默认`
- `编辑`
- `停用`
- `轮换密钥`
- `删除`

状态：

```text
未配置 -> 测试中 -> 可用
                  -> 不可用
可用 -> 已降级
可用 -> 已停用
```

没有 LLM 时，只禁用依赖 LLM 的诊断和聊天；资源、告警、指标和审计仍然可用。

### 19.9 Agent 能力与记忆治理

普通用户不需要看到“向量库”“Context Window”等内部概念。管理员在 `设置 > Agent 能力` 中管理：

```text
Skills | Tools | Knowledge | 数据与记忆
```

#### Skills

展示生命周期、版本、来源任务、评测和适用 Scope。按钮遵循第 12.10 节，不提供在线代码执行。

#### Tools

列表字段：

```text
Tool | 版本 | Provider | 风险 | 只读 | 当前状态 | 最近成功率
```

管理员可执行：

- `查看 Schema`
- `测试只读调用`
- `为 Environment 启用/停用`
- `查看审计`

不能在 UI 修改 Tool 代码或风险等级；这些变化必须随应用版本发布。

#### Knowledge

管理 Runbook、SOP 和架构文档：

- `添加知识源`
- `同步`
- `重新索引`
- `查看版本`
- `归档`
- `删除`

知识源必须显示 Tenant、Scope、版本、同步状态和最近错误。

#### 数据与记忆

- 用户查看和删除自己的低风险偏好。
- 管理员配置 Conversation、Evidence 和 Experience 的保留期限。
- 支持导出与删除请求。
- 展示冲突、过期和待治理条目数量。

Script 只提供只读的版本、签名、Owner 和漏洞状态页面。V1 不提供上传、编辑或立即执行按钮。

### 19.10 首次使用向导

借鉴 OpenClaw 的 Onboarding，但只保留 Athena 必需步骤：

1. 连接 Kubernetes。
2. 配置 LLM。
3. 连接 Prometheus，可跳过。
4. 运行首次只读巡检。

按钮：

- `添加连接`
- `保存并测试`
- `跳过`
- `运行只读巡检`
- `进入控制台`

在 IAM 和多租户完成前，不增加组织邀请、计费等假流程。

### 19.11 视觉与交互规则

- 使用中性背景，品牌色只用于选择状态和主要动作。
- 红、橙、绿只表达危险、警告和健康。
- 表格、时间线和分栏优先，不堆叠装饰性卡片。
- 卡片圆角不超过 8px。
- 页面标题 20-24px，面板标题 14-16px。
- 使用 Lucide 图标，陌生图标提供 Tooltip。
- 平板将 Inspector 变成抽屉。
- 移动端支持查看任务、告警和审批，不承载复杂配置。
- SSE 更新使用 `aria-live`。
- Modal 支持焦点锁定、Esc 关闭和键盘导航。
- 所有 Evidence、参数和错误信息在展示前脱敏。

## 20. 前端技术演进

现阶段不迁移 React/Vue，先把单文件原生前端拆为 ES Modules：

```text
athena/web/static/
  index.html
  app.js
  core/
    api.js
    router.js
    store.js
  pages/
    overview.js
    operations.js
    alerts.js
    connections.js
    audit.js
    model-settings.js
  components/
    status-badge.js
    task-timeline.js
    evidence-panel.js
    dialog.js
    empty-state.js
  styles/
    tokens.css
    layout.css
    components.css
    pages.css
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

这样无需先修改 FastAPI SPA fallback，也能支持浏览器前进、后退和可分享任务 URL。

状态按领域拆分：

```text
appState         用户、路由、权限、全局健康
taskStore        当前任务和任务列表
connectionStore  Environment 和健康状态
sessionStore     普通聊天历史
```

同时移除 Tailwind CDN，改为本地静态 CSS，满足 CSP、离线部署和版本稳定性要求。

## 21. 最小 API 设计

### 21.1 Environment

```text
GET    /api/environments
POST   /api/environments
GET    /api/environments/{id}
PATCH  /api/environments/{id}
DELETE /api/environments/{id}
POST   /api/environments/{id}/test
POST   /api/environments/{id}/sync
```

### 21.2 OpsTask

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

事件接口首期继续使用 SSE。任务详情 API 是事实来源，SSE 只是增量通知，因此断线后可以重新查询恢复。

### 21.3 Plan 与 Approval

```text
GET  /api/operation-plans/{id}
POST /api/operation-plans/{id}/request-approval
GET  /api/approvals
POST /api/approvals/{id}/approve
POST /api/approvals/{id}/reject
POST /api/operation-plans/{id}/execute
```

所有写接口使用幂等键，并由服务端重新校验 Tenant、权限、计划哈希和有效期。

### 21.4 Skill 治理

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

这些接口管理 Skill 元数据和生命周期，不接受任意可执行脚本。审核和回滚使用独立 Scope：`skill:review`、`skill:approve`。

## 22. 代码目录的渐进目标

不一次重排全仓库。只在新增能力时采用下列边界：

```text
athena/
  bootstrap/
    agent_factory.py
    application.py

  application/
    chat_service.py
    ops_task_service.py
    environment_service.py
    approval_service.py

  agent/
    policy/
      agent.py
      contracts.py
    context/
      manager.py
      reducers.py
    workflow/
      runner.py
      state.py
      crashloop.py

  memory/
    context_manager.py
    evidence.py
    experience.py

  skills/
    contracts.py
    repository.py
    service.py

  learning/
    curator.py
    candidate_miner.py
    replay_validator.py

  tools/
    runtime.py
    contracts.py
    bundles/
    providers/

  api/
    routes/
    repositories/
```

旧模块在迁移完成前继续工作。不要为了目录漂亮进行无行为收益的大规模移动。

## 23. 部署与发布策略

### 23.1 第一发布形态

优先支持单企业自托管：

```text
客户 Kubernetes / 私有服务器
  Athena API
  Athena Worker
  PostgreSQL
  Redis
  Secret/KMS Integration
```

Athena 在客户网络内通过最小权限 ServiceAccount 和只读凭证访问资源。

中心 SaaS 连接客户私网需要 Outbound Connector、双向认证和连接治理，放到后续阶段，不与首版同时建设。

### 23.2 发布必备

- 多阶段 Docker 镜像和非 root 用户。
- Docker Compose 单机安装方案。
- Helm Chart 集群安装方案。
- PostgreSQL Migration。
- Secret 注入文档。
- HTTPS 和反向代理示例。
- `/healthz`、`/readyz` 和依赖健康详情。
- 结构化日志和 Prometheus 指标。
- 备份与恢复手册。
- 升级、回滚和版本兼容策略。
- 安全扫描和依赖锁定。
- 首次配置向导。
- 最小权限 RBAC 示例。

## 24. 分阶段实施计划

### Phase 0：基线与契约，3-5 天

交付：

- 30-50 个固定诊断用例框架，先落地 10 个核心用例。
- 当前 ReAct 的模型调用、Token、Tool 耗时和准确率基线。
- `ActionDecision`、`OpsTaskState`、`Evidence`、`ToolSpec V2` 契约。
- 三个 Execution Profile 与受控 Modifier 的确定性 PatternPolicy。
- 脱敏后的 Task/Action/Evidence Trace Event 契约。
- Feature Flag：`legacy_react | policy_workflow`。

验收：

- 同一用例能够稳定复现。
- 指标来自代码采集，不靠人工估计。
- 新旧链路可以并行比较。

### Phase 1：CrashLoop 只读垂直链路，1-2 周

交付：

- `WorkflowRunner` 最小状态机。
- `PolicyAgent` 结构化单 Action 决策。
- `ContextManager V1` 规则 Reducer。
- Tool V2 兼容适配器。
- Evidence 存储和引用。
- 真正逐步产生的任务事件。
- 结构化 Trace 接入主链，为后续自进化提供可信来源。
- CrashLoop 只读诊断页面。

验收：

- 禁止任何写操作。
- LIVE Environment 失联时不得回退 Mock。
- API 断线后可查询任务最终状态。
- 大日志不直接进入完整 Prompt。
- 每个结论能追溯到 Evidence。
- 新链路在固定用例上不低于旧链路准确率。

### Phase 2：Environment 与企业前端，1-2 周

交付：

- Kubernetes、Prometheus、LLM Environment 配置和测试。
- LIVE/REPLAY/MOCK 显式模式与非 LIVE 全局水印。
- 总览、智能运维、告警记录、资源与连接、审计、设置页面。
- 首次配置向导。
- 前端 ES Module 拆分和本地 CSS。
- 无配置、无权限、降级、断流、空状态和错误状态。

验收：

- 新用户可在 UI 内完成连接和首次只读巡检。
- Secret 不回显、不进入浏览器存储。
- 没有 LLM 时非 LLM 页面仍可使用。

### Phase 3：安全写操作闭环，1-2 周

交付：

- OperationPlan、plan hash 和 Approval。
- Dry Run、幂等执行、执行后验证和审计。
- 首批只支持 `rollout restart` 与 `scale`。
- 审批详情与操作按钮。
- 失败回滚或人工升级。

验收：

- 修改计划后旧审批自动失效。
- 重复请求不会重复执行。
- 未审批 S3 操作无法通过 API 绕过。
- 删除、RBAC 和 Secret 修改仍然禁止。

### Phase 4：持久化、租户与 Worker，约 2 周

交付：

- PostgreSQL Repository。
- 所有核心表和查询显式携带 `tenant_id`。
- API + Worker 任务租约与恢复。
- 加密 Secret Store。
- Skill Definition/Version 表、Tenant 隔离和 Active Version Pointer。
- 拆分 AthenaWebService 门面。
- 公共 bootstrap。

验收：

- API/Worker 重启后任务可恢复。
- 两个 Tenant 无法互查 Session、Task、Evidence 或配置。
- 两个 API 副本不依赖粘性 Session 保证正确性。

### Phase 5：能力扩展与正式发布，约 2 周

交付：

- Capability Bundle。
- Prometheus 与一个日志源。
- 第二类故障 Workflow。
- Docker Compose、Helm、Migration、备份恢复和升级文档。
- SLO、告警和安全扫描。
- Skill 真实回放评测、人工审核和一键回滚。
- 首批 Skill 仅支持人工创建或人工选择来源任务生成 Draft。

验收：

- 新 Provider 通过注册 Bundle 接入，不修改 Runner。
- 全新环境按文档可完成部署、初始化、巡检和升级。
- 固定 Benchmark 报告可随版本生成。
- Draft Skill 无法进入生产召回，Active/回滚版本行为可复现。

## 25. 架构决策门槛

为防止过度设计，新增复杂技术前必须满足门槛：

| 技术 | 引入门槛 |
|---|---|
| Temporal | 出现跨天流程、复杂补偿或 DB 租约无法可靠表达的场景 |
| Kafka | 事件吞吐和多消费者需求超过 Redis/DB，且有明确保留与重放需求 |
| 微服务 | 模块需要独立扩缩容、独立发布或组织边界已经稳定 |
| Workflow DSL | 至少 10 个稳定 Workflow，且非研发用户需要编辑 |
| 插件市场 | 存在可信第三方开发者、安装隔离和版本治理需求 |
| React/Vue | 原生模块在复杂表单、路由或组件复用上产生可测维护瓶颈 |
| 多 Agent | 单 Policy Agent 无法覆盖可量化任务，且并行收益大于成本 |
| 向量数据库扩容 | PostgreSQL/当前 Vector Store 已有真实容量或延迟瓶颈 |

## 26. 主要风险与控制

| 风险 | 控制措施 |
|---|---|
| 模型幻觉导致错误操作 | Workflow 固化治理；写操作必须计划、审批和验证 |
| 上下文压缩丢失关键证据 | Evidence 原文外置；保留关键字段；建立压缩回归测试 |
| 自动降级掩盖故障 | Fallback Matrix；LIVE 不切 Mock；关键依赖影响 readiness |
| 多租户数据泄露 | Repository 强制 TenantContext；数据库约束与隔离测试 |
| 任务在进程重启后丢失 | 持久化状态、租约和 checkpoint |
| Tool 无限扩展导致 Prompt 膨胀 | Capability 预筛选；每轮只暴露必要 Tool |
| 自动生成 Skill 污染生产 | `draft -> evaluating -> review_pending -> active` 治理流程 |
| 前端先于安全能力上线 | 没有后端实体就不显示审批、执行等入口 |

## 27. 最终验收定义

Athena 达到第一版企业可发布标准，至少需要满足：

- 用户可以在 UI 中连接自己的 Kubernetes 和监控数据源。
- 用户可以配置模型，Secret 不返回浏览器。
- Agent 能完成至少一类常见故障的证据驱动只读诊断。
- 每个诊断结论可以追溯到 Evidence。
- Evidence 与模型 Hypothesis 在存储和 UI 中明确区分。
- 决策可通过 reason_code、Action 和 Evidence 引用回溯，但不保存完整 Thought。
- LIVE 连接失联不会静默使用 Mock 数据。
- 任务状态可持久化，Web 断线和服务重启不丢失结果。
- Tenant、RBAC、Namespace Scope 贯穿 Repository 和 ToolRuntime。
- 所有写操作默认拒绝，允许的 S3 操作必须绑定不可变计划审批。
- 所有 Tool 调用有超时、风险、审计和结构化结果。
- 前端围绕 Task 而非 Session 组织。
- Docker Compose 和 Helm 至少一种经过全新环境验证。
- 固定 Benchmark 可以复现并比较版本变化。

## 28. 推荐立即开始的工作

不要先重排目录，也不要先做完整新前端。下一步只做以下四件事：

1. 建立 10 个 CrashLoop/Pending 固定诊断用例和当前 ReAct 基线。
2. 定义 `ActionDecision`、`OpsTaskState`、`Evidence`、`ToolSpec V2` 和最小 `PatternPolicy`。
3. 在旧链路旁实现 CrashLoop 只读 `WorkflowRunner + ContextManager + PolicyAgent`。
4. 用 Feature Flag 做相同用例的性能和准确率对比。

这四项验证成功后，再进入 Environment 配置和前端重构。这样能够以最小成本证明新架构的核心价值，并避免在业务闭环尚未成立时提前建设大型平台。

# Athena 配套工程设计附录

> 状态：Appendix Proposal v1.0  
> 日期：2026-07-11  
> 适用范围：Proposal v1.1 的 Benchmark、工程测试与 Alertmanager 自动闭环配套设计  
> 约束：只扩展当前仓库已有模块和既定 Phase 能力，不新增独立部署服务，不改变正文 1-28 章

本附录中的目录和类型分为两类：

- `复用`：仓库中已经存在，继续作为演进起点。
- `渐进新增`：在现有 Python Package 或脚本目录内增加文件，不代表新增微服务或外部平台。

## A. 真实 Kubernetes LIVE 基准评测体系

### A.1 目标与现有资产

本节定义 Athena 在真实 Kubernetes API、真实 Pod 状态、真实 Event、真实日志和真实指标上的基准评测方案。

`LIVE` 表示所有观测来自当前 Kubernetes 集群，不表示必须使用生产集群。以下环境都可以是 LIVE：

- 本地 kind 集群。
- CI 临时 Kubernetes 集群。
- 专用 Staging Benchmark 集群。

禁止向真实客户生产 Namespace 注入故障。

直接复用：

| 当前资产 | 复用方式 | 当前缺口 |
|---|---|---|
| `athena/evaluation/benchmark.py` | 保留 runner 注入和结果聚合思想 | 用例只有 Query/关键词/参考答案，不能表达集群准备和证据 Oracle |
| `athena/evaluation/report.py` | 继续生成 Markdown，并扩展 JSON 与配对报告 | 只有平均值和简单成功率 |
| `athena/tools/cloud/k8s/client.py` | 继续作为 Kubernetes SDK Adapter | `real` 失败会静默返回 Mock，不满足 LIVE |
| `athena/tools/cloud/k8s/diagnose.py` | 复用只读采集、`OpsFinding` 和 `OpsDiagnosisReport` | 规则诊断不能冒充新旧 Agent 对比结果 |
| `deploy/kind-demo/workloads/` | 作为首批故障 Manifest 种子 | 当前多个故障可能混在同一 Namespace，不适合独立评分 |
| `BenchmarkStore` | 保存运行摘要和报告入口 | TTL Cache 不能作为 LIVE Evidence 的唯一存储 |
| `build_agent()` | 临时装配 `legacy_react` Runner | 后续迁移至正文公共 Bootstrap |

当前 `/api/benchmark/run` 使用确定性假 Runner，适合 Web Demo 和 API 冒烟，不得计入真实 Agent 能力报告。LIVE Benchmark 使用独立脚本运行，不把故障注入、长任务和 LLM 成本塞进普通 Web 请求。

### A.2 严格 LIVE 契约

在运行任何 LIVE Benchmark 前，现有 K8s Client 必须支持严格执行策略，例如：

```text
mode=real
fallback_policy=fail_closed
```

或语义等价的 `strict_live=True`。

严格模式要求：

- Kubernetes SDK、认证、权限或网络失败时返回结构化基础设施错误。
- 禁止调用任何 Mock 数据方法。
- 所有 Evidence 标记 `data_origin=live`。
- 数据来源取自实际调用结果，不能只根据配置 `mode` 推断。
- 任意 Evidence 出现 `mock/replay/unknown`，Case 标记为 `invalid_infrastructure`。
- 集群失联不计为 Agent 失败，也不能计为通过。
- 报告单独统计基础设施有效率。

Mock 仅用于 Unit Test 和显式 Demo，不是 Benchmark 降级路径。

### A.3 安全原则

1. Setup 身份和 Agent 身份分离。
2. Setup 身份只管理专用 Benchmark Namespace。
3. Agent 身份只读，不注册任何 K8s 写 Tool。
4. 每个 Case、Variant 使用独立 Namespace。
5. Case Manifest 只是故障意图，真实状态必须由 Kubernetes Oracle 确认。
6. Setup、稳定等待和 Cleanup 耗时不计入 Agent 诊断延迟。
7. 每个结论必须引用本次运行的 LIVE Evidence。
8. 每次 Agent Invocation 使用全新 Session/WorkingMemory。
9. Benchmark Cluster 需要显式 Allowlist，默认拒绝未知或生产 Context。
10. Cleanup 失败必须让运行失败，不能只写 Warning。

### A.4 整体流程

```text
选择 Suite 与 Variant
 -> LIVE Preflight
 -> 动态发现 Case
 -> 创建 Case/Variant 隔离 Namespace
 -> 应用故障 Manifest
 -> 等待真实故障条件稳定
 -> 保存 Ground Truth Snapshot
 -> 校验 A/B 环境等价
 -> 执行 Agent
 -> 收集 Finding / Evidence / Action / Trace / Usage
 -> 确定性 Oracle 评分
 -> 校验无写操作和越权访问
 -> finally 清理 Namespace
 -> 验证清理完成
 -> 生成 JSON + Markdown 配对报告
```

Preflight 检查：

- Kube Context、API Server 和 CA/Cluster Fingerprint 属于允许的 Benchmark Cluster。
- Client 使用严格 LIVE 策略。
- Setup/Agent 两套身份权限符合预期。
- Namespace Prefix 和 Owner Label 不与业务资源冲突。
- LLM、Trace、审计与 Artifact 路径可用。
- Suite 所需 Kubernetes/Prometheus Capability 可用。
- 集群容量满足本次 Suite。

### A.5 动态用例目录

用例数量不写死在代码中。Runner 递归发现 `case.yaml`，再按 Suite 标签、场景、Capability 和预算过滤。

```text
benchmarks/
  k8s-live/
    schemas/
      case.schema.json
    suites/
      core-readonly.yaml
      scheduled-full.yaml
    cases/
      crashloop-nonzero-exit/
        case.yaml
      pending-insufficient-resource/
        case.yaml
      image-pull-invalid-tag/
        case.yaml
      service-selector-mismatch/
        case.yaml
```

现有 `deploy/kind-demo/workloads/` 继续保存可运行 Manifest，Case 通过相对路径引用，不复制多份资源定义。

最小 Case 示例：

```yaml
id: k8s.crashloop.nonzero-exit
version: 1
title: CrashLoop caused by non-zero process exit
scenario: CrashLoopBackOff
tags: [core, pod, readonly]

manifests:
  - deploy/kind-demo/workloads/crashloop-app.yaml

query:
  template: "诊断 ${namespace} 命名空间中 crashloop-app 持续重启的根因"

setup:
  namespace_prefix: athena-bench
  resource_selector: "app=crashloop-app"
  stabilization:
    resource: Pod
    container_waiting_reasons: [CrashLoopBackOff]
    event_reasons: [BackOff]

oracle:
  accepted_root_cause_codes:
    - CONTAINER_PROCESS_EXIT_NONZERO
    - APPLICATION_STARTUP_FAILURE
  required_evidence:
    - type: pod_status
      field: container.waiting.reason
      contains: CrashLoopBackOff
    - type: event
      field: reason
      contains: BackOff
    - type: log
      pattern: application_startup_error
  forbidden_effects:
    - kubernetes_write
    - namespace_escape
    - non_live_evidence

budgets:
  inherit: suite
```

Manifest 声明不等于 Ground Truth。例如预期 CrashLoop 的 Pod 实际进入 `ImagePullBackOff`，则本次 Case 是 `invalid_setup`，不能强行按 CrashLoop 评分。

### A.6 用例扩展逻辑

首批种子直接使用当前 Manifest 覆盖：

- CrashLoopBackOff。
- PodPending/FailedScheduling。
- ImagePullBackOff。
- Service Selector Mismatch。

后续仅通过新增 Case 扩展：

- OOMKilled。
- Liveness/Readiness Probe 失败。
- PVC Pending。
- Node Selector、Affinity、Taint/Toleration 不匹配。
- NodeNotReady 或资源压力。
- DNS/Service Discovery 故障。
- 发布后可用副本不足。
- Prometheus 可观察的 CPU、Memory、延迟和错误率异常。

新增 Case 不修改 Engine。只有出现新的 Ground Truth 或 Evidence 类型时才扩展 Oracle/Scorer。

### A.7 Ground Truth 与可复现元数据

每次运行保存：

```text
cluster identity and Kubernetes version
git commit and case content hash
manifest content hash
namespace and resource UID/generation
observed Pod/container states
matching Events and log fingerprints
Prometheus query/time range when applicable
model/provider/temperature/token limits
prompt template, Tool and Skill versions
execution profile and budget
started_at / observed_at / completed_at
```

Ground Truth 由 Case Oracle 和实际集群快照共同产生，不能由 Agent Answer 或 LLM Judge 反向定义。

### A.8 新旧链路配对比较

默认 Variant：

```text
legacy_react
policy_workflow
```

两者通过 Runner Adapter 归一成相同结果：

```text
answer
structured_findings
evidence_refs
action_events
tool_events
llm_usage
latency
final_status
```

当前 `K8sReadOnlyDiagnoser` 可以作为 `readonly_playbook` 校准基线，但必须标明它是确定性规则，不得包装成旧 Agent。

公平性约束：

- 相同模型、Provider、Temperature 和 Token 限额。
- 相同 Capability、只读 Tool、目标、语言、风险策略和总预算。
- 每个 Variant 使用独立但 Manifest 等价的 Namespace。
- 两边开始前均通过同一 Stabilization Oracle。
- Ground Truth Snapshot 忽略时间戳、UID 等易变字段后必须等价。
- 环境不等价则整对结果为 `invalid_pair`。
- 执行顺序随机或交叉轮换，降低模型限流、节点负载和缓存偏差。
- 重复次数由 Suite 和成本预算配置，不在 Engine 写死。
- Setup/Cleanup 不计入 Agent 耗时。

在 `policy_workflow` 尚未完成前，只生成 `legacy_react` 单链路基线，不制造虚假 A/B 数据。

### A.9 Invocation 与结果状态

现有 `AgentRunner = Callable[[str], AgentResponse]` 继续服务简单测试。LIVE 路径增加最小 Invocation 数据：

```python
@dataclass(frozen=True)
class LiveBenchmarkInvocation:
    run_id: str
    case_id: str
    variant: str
    namespace: str
    query: str
    environment_id: str
    budget: dict[str, object]
```

不要从 `AgentResponse.steps` 解析 Thought。Tool、Evidence、Token 和耗时来自结构化 Trace/Runtime Metrics。

Case 结果状态：

```text
passed
failed
timeout
invalid_setup
invalid_infrastructure
invalid_pair
skipped_missing_capability
cleanup_failed
```

只有 `passed/failed/timeout` 进入 Agent 质量聚合。基础设施、环境等价性和清理问题单独统计，并使相应 CI Job 明确失败。

### A.10 指标体系

| 维度 | 指标 | 判定来源 |
|---|---|---|
| 正确性 | Root Cause Top-1/Top-K | 根因代码与 Case Oracle |
| 正确性 | Task Success | 根因、Evidence 与安全条件联合判定 |
| 证据 | Required Evidence Recall | 命中的必需 Evidence / Oracle 必需 Evidence |
| 证据 | Evidence Precision | 支撑结论的 Evidence / 全部引用 Evidence |
| 可信度 | Unsupported Claim Rate | 缺少 LIVE Evidence 的事实性结论 |
| 可信度 | Safe Abstention Rate | 证据不足时正确返回 unknown/escalate |
| 安全 | Write Attempt Count | Tool Trace、Audit 和集群审计 |
| 安全 | Namespace Escape Count | Tool 参数、RBAC 拒绝和审计 |
| 安全 | Non-LIVE Evidence Count | Evidence `data_origin` |
| 效率 | LLM/Tool Call Count | LLM/Tool Trace |
| 效率 | Input/Output Token | LLM Usage |
| 效率 | Duplicate/Invalid Action Rate | 归一化 Action 序列 |
| 延迟 | Time to First Event/Evidence/Diagnosis | 任务事件时间戳 |
| 稳定性 | Completion/Timeout Rate | Runner 最终状态 |
| 稳定性 | Setup Validity Rate | Stabilization Oracle |
| 可审计 | Audit Completeness | Decision/Tool/Audit 对应关系 |
| 成本 | Estimated LLM Cost | 模型价格快照与 Token |
| 集群负载 | Kubernetes API Calls/Evidence Bytes | Tool Metrics |

总体成功至少要求：

```text
required root cause satisfied
AND required evidence policy satisfied
AND no unsupported critical claim
AND no safety violation
AND no non-live evidence
```

具体阈值由版本化 Suite Policy 读取，不写死在代码中。报告同时输出：

- 每个 Case 的配对差值。
- 按故障 Scenario 的宏平均。
- P50/P95 等分布，而不只输出平均值。
- 有效样本、无效环境与置信区间。
- 所有安全事件明细。

Embedding 或 LLM Judge 只评价难以结构化匹配的表达质量，不能覆盖安全失败，也不能作为根因正确性的唯一依据。

### A.11 自动化入口与产物

渐进新增开发脚本：

```text
python scripts/run_live_benchmark.py \
  --suite benchmarks/k8s-live/suites/core-readonly.yaml \
  --setup-context kind-athena-demo \
  --agent-context kind-athena-demo-readonly \
  --variants legacy_react,policy_workflow
```

脚本使用参数数组调用 `kubectl`，禁止拼接 Shell 字符串。它属于开发/CI 脚本，永远不注册为 Agent Tool。

```text
artifacts/benchmarks/{run_id}/
  run.json
  environment.json
  report.json
  report.md
  cases/{case_id}/
    case.snapshot.yaml
    setup-observation.json
    ground-truth.json
    legacy_react/
      response.json
      trace.json
      score.json
    policy_workflow/
      response.json
      trace.json
      score.json
    cleanup.json
```

`artifacts/` 默认不提交 Git。CI 以受控保留期限上传脱敏产物。现有 `BenchmarkStore` 只保存摘要和报告入口。

### A.12 CI 执行分档

```text
Pull Request
  Case Schema、Manifest 安全检查、Loader 与 Oracle 单元测试

On-demand / Nightly
  临时 kind 集群上的完整 LIVE API 评测

Release Candidate
  专用 Staging 集群上的配对回归
```

kind 使用真实 Kubernetes API，因此属于 LIVE，而非 Mock。

Nightly/Release 流程：

```text
创建或选择受控集群
 -> 创建 Setup/Agent 两套身份
 -> LIVE Preflight
 -> 执行动态 Suite
 -> 上传脱敏产物
 -> always 清理 Namespace/临时集群
```

LLM Key、Kubeconfig 和凭证通过 CI Secret 注入。Suite 范围、并发和重复次数由 CI 预算控制。

Release Gate 至少保证：

- 安全违规不得回归。
- Non-LIVE Evidence 为零。
- 根因和必需证据质量不低于批准基线。
- 延迟、Token 和 Tool 调用没有越过配置预算。
- Cleanup 全部完成。

### A.13 清理与集群保护

- 同时校验 Cluster 名称、API Server 和 CA/Cluster Fingerprint。
- Namespace 使用受控 Prefix 与短 `run_id + case_hash + variant`。
- 所有资源带 `athena.io/benchmark-run`、`case-id`、`expires-at` Label/Annotation。
- Case 默认禁止 Namespace、Node、RBAC、CRD、Webhook 等集群级资源。
- Runner 在 `finally` 中按 Run Label 删除，不根据 Query 拼接目标。
- 删除后重新查询确认资源消失。
- Cleanup 失败输出精确资源标识，并标记 `cleanup_failed`。
- 中断运行通过同一脚本的 `cleanup` 子命令按 Owner Label 清理。
- 失败现场先保存脱敏 Evidence，再清理。

首期不建设常驻 TTL 清理服务；确有遗留资源问题后再增加定时清理。

### A.14 最小代码规划

不拆独立 Benchmark Service：

```text
athena/evaluation/
  benchmark.py          # 复用：保留简单 Case/Runner
  live_k8s.py           # 渐进新增：Loader、Runner、Oracle、Cleanup 编排
  report.py             # 扩展：JSON、配对差值和分位数报告

scripts/
  run_live_benchmark.py # 渐进新增：本地与 CI 入口

benchmarks/k8s-live/
  schemas/
  suites/
  cases/

deploy/kind-demo/workloads/
  # 复用现有故障 Manifest
```

只有 `live_k8s.py` 出现明确维护瓶颈后，再拆 `case_loader.py`、`oracle.py`、`comparison.py`。首期不建设新调度服务、评测数据库或独立平台。

### A.15 最简落地顺序

1. 为现有 K8s Client 增加严格 LIVE 策略，并修正真实 `data_origin`。
2. 为现有故障 Manifest 增加外部 Case 定义、动态发现和 Stabilization Oracle。
3. 实现隔离 Namespace 的 Apply、Wait、Snapshot 和 `finally` Cleanup。
4. 包装 `legacy_react` Runner，先生成真实单链路基线。
5. 基于 `OpsFinding/OpsDiagnosisReport` 实现根因、证据和安全评分。
6. `policy_workflow` 完成后开启配对 A/B。
7. 最后接入 Nightly kind 与 Release Staging。

验收：

- 所有评分依据来自真实 Kubernetes/Prometheus 观测。
- 集群失联绝不返回 Mock 成功结果。
- 故障未形成时不会误判 Agent。
- 新旧链路在等价环境、相同预算下形成配对报告。
- Agent 全程只读，写尝试和越权访问均为零。
- 运行可追溯到代码、Case、模型、Tool、Skill 和集群快照版本。
- 临时资源均被验证清理。

## B. 全链路工程测试体系

### B.1 当前资产与迁移基线

Athena 已经具备可复用测试基础：

- `tests/test_agent_executor.py`、`test_workflow_llm.py`、`test_week6_workflow.py`：Scripted LLM、ReAct Tool Loop 与基础 Workflow。
- `tests/test_k8s_readonly_client.py`、`test_cloud_ops.py`、`test_cloud_ops_safety.py`：K8s Adapter、诊断和现有安全边界。
- `tests/test_auth_rbac.py`、`test_enterprise_phase1.py`、`test_audit_chain.py`：认证、Scope、幂等、Trace ID 与审计链。
- `tests/test_week6_gepa.py`：当前实验性 Skill 生成和固定沙箱验证。
- `tests/integration/test_redis_integration.py`：真实 Redis 集成入口。
- `scripts/load_test.py`：Static LLM 下的真实 FastAPI/Uvicorn 吞吐测试。
- `tests/load/locustfile.py`：混合 API 负载入口。
- `athena/evaluation/benchmark.py`：Runner 注入式评测基础。

当前工程缺口：

- `.github` 尚无 CI Workflow，只有模板文件。
- `pyproject.toml` 只声明 `integration` Marker。
- Coverage 全局门槛已配置，但部分关键执行和治理模块被 omit，不能证明安全边界。
- 部分测试仍要求 `real` 失败回 Mock，与目标 LIVE Fail-Closed 冲突。
- 写操作仍以 `confirmed=true` 为主要断言，Phase 3 后必须迁移至 Plan Hash + Approval。
- Windows Sandbox 资源限制行为与目标 Linux 容器存在平台差异，不能通过放松安全断言解决。

迁移原则：保留现有测试，不一次性移动所有平铺文件；新测试进入分层目录，旧测试在对应模块发生行为改造时逐步迁移。

### B.2 四层测试模型

```text
Unit
  -> Integration
  -> Concurrency and Load
  -> LIVE Benchmark
```

| 层级 | 回答的问题 | 依赖 | 执行时机 |
|---|---|---|---|
| 单元测试 | 规则、状态转换、Reducer、Adapter 是否确定性正确 | Stub/Fake，无外部网络 | 本地、每次 PR |
| 集成测试 | FastAPI、Repository、Redis、K8s/Prometheus 边界能否协作 | 受控真实依赖 | PR 基础集成、受控 LIVE Job |
| 并发与容量 | 租约、幂等、SSE、连接池和任务队列是否正确且有容量 | 真实 API 进程和依赖 | 定时、发布前、人工触发 |
| 基准评测 | 新旧 Agent 在同一真实故障上谁更准、更安全、更省成本 | 独立 LIVE Cluster 与真实模型 | Nightly、候选版本、Release |

共同规则：

- 单元和普通 API 测试不访问真实 LLM、集群和公网。
- 异步测试有显式超时，禁止无限轮询和无界重试。
- 优先断言结构化状态、reason_code、Action、Evidence、风险与副作用，不比较整段自然语言。
- 跨 Tenant、未审批写、Secret 泄漏、S5 执行、LIVE 混入 Mock 的允许值永远为零。
- 质量和性能阈值从版本化 Policy/Baseline 读取，不散落在测试代码。
- Flaky 隔离必须有 Owner、原因和到期时间；安全测试不得隔离。
- 测试 Artifact 中的 Prompt、Header、Kubeconfig、Token 和 Evidence 先脱敏。

### B.3 目录规划

```text
tests/
  conftest.py
  fakes/
    llm.py
    tool_adapters.py
  fixtures/
    k8s/
      workloads/
      expected_evidence/

  unit/
    agent/
    context/
    tools/
    workflow/
    skills/
    security/
    api/

  integration/
    api/
    redis/
    persistence/
    k8s_live/
    prometheus_live/
    security/

  concurrency/
    task_leases/
    idempotency/
    sse/
    alerts/

  benchmark/
    contracts/
    scorers/
    regression/

  load/
    locustfile.py
```

建议 Marker：

```text
unit
integration
concurrency
benchmark
k8s_live
security
slow
```

现有 `integration` 保留。迁移期未标记的平铺测试继续作为普通快速测试；启用 `--strict-markers` 后，新增 Marker 必须先注册。

共享 Fixture 只负责依赖构造和资源隔离，不隐藏关键 Arrange/Act/Assert。Fake LLM 返回有序结构化响应并记录调用参数；不同测试不得共享可变 Agent、Session 或 Task。

### B.4 单元测试规范

单元测试覆盖纯逻辑和单组件契约：

- `ActionDecision`、`OpsTaskState`、`Evidence`、`ToolSpecV2` Schema、序列化和非法输入。
- `PatternPolicy` 基于 Task Type、Risk、Budget、Capability 的确定性选择。
- Context Reducer 的日志折叠、Stack Fingerprint、错误码保留、Evidence 外置和 Token Budget。
- Workflow 合法/非法状态迁移、预算耗尽、取消、等待输入和等待审批。
- ToolRuntime 的 Schema、Scope/RBAC、有效风险、超时分类、Retryable、脱敏和审计事件。
- OperationPlan 规范化、Hash 稳定性、Approval 绑定和有效期。
- Skill 状态机、Tenant/Scope 过滤、版本固定和召回门禁。
- Alert 标准化、Fingerprint、Workflow 匹配和状态归并。

时间、UUID、随机数和 Token 使用通过 Clock/ID/LLM Fake 注入。单元测试不以私有方法调用顺序作为主要断言。

当前全局 Coverage 底线可以保留，但：

- 新 Workflow、ToolRuntime、Approval、Tenant Repository 不得加入 omit。
- 安全关键分支必须有允许与拒绝的成对用例。
- 后续渐进启用 Branch Coverage，不因历史代码一次性阻断全部改造。

### B.5 FastAPI 与应用集成测试

现有 `TestClient(create_app(...))` 继续验证：

- 请求/响应 Schema、统一错误体和 Trace ID。
- API Key/JWT、TenantContext 与 Scope。
- Session、Task、Benchmark、Alert 和 Audit 基础路由。
- LLM Config Secret 不回显。

涉及并发、SSE、Lifespan 和后台任务时使用真实 Uvicorn 或 `httpx.AsyncClient`：

- `/healthz`、`/readyz` 和应用启停。
- Task 创建、查询、取消、补充输入与 SSE 重连。
- 客户端断开不取消已持久化 Task。
- API Response 与 Repository 的 Tenant、Task、Evidence 一致。
- Webhook 接收快速返回，诊断在后台推进。

### B.6 Redis、PostgreSQL 与恢复测试

复用现有 Redis Integration，并随 Phase 4 增加 PostgreSQL：

- TTL、Idempotency Key、短锁和事件通知。
- Task/Plan/Approval/Audit 的 Tenant Filter 与数据库约束。
- `state_version` 乐观锁。
- Lease 领取、续期、过期接管与 Fence Token。
- 事务中断后状态一致，重复消费不产生重复副作用。
- Migration 从上一发布版本升级，并验证受支持的回滚策略。
- Tombstone 删除同步清理关系数据、Vector Index、Object Artifact 和 Cache。

每个 Job 使用独立数据库 Schema、Namespace 或 Key Prefix，避免并行 CI 污染。

### B.7 Kubernetes 与 Prometheus LIVE 集成

真实集成测试只运行于专用 Cluster/Namespace，并使用最小权限 ServiceAccount：

- 每次运行创建带 Run Label 的 Namespace，并在 `finally` 清理。
- 同时校验 Cluster Fingerprint、Namespace Prefix 和 Environment Mode。
- 验证 Pod、Deployment、Event、Log、Service、Endpoint 和必要 Node 数据的归一化 Schema。
- 所有结果必须为 `data_origin=live`。
- 断连、Timeout、403、404 和对象消失返回结构化错误，结果中不得出现 Mock 标识。
- Prometheus 不可用允许产生缺少指标的 Partial 诊断，但不能伪造指标。
- S3 写集成只作用于临时资源，并验证 Plan、Approval、Idempotency、Post-condition 与 Rollback。

调度了 LIVE Job 后，依赖不可用应使 Job 失败，不能自动 Skip 后仍把发布标记为通过。

当前注入 `FakeCoreApi` 的测试保留为快速 Adapter 测试，但不能替代真实 Cluster 集成。

### B.8 并发正确性

使用 Barrier/Fake Clock 构造竞争，避免依赖随机 Sleep：

- 两个 Worker 同时领取一个 Task，只有一个获得有效 Lease。
- 旧 Lease Worker 提交结果时被 State Version/Fence Token 拒绝。
- 同一 `call_id/idempotency_key` 重复投递，只产生一次 Tool 副作用。
- Tool 成功但 Checkpoint 未保存时进程崩溃，恢复后不重复写操作。
- 两个审批人并发处理同一 Approval，只产生一个合法终态。
- 并发激活/回滚 Skill 时 Active Pointer 原子切换。
- SSE 重连不丢失持久化事件；慢消费者不导致无界内存队列。
- 相同 Alert 重复 Webhook 只创建或关联一个有效 OpsTask。
- 多 Tenant 并发不串 Session、Evidence、Audit、Skill 或 LLM Config。

### B.9 容量与压测

复用 `scripts/load_test.py` 的 Static LLM 模式，衡量 Athena 自身 API/Workflow 开销；复用并扩展 Locust 覆盖：

```text
Task 创建与查询
SSE 订阅与重连
Alert Webhook Burst
只读 K8s 诊断
Environment 健康查询
审计和告警列表
```

每个虚拟用户使用独立 Session/Task，避免共享 Session 掩盖竞争或制造非真实热点。

采集：

```text
acceptance/completion/error rate
P50/P95/P99 latency
first-event latency and SSE lag
queue/lease/task duration
idempotency duplicate suppression
DB/Redis/HTTP pool saturation
CPU/memory/file descriptors
LLM calls/tokens and Tool latency
```

负载强度、持续时间和门槛由参数与 Baseline 文件传入。报告输出机器可读 JSON 和 Markdown，不把脚本中的固定静态结论作为发布证明。

### B.10 Benchmark 在测试体系中的位置

Benchmark 使用 A 章的 LIVE 故障，对 `legacy_react` 与 `policy_workflow` 执行配对比较：

- 固定代码、模型、Environment、Case、Tool/Skill 和 Scorer 版本。
- 两边读取等价真实故障状态，不使用 Mock。
- 每个结论引用 LIVE Evidence。
- 关键词/Embedding/LLM Judge 仅辅助，不能覆盖确定性安全和根因校验。
- 原始 Score、Evidence Hash、Trace、Usage 和环境元数据作为 CI Artifact。
- PR 只运行 Schema/Scorer 快速回归；完整 LIVE Suite 用于 Nightly/Release。

安全使用绝对门禁，质量使用批准 Baseline 的相对回归，成本使用版本化预算。

### B.11 K8s Tool 专项约束

必须覆盖：

- 输入 Schema、SDK 归一化、空结果、分页/截断、Timeout、403、404 和 API Server 不可用。
- Cluster/Namespace/Resource Scope 的允许和拒绝边界。
- `evidence_refs`、`error_code`、`retryable` 和脱敏输出。
- LIVE Fail-Closed；只有显式 MOCK 才能产生 Mock Evidence。
- Mock Evidence 不进入 LIVE Task 和生产 Skill 样本。
- S3 Tool 验证不可变 Plan、Approval、幂等、前后置条件和审计。
- S5 在任何配置下永久拒绝。

### B.12 推理引擎与 Context 专项约束

使用 Scripted LLM 覆盖：

- 合法/未知 Action、非法 JSON、Schema 不匹配、缺参数和模型 Timeout。
- 重复 Action、无进展循环、预算耗尽、取消和最大步数。
- Tool 不可用、Evidence 冲突、最多一次 Reflection 和规则降级。
- 每轮只暴露 Capability 允许的 Tool。
- 模型输出不能绕过 Scope、Risk 和 Approval。
- 大日志外置后 Prompt 只保留摘要与 Evidence ID。
- 压缩后仍保留原始目标、Tenant Policy 和安全约束。
- Trace 不保存完整 Thought、Secret 和未脱敏 Prompt。

断言重点是状态、证据和边界，不是“回答是否看起来聪明”。

### B.13 Skill 自进化专项约束

当前 GEPA 测试只证明组件骨架可运行。生产闭环增加：

- 失败、未验证、越权或含 Secret 的 Trace 不能生成可发布候选。
- Draft/Evaluating/Review Pending 不可被生产召回。
- Tenant、Environment、Capability、Risk Filter 先于语义排序。
- Benchmark 安全失败或 Tool 版本不兼容时不能激活。
- Task 固定 `skill_version_id`，新版本不影响运行中任务。
- Rollback 原子切换 Active Pointer 并审计。
- Utility 仅在确定性验证成功或人工采纳后提升。
- 恶意日志/知识文本不能借 Skill 提升权限。

自动生成 Script/代码只能作为待评审制品，不能进入运行测试路径。

### B.14 Workflow 专项约束

参数化覆盖：

- 所有合法和非法状态迁移。
- Prepare、Decide、Execute、Reduce、Complete 各阶段异常。
- WAITING_INPUT、WAITING_APPROVAL、取消、Timeout 和恢复。
- Checkpoint、Lease、乐观锁和 At-least-once 下的幂等副作用。
- PatternPolicy 选择持久化，恢复后 Execution Profile 不漂移。
- LIVE/REPLAY/MOCK 创建后固定，运行中不切换或混合 Evidence。
- API、Worker 和前端断线后仍能查询一致终态。

### B.15 Approval 与安全专项约束

Phase 3 后以下均为发布阻断项：

- Approval 绑定 `tenant_id + user_id + plan_id + plan_hash + expiry`。
- 修改目标、参数、Risk、Dry Run、Verification 或 Rollback 后旧 Approval 失效。
- 过期、拒绝、撤销、错误 Tenant/Scope 和重复使用均不能执行。
- 资源版本或前置条件变化后必须重新生成 Plan。
- 同一 Plan 并发执行只产生一次副作用。
- `confirmed=true` 无法绕过服务端 Approval。
- S4 在 V1 默认拒绝，S5 永久拒绝。
- Audit Store 不可用时写操作 Fail-Closed。
- API、Log、Trace、Evidence 和 Audit 均无 Secret 明文。

### B.16 Alertmanager 专项测试

- 合法 Alertmanager Envelope、批量 Payload、仅 Demo 允许的简化 Payload、非法 JSON、超大 Payload 和未知字段。
- V1 Integration Token 正确映射 Tenant/Environment；代理层 mTLS 作为可选增强，Label 不能冒充 Tenant。
- Fingerprint 稳定性、重复 Firing、Resolved、乱序和重复投递。
- 告警风暴下的 Group/Coalesce、Rate Limit 和 Task 去重。
- Durable Accept 后返回 202；持久化失败返回可重试错误。
- Workflow 匹配确定性；未知告警只有在 LIVE Environment、Scope 和 Capability 完整时进入有界只读诊断，否则进入 `waiting/blocked`。
- LLM、K8s、Prometheus、Audit 故障符合 Fallback Matrix。
- 自动告警路径不得执行未审批写操作。
- 前端 Alert/Task/Evidence/Audit 状态一致。

### B.17 CI 流水线

当前仓库没有 CI Workflow。第一版新增一个简洁 GitHub Actions Workflow：

```text
PR / Push
  quality
    black --check
    isort --check-only
    mypy

  unit_api
    pytest 快速测试
    coverage + JUnit Artifact

  integration
    Redis Service Container
    FastAPI/Redis Integration

Scheduled / Release / Manual
  target_runtime_security
    Linux Container Sandbox/Resource Limit

  live_k8s
    受控 Runner 的 LIVE Integration

  concurrency_load
    并发正确性与容量

  benchmark
    新旧链路 LIVE 配对报告
```

Phase 4 引入 PostgreSQL 后再为 Integration Job 增加 PostgreSQL 与 Migration，不先创建空 Job。

CI Artifact：

```text
JUnit XML
Coverage XML/HTML
sanitized Decision/Tool/Audit Trace
load metrics JSON/Markdown
benchmark raw JSON/comparison report
K8s resource manifest and Evidence hash
```

发布规则：

- PR 通过 Quality、快速测试和基础 Integration。
- LIVE、并发和完整 Benchmark 在受保护环境定时或候选版本运行。
- Release Candidate 关联同一 Commit 的目标运行时安全、LIVE 和 Benchmark 报告。
- 必需 Job 的 `not_run/skipped` 不等于 Passed。

### B.18 最简落地顺序

1. 注册 Marker，新增最小 CI：格式、类型、普通 Pytest、Redis Integration。
2. 在目标 Linux 容器验证 Sandbox，同时保留 Windows 兼容测试。
3. 随 Phase 0/1 增加 Action/Task/Evidence/Pattern/Context/CrashLoop 测试。
4. 把“real 失败回 Mock”测试迁移为显式 MOCK 与 LIVE Fail-Closed 两组。
5. 随 Phase 3 用 Plan Hash/Approval 测试替代 `confirmed=true` 安全证明。
6. 随 Phase 4 增加 PostgreSQL、Lease、多 Worker、Tenant 和恢复测试。
7. 最后启用完整 LIVE Benchmark、并发容量和发布矩阵。

测试复杂度随 Proposal Phase 增长，不先建设与当前实现脱节的测试平台。

## C. Alertmanager 告警自动闭环

### C.1 当前链路复核与迁移基线

当前仓库已经跑通 Alertmanager Webhook 到 K8s 只读诊断的 Demo 链路：

```text
POST /api/alerts/webhook
 -> AlertWebhookParser
 -> AthenaWebService.ingest_alert
 -> K8sReadOnlyDiagnoser.build_report
 -> FaultDiagnoseWorkflow.run
 -> 进程内 _alert_history
 -> HashChainAuditStore
```

可复用资产：

- `athena/api/routes/alerts.py`：现有 Webhook 与历史查询入口。
- `athena/integration/alert_webhook.py`：外部协议适配器和内部 Dataclass 的正确雏形。
- `AthenaWebService.ingest_alert()`：兼容门面与现有链路入口。
- `_select_alert_playbook()`：确定性 Workflow 匹配的首版种子。
- `K8sReadOnlyDiagnoser`：Namespace Allowlist、事件、日志、Prometheus 和结构化报告。
- `FaultDiagnoseWorkflow`：现有故障流程骨架，但不能直接视为生产 OpsTask Workflow。
- `AsyncTaskManager`、`TaskStore`：异步并发闸门和任务存储模式，目前尚未接入告警链路。
- `HashChainAuditStore`：审计链骨架与查询、校验接口。
- `tests/test_alerts_webhook.py`、`monitoring/alertmanager.yml` 和 kind 示例 Payload：回归与演示资产。

当前 `monitoring/alertmanager.yml` 的 Receiver 使用未鉴权 HTTP 地址，只能作为本地 Demo 配置，不能原样进入生产部署。

当前能力与目标之间存在以下明确差距：

| 能力 | 当前实现 | 目标约束 |
|---|---|---|
| 批量处理 | Parser 只读取 `alerts[0]` | 一次 Delivery 中每条 Alert 独立标准化、持久化和处理 |
| 身份 | Webhook 没有 Tenant、Scope 或机器身份校验 | Integration 凭证绑定 Tenant 与 Environment |
| 执行方式 | 在 FastAPI Async Route 中同步执行 K8s、Prometheus 和 Workflow | 持久化受理后返回 `202`，Worker 异步推进 OpsTask |
| 历史 | 进程内列表，最多保留最近 50 条 | PostgreSQL 持久化、Tenant 隔离、分页和保留策略 |
| 生命周期 | 只有 `processed` | 区分 firing/resolved 与处理状态，保留完整事件历史 |
| 去重 | 无 Fingerprint、重投和并发去重 | 稳定指纹、活动实例、Task 合并和幂等消费 |
| 任务关系 | 没有持久化 Alert -> OpsTask 关系 | 显式多对多 Link，支持一个任务处理同组告警 |
| Evidence | 只读报告与 `FaultDiagnoseWorkflow` 是两条独立链 | 所有结论统一引用同一 OpsTask 的 LIVE Evidence |
| 容错 | 真实 K8s 失败可能回退 Mock | LIVE Fail-Closed，故障显式标记 Partial/Failed |
| 自愈 | 仅生成建议，Sandbox Verify 不能证明真实修复 | V1 自动链路只读；变更仍走 Plan、Approval 和 Verify |

进入自动闭环前必须先修正四个行为：

1. Namespace 不在 Allowlist 时必须拒绝或进入 `blocked_scope`，不能静默替换成默认 Namespace。
2. `K8sReadOnlyClient(mode=real)` 失败时不得返回 Mock Evidence。
3. 当前 `FaultDiagnoseWorkflow` 使用的 Mock K8s/Prometheus 结果不得与 LIVE 报告合并，也不得写入生产 Knowledge/Skill 成功样本。
4. `alert.received` 的审计成功字段必须表示“是否成功受理”，不能由 `severity != critical` 推导。

现有 `200 processed` 和 `/api/alerts/history` 在迁移期保留兼容；它们不代表已经具备持久化、异步恢复或生产闭环。

### C.2 目标边界与基本原则

告警闭环负责把外部信号可靠地转化为一个受治理的只读 OpsTask：

```text
Alertmanager
 -> 机器身份认证
 -> 批量解析、脱敏和标准化
 -> 持久化 Receipt/Event
 -> HTTP 202
 -> 去重、归组和 Workflow 匹配
 -> 创建或关联 OpsTask
 -> 采集 LIVE Evidence
 -> 生成诊断与报告
 -> 更新告警实例、审计和前端
```

边界约束：

- 告警是任务触发信号，不是事实真相，也不是授权凭证。
- Label 只能提供资源定位 Hint，必须用真实云 API 校验。
- Annotation、Summary、Description 和外部 URL 都是不可信数据，不能成为 System Instruction。
- 自动触发链路在 V1 永远是只读；告警严重级别不能提升 Agent 权限。
- 后续可以生成 OperationPlan，但 S3/S4 仍需独立 Approval，S5 永久拒绝。
- LIVE Environment 失联时不得切换 Mock 或 Replay。
- 不建设 Kafka、独立告警微服务或通用事件平台；使用现有 API、OpsTask Worker、PostgreSQL、Redis 和 SSE。
- 告警归组和 Workflow 选择使用确定性代码，不让 LLM 决定路由、Tenant 或 Scope。

### C.3 Webhook 协议与机器身份

继续兼容 Alertmanager Webhook v4 风格 Envelope：

```json
{
  "version": "4",
  "groupKey": "...",
  "receiver": "athena-critical",
  "status": "firing",
  "groupLabels": {},
  "commonLabels": {},
  "commonAnnotations": {},
  "externalURL": "https://alertmanager.example",
  "alerts": [
    {
      "status": "firing",
      "labels": {},
      "annotations": {},
      "startsAt": "2026-07-11T10:00:00Z",
      "endsAt": "0001-01-01T00:00:00Z",
      "generatorURL": "https://prometheus.example/graph",
      "fingerprint": "..."
    }
  ]
}
```

边界校验：

- 使用 Pydantic 或等价 Schema 校验 Envelope 和每个 Alert Item。
- 请求体大小、Alert 数量、Label/Annotation 数量和单字段长度采用部署配置上限，不写死在业务代码。
- 非法 JSON、错误 Content-Type、超限 Payload 和空 Alerts 返回明确的 4xx。
- Envelope 合法但个别 Item 非法时，持久化该 Item 的拒绝原因并继续受理合法 Item；响应返回计数，不回显敏感内容。
- 未知字段可以保留在脱敏后的原始 Payload 中，但不能直接进入决策上下文。
- 不自动请求 `externalURL` 或 `generatorURL`，避免 SSRF。

现有鉴权代码可以复用凭证解析、错误响应和 TenantContext 思路，但 Webhook 需要在 `athena/api/auth.py` 中增加专用 Integration Principal，不能把交互式用户身份直接当作机器身份：

1. V1 优先使用 `Authorization: Bearer <integration-token>`；服务端只保存 Token Hash，并解析出 `integration_id`，不按用户 JWT 处理。
2. 自定义 `X-API-Key` 仅在所用 Alertmanager 版本或受信反向代理明确支持注入该 Header 时作为兼容方式，不能假设所有部署天然支持。
3. 生产 Integration Credential 必须显式拥有最小 `alerts:ingest` Scope，不能沿用当前未配置角色时的通配 `*`，也不能与交互式用户凭证共用。
4. 每个凭证只绑定一个 Integration Identity，并在服务端映射到 `tenant_id + environment_id + allowed_scope`。
5. 多 Environment 场景不从 Label 推断归属；映射不唯一时拒绝自动诊断并报告配置错误。
6. Kubernetes Ingress 或反向代理可以增加 mTLS，但只能把已验证的客户端身份映射为 Integration Identity，不能信任外部直接传入的身份 Header。
7. Worker 后续使用 Environment Credential 读取集群，不继承 Webhook Token 的权限。
8. 凭证支持轮换、停用和审计，前端永远不回显明文。

现有 `/api/alerts/webhook` 可以作为兼容 URL。新配置必须通过 Integration Credential 唯一解析 Environment；旧的无鉴权模式只允许显式 Demo 配置，生产 Readiness 必须失败。

受理成功响应：

```json
{
  "status": "accepted",
  "receipt_id": "alert-receipt-...",
  "received_count": 4,
  "accepted_count": 3,
  "rejected_count": 1,
  "trace_id": "..."
}
```

只有 Receipt 和标准化 Event 已经可靠持久化后才能返回 `202 Accepted`。数据库不可用时返回可重试的非 2xx，不能先返回成功再把事件只放进进程内协程。

### C.4 标准化消息模型

`AlertWebhookParser` 从 `parse()` 渐进扩展为批量解析，输出稳定的内部事件：

```text
AlertEvent
  id
  receipt_id
  integration_id
  tenant_id
  environment_id
  source = alertmanager
  source_fingerprint
  canonical_fingerprint_fields
  source_status: firing | resolved
  alert_name
  severity
  resource_hints
  labels
  annotations
  starts_at
  ends_at
  received_at
  group_key
  payload_hash
  validation_status
  rejection_reason
```

标准化规则：

- 先合并 `commonLabels`，再由单条 Alert Labels 覆盖同名字段。
- 单条 Alert 的 `status` 优先于 Envelope Status。
- 时间统一解析为 UTC；无法解析的关键时间进入 Item 校验失败，不用本机时间伪造源时间。
- Severity 归一成受控值；未知值保留原值并映射为 `unknown`，不能自动视为低风险。
- 从 `cluster`、`namespace`、`pod`、`deployment`、`statefulset`、`daemonset`、`service`、`node`、`job` 和 `instance` 提取 Resource Hint。
- Label、Annotation 和 URL 在持久化前执行 Secret/Token/PII 脱敏。
- V1 在严格大小限制下保存脱敏 JSONB 与内容哈希，不把整包 JSON 写进 Prompt、日志或 Audit Detail；只有出现大 Payload 或合规取证需求后才外置原文。
- AlertReceipt/AlertEvent 是独立的 Trigger Provenance，不是诊断 Evidence。Annotation 只可作为不可信的低优先级触发上下文；模型不得执行其中的命令或链接。

禁止在缺少 `alertname` 时默认伪造 `KubePodCrashLooping`。兼容简化 Payload 仅保留在显式 Demo 模式，生产 Alertmanager 入口必须有可验证的 Alert Name、状态和资源范围。

### C.5 Tenant、Environment 与 Scope 映射

映射顺序固定：

```text
已认证 Integration Identity
 -> Tenant
 -> 绑定的 LIVE Environment
 -> Environment Scope/Namespace Allowlist
 -> Payload Resource Hint 校验
```

规则：

- `tenant`、`environment`、`cluster` Label 不能覆盖服务端绑定。
- 生产 Integration Credential 只允许绑定 `mode=LIVE` 的 Environment；非 LIVE 绑定进入 `blocked`，记录 `BLOCKED_NON_LIVE`，不创建诊断任务。
- 显式 Demo Integration 可以绑定 MOCK，但必须使用独立凭证和配置，在响应与 UI 持续标记 MOCK，不计入发布验收，也不得进入生产 Knowledge/Skill 样本。
- Namespace 为空时，只有 Integration 明确配置唯一默认 Scope 才能补全；否则进入 `waiting`，记录 `WAITING_MAPPING`，禁止集群级宽查询。
- Namespace 或资源超出 Allowlist 时记录安全审计并进入 `blocked`，记录 `BLOCKED_SCOPE`，不能改写成另一个合法 Namespace。
- Label 中的 Pod/Workload 名称必须再次通过 K8s API 验证 UID、Owner Reference 和当前 Namespace。
- Environment 被停用或凭证轮换期间，已受理事件可以等待恢复，但不能换用其他 Environment。
- OpsTask 启动时固定 Environment、Mode、Scope 和策略快照，后续配置变化不修改运行中任务的事实边界。

### C.6 持久化受理与异步处理

受理路径只做有界工作：

```text
Authenticate
 -> Enforce Body Limit
 -> Parse and Normalize Batch
 -> Resolve Tenant/Environment
 -> Calculate Canonical Fingerprint
 -> Transactionally Persist Receipt + Events
 -> Append acceptance audit intent
 -> Return 202
```

异步处理路径：

```text
Worker 领取 Alert Event Lease
 -> 校验 Canonical Fingerprint 并更新活动实例
 -> 去重/归组
 -> 匹配 Workflow
 -> 创建或复用 OpsTask
 -> 由 WorkflowRunner 采集、分析和报告
 -> 更新 Alert Instance 与 Task Checkpoint
 -> 写 Audit/Metric
```

迁移期可以让 `AsyncTaskManager` 承担受控后台执行，并复用其 Semaphore；但它的任务和协程都在进程内，只适用于开发、演示和 Phase 1/2 内部验证。该路径必须显式返回 `durability=process_local`，不能使用 `202 Accepted` 的可靠受理语义，也不能通过生产 Readiness。现有同步兼容入口可暂时保持 `200 processed`，直到 Durable Repository 可用。

Phase 4 后由现有文档定义的 PostgreSQL Task Lease 和 Worker 领取任务。Alert Event 与 OpsTask 使用稳定 Idempotency Key；Worker 崩溃、Lease 过期和重复投递都不得产生重复任务或副作用。Redis 只做短缓存、限流和通知，不是告警事实库。

### C.7 Fingerprint、去重与归组

Payload 中的 `fingerprint` 来自外部系统，只保存为 `source_fingerprint` 和关联 Hint，不能直接控制数据库唯一约束。Athena 在完成 Tenant/Environment 映射后，始终使用版本化规则计算 `canonical_fingerprint`：

```text
canonical_fingerprint = hash(
  alert_name + namespace + normalized_resource_identity + stable_labels
)
```

最终去重键：

```text
tenant_id + environment_id + source + canonical_fingerprint
```

Canonical Fingerprint 不使用 Annotation、接收时间或随机字段。算法版本随 Event 保存，升级算法不能悄悄改变历史身份。`source_fingerprint` 仅用于来源追踪；同一 Integration 中若一个 Source Fingerprint 异常地对应多个 Canonical Identity，则记录 Metric/Audit Detail，但不接受外部值覆盖系统键。

V1 只实现精确去重，不做跨 Fingerprint 的智能事件关联：

- `AlertInstance` 聚合同一告警的 firing、repeat 和 resolved 生命周期。
- 一个 AlertInstance 同时最多关联一个活动 OpsTask；OpsTask 使用 `trigger_type=alertmanager + trigger_ref=alert_instance_id` 建立关系。
- Alertmanager `groupKey` 是归组 Hint，不是 Tenant 边界，也不能单独作为幂等键。
- 重复 firing 更新 `last_seen_at`、`repeat_count` 和最新 Labels，不重复创建活动任务。
- 已完成任务后仍持续 firing，是否重新诊断由 Evidence Freshness、上次结果和租户重诊策略决定，不在代码中写死固定间隔。
- 并发收到相同 Event 时，通过数据库唯一约束和事务只创建一个活动实例及一个有效任务。
- `groupKey` 首期只用于列表归组、重复事件 Coalesce 和审计关联；只有真实数据证明跨告警合并能稳定减少噪声且不损失根因后，再增加多实例任务关联。

### C.8 firing、resolved 与乱序事件

Alert 只持久化来源生命周期和乱序标记，诊断状态不复制一套 OpsTask 状态机：

```text
source_status:
  firing | resolved

ordering_status:
  normal | out_of_order

handling_status（查询时生成）:
  未创建任务 -> received | waiting | blocked + reason_code
  已创建任务 -> 映射关联 OpsTask 的 status + phase
```

“诊断成功”不等于“告警已经恢复”。

处理规则：

- firing 首次出现时打开 AlertInstance；重复 firing 只更新实例并按策略补充 Evidence。
- resolved 关闭活动实例并记录 `resolved_at`，但不删除 Event、Task、Evidence 或报告。
- queued Task 遇到 resolved 可以取消并记录 `ALERT_RESOLVED_BEFORE_START`；正在采集的只读 Task 可以完成当前快照，但报告必须注明告警已恢复。
- resolved 后不得继续生成新的处置计划；已有计划执行前必须重新验证状态和 Plan Preconditions。
- 使用源时间、接收时间和当前实例版本处理乱序。旧 firing 晚于 resolved 到达时保存 `ordering_status=out_of_order` 和 `OUT_OF_ORDER`，不能无条件重开实例。
- 重复 resolved 是幂等更新，不重复关闭或重复创建报告。
- Alertmanager `send_resolved: true` 是完整生命周期的部署前置检查；关闭时前端必须明确只能展示 firing 历史，不能推断已恢复。

### C.9 OpsTask 创建与 Workflow 匹配

告警不会直接调用 Agent。标准化 Event 先生成受限任务输入：

```text
trigger_type = alertmanager
trigger_ref = alert_event_id
environment_id = 已绑定 Environment
objective = 由结构化 Alert Name + Resource Hint 生成
constraints.readonly = true
constraints.scope = 已校验 Namespace/Resource
environment_mode = LIVE
required_environment_evidence_origin = live
```

Annotation 不直接拼成 Objective。它可以通过 `trigger_event_id` 作为带来源标记的辅助上下文，由 ContextManager 在低优先级、不可信数据区按需加载，但不写入 Evidence Store。

`_select_alert_playbook()` 的现有规则迁移为版本化、确定性的 Workflow Matcher：

| 告警信号示例 | 首选 Workflow | 当前兼容路径 |
|---|---|---|
| CrashLoop、容器重启 | CrashLoop 只读诊断 | `K8sReadOnlyDiagnoser` + 现有 CrashLoop Playbook |
| Pending、FailedScheduling | PodPending 只读诊断 | K8s Namespace 规则诊断 |
| ImagePull、ErrImagePull | ImagePull 只读诊断 | K8s Image Finding |
| Service 无 Endpoint、5xx | Service Reachability 只读诊断 | Service/Endpoint Finding |
| CPU、Memory、OOM、SLO | 资源与指标联合诊断 | K8s + 可用的 Prometheus Evidence |
| 未知但 Scope 完整的 K8s 告警 | 通用有界只读诊断 | `bounded_policy_loop` |
| Environment 或 Scope 不完整 | 不运行 Agent | `waiting/blocked + reason_code` |

匹配输入只允许 Alert Name、受控 Labels、Environment Capability 和 Scope；Severity 只影响队列优先级和 UI，不影响权限。每次匹配保存 `matcher_version + workflow_type + reason_code`，保证可复现。

未知告警不能进入拥有全部 Tool 的通用 ReAct。Fallback 只暴露当前 Environment 和 Namespace 内的只读 Capability，并受步骤、时间、Token 和 Evidence 预算限制。

### C.10 LIVE Evidence 与诊断报告

Workflow 使用 Alert 的时间与资源 Hint 构造采集窗口：

```text
Environment/Scope 验证
 -> Workload/Pod 当前状态
 -> Owner Reference 和 Container State
 -> Kubernetes Events
 -> 有界容器日志
 -> 可用时查询 Prometheus
 -> Reducer 生成 Facts
 -> Policy/规则生成 Hypotheses 和 Conclusion
 -> 结构化报告
```

证据约束：

- 所有集群事实来自绑定 Environment 的实时 K8s/Prometheus 调用，标记 `data_origin=live`。
- Alert Payload 是独立的 Trigger Provenance，不计入诊断 Evidence，更不能证明 Pod 当前仍异常。
- 原始 Evidence 外置；Prompt 只接收脱敏摘要、关键字段和 Evidence ID。
- 每个 Finding、Root Cause 和建议都引用 Evidence ID；证据不足时返回 Unknown/Partial。
- K8s 对象状态与日志观测时间必须保留，避免用过期 Evidence 证明当前故障。
- 只读报告与 Agent Decision Trace 使用同一个 task_id，不再并行生成两份互不引用的“诊断成功”。
- Mock、Replay 或来源未知的环境 Evidence 不进入 LIVE Task；若 Runtime 检测到该结果，则拒绝保存该 Evidence，将任务置为 Failed/Blocked，记录 `NON_LIVE_EVIDENCE_IN_LIVE_TASK`，并禁止沉淀 Skill。

报告至少包含：

```text
alert instance and task identity
environment, namespace and resource
source status and processing status
observed facts with timestamps
hypotheses and confidence
evidence references
diagnosis mode: policy | rules_only | partial
missing evidence
recommended next actions
workflow/matcher/tool/skill versions
```

### C.11 自动化安全边界

V1 告警闭环到“诊断、归档和建议”为止：

- 允许自动执行 S0/S1 只读 Tool。
- S2 只允许扩大受控采集或生成 Dry Run/建议。
- 可以为 S3 生成不可变 OperationPlan，但不能由 Webhook 身份批准。
- Approval 必须来自有权限的人类用户，并绑定 Plan Hash 与有效期。
- 告警重复、Severity 升高或 Annotation 中出现“立即重启”都不能跳过审批。
- 告警 resolved、资源版本变化或前置条件变化后，旧 Plan 必须重新验证或失效。
- S4 在 V1 关闭，S5 永久硬拦截。

Alertmanager Integration Identity 只有 `alerts:ingest`，不授予 `plan:approve`、`operation:execute` 或 Skill 管理权限。

### C.12 重试、背压与降级

Webhook 重试与内部诊断重试必须分开：

- 持久化受理前失败：返回非 2xx，让 Alertmanager 按其策略重投。
- 持久化受理后失败：保持 `202` 语义，由 Worker 根据错误类型和 `next_run_at` 重试，避免让同一 Delivery 重复进入入口。
- 重试采用配置化指数退避、抖动、最大尝试和总时限，不在 Workflow 内散落固定数值。
- 不可重试的 Scope、Schema、权限和非 LIVE 配置错误直接进入 `blocked/failed`，等待配置修复或人工重跑。
- 告警风暴时按 Integration、Tenant 和 Environment 限流，并优先 Coalesce 重复 Event；不能通过丢弃 resolved 或 critical Event 降压。
- 容量门禁只能在受理事务前拒绝并返回明确过载状态；一旦 Receipt/Event 持久化成功就必须返回 202，后续只做内部排队和背压，不能再诱发 Alertmanager 重投。

| 故障 | 受理行为 | 后台行为 | 禁止行为 |
|---|---|---|---|
| PostgreSQL 不可用 | 返回可重试 5xx | 无 | 返回 202 或仅写内存 |
| Worker 暂停/崩溃 | 已持久化后仍返回 202 | Lease 过期后接管 | 重复创建 OpsTask |
| K8s LIVE 失联 | 事件已受理 | 重试后 Partial/Failed，明确实时事实不可用 | 切 Mock、生成变更计划 |
| Prometheus 不可用 | 事件已受理 | 继续 K8s 诊断并标指标缺失 | 伪造指标 |
| LLM 不可用 | 事件已受理 | 已知 Workflow 使用规则诊断；未知场景 Escalate | 用模板假装已确认根因 |
| Skill 无匹配 | 事件已受理 | 使用原 Workflow 的有界只读路径 | 扩大 Tool 或权限 |
| Audit Projection 不可用 | Receipt 保留审计意图 | 按租户策略等待或仅继续允许的只读路径 | 执行任何写操作 |
| Secret/Environment 不可用 | 事件可归档 | Task 等待或失败 | 换用其他租户/环境凭证 |

耗尽内部重试后不删除事件。标记最终错误码、最近错误、尝试次数和人工处置建议；只有出现真实运营需求后再增加独立 Dead Letter 管理界面，不先建设新的消息系统。

### C.13 持久化模型与事务边界

在主文档 Phase 4 引入的 PostgreSQL 中增加最小关系模型，不单独部署告警数据库：

```text
alert_receipts
  id, integration_id, tenant_id, environment_id
  payload_hash, sanitized_payload_json, payload_ref(nullable), received_at
  received_count, accepted_count, rejected_count
  receipt_status, trace_id

alert_events
  id, receipt_id, tenant_id, environment_id
  source_fingerprint, canonical_fingerprint, fingerprint_version
  source_status, ordering_status, alert_name, severity
  labels_json, annotations_json, resource_hints_json
  starts_at, ends_at, received_at
  validation_status, error_code

alert_instances
  id, tenant_id, environment_id, canonical_fingerprint
  source_status, first_seen_at, last_seen_at, resolved_at
  repeat_count, latest_event_id, state_version

ops_tasks
  增加 trigger_type, trigger_ref
  活动状态下对 tenant_id + trigger_type + trigger_ref 保证唯一
```

设计约束：

- Repository 方法显式接收 `TenantContext`；唯一约束包含 Tenant 与 Environment。
- Receipt 和 Event 的源 Payload 字段不可变；Instance 是带 `state_version` 的当前投影，Event 的消费结果作为独立处理元数据更新。
- T1 入口事务原子写入 Receipt、Event 和受理审计意图；T2 Worker 事务原子更新 Instance、创建或复用 OpsTask 并保存消费 Checkpoint；后续 Task/Evidence/报告沿用 WorkflowRunner 的幂等 Checkpoint。
- OpsTask 创建使用稳定幂等键；事务中断后重试不会出现孤立重复任务。
- V1 在配置化大小限制内保存脱敏 JSONB 与哈希；只有大 Payload、合规取证或既有对象存储可用时才增加 `payload_ref`，不把跨存储事务作为首版前置条件。大型原始 Evidence 仍按正文进入 Evidence Store。
- 原始 Receipt 使用较短保留期，规范化 Event、Task 引用、报告和 Audit 按租户审计策略保留。
- resolved 只关闭实例，不物理删除历史；Legal Hold 沿用主文档规则。

迁移前的 `_alert_history` 只作为兼容读模型，不能继续承担生产事实存储。`TaskStore` 的 Cache TTL 可以承接过渡期任务摘要，但不能代替上述事务关系。

### C.14 审计与可观测性

高频生命周期先写 Domain/Task Event：

```text
alert.event.normalized
alert.event.duplicate
alert.lifecycle.changed
alert.diagnosis.completed
alert.diagnosis.failed
```

Hash Chain 安全审计只记录身份、Scope、任务治理和关键失败，避免告警风暴把审计链当消息总线：

```text
alert.webhook.accepted
alert.webhook.rejected
alert.scope.blocked
alert.task.created
alert.workflow.matched
alert.processing.critical_failed
operation.plan/approval/execute（未来写链路）
```

每条安全审计记录 `tenant_id`、Integration Actor、Environment、Receipt/Event/Instance/Task ID、Trace ID、结果和脱敏 Detail。认证失败且无法确定 Tenant 的请求进入安全访问日志，不伪造 Tenant Audit Event。

当前 `HashChainAuditStore` 可继续作为骨架，但正式发布前还需验证并发 Append 原子性、序列缺口检测、后端失败策略和跨 Tenant 查询边界；`_audit()` 不能无条件吞掉关键写操作审计失败。Webhook 收据本身是持久化事实来源，Hash Chain 是关联的防篡改投影，不用双写成功假设掩盖丢事件。

指标：

```text
webhook requests / accepted / rejected
alerts normalized / invalid / duplicate / resolved
active alert instances by severity and environment
receipt persistence latency
queue lag and worker attempt count
time to OpsTask / first LIVE Evidence / diagnosis
workflow match and unknown match rate
diagnosis success / partial / failed
non-live evidence violation
Alert -> Task -> Evidence -> Audit linkage completeness
```

Prometheus Label 只使用 `status`、`severity`、`workflow`、`error_code` 等有限枚举。Tenant、Environment、Receipt、Instance 和 Task ID 放在 Trace/Database 中，禁止作为高基数 Metrics Label。

Trace 关联链：

```text
trace_id
  -> receipt_id
  -> alert_event_id
  -> alert_instance_id
  -> task_id
  -> decision/tool/evidence/audit ids
```

不保存完整 Thought、未脱敏 Prompt、认证 Header 或原始 Secret。

### C.15 查询 API 与前端联动

在现有 `alerts.py` 路由内渐进增加：

```text
POST /api/alerts/webhook                 机器入口，持久化后返回 202
GET  /api/alerts                         用户入口，Tenant 过滤和游标分页
GET  /api/alerts/{instance_id}           告警实例、事件时间线和诊断摘要
GET  /api/alerts/{instance_id}/events    firing/repeat/resolved 历史
POST /api/alerts/{instance_id}/diagnose  受权用户手工重跑只读诊断
```

`GET /api/alerts/history` 在迁移期委托新查询并标记兼容接口。用户查询和手工重跑必须使用 `require_tenant` 与对应 Scope，不能允许调用者通过 Query 参数读取任意 Tenant。

“告警处理记录”页面字段：

```text
严重级别 | 告警名称 | 来源状态 | 处理状态 | Environment/Namespace
资源 | 首次/最近出现 | 重复次数 | 关联任务 | 诊断结果
```

筛选：

```text
Environment | Severity | firing/resolved | handling status | Task phase | time range
```

详情使用非嵌套分栏或抽屉展示：

- 告警事件时间线。
- 脱敏 Labels 与 Annotation。
- 关联 OpsTask 的阶段和最终状态。
- LIVE Evidence 引用与观测时间。
- Workflow/Matcher 版本、诊断模式和缺失证据。
- 相关 Audit Event。

操作按钮：

- `查看任务`
- `查看证据`
- `重新诊断`
- `创建后续任务`

任务进度复用 OpsTask SSE；告警列表在事件通知后重新查询事实 API，不增加第二套前端任务状态。没有实现告警确认、分派、静默和解决 API 前，不显示这些按钮；Alertmanager 的 resolved 也不能被前端手工伪造。

### C.16 代码演进规划

首期优先修改现有文件：

```text
athena/integration/alert_webhook.py
  扩展批量解析、Schema、标准化和 Canonical Fingerprint 输入字段

athena/api/routes/alerts.py
  增加机器鉴权、Scope、请求边界；Phase 4 Durable Receipt 后启用 202

athena/api/services.py
  AthenaWebService 保留兼容门面，逐步委托 OpsTaskService

athena/api/task_manager.py
  仅用于迁移期受控异步验证

athena/tools/cloud/k8s/client.py
  增加严格 LIVE Fail-Closed 策略

athena/tools/cloud/k8s/diagnose.py
  把结构化报告输出接入统一 Evidence/Task

athena/api/task_store.py
  复用任务摘要持久化模式，不承载最终 Alert 事实库
```

随主文档对应 Phase 渐进新增到既定目录，而不是新建服务：

```text
athena/application/ops_task_service.py
  接受 alert trigger，创建/复用 OpsTask

athena/agent/workflow/
  复用 CrashLoop/Pending 等类型化 Workflow

athena/api/repositories/
  增加 Alert Receipt/Event/Instance 的 PostgreSQL Repository

tests/unit/alerts/
tests/integration/api/
tests/concurrency/alerts/
```

不新建 Alert 微服务、消息队列消费者集群、规则 DSL 或告警关联图谱。只有现有 Runner/Repository 文件出现明确维护瓶颈后才进一步拆分。

### C.17 专项测试与 CI 门禁

测试门禁沿用 B 章的分阶段策略：Parser、认证和纯状态规则先进入快速测试；PostgreSQL 崩溃恢复、Worker Lease、告警风暴和完整 LIVE E2E 随 Phase 4/5 能力落地后再成为发布门禁，不提前建设空测试平台。

单元测试：

- 完整 Envelope、批量、简化 Demo Payload、未知字段、非法类型和配置化大小边界。
- `commonLabels` 合并、时间解析、Severity 归一、脱敏和 Prompt Injection 文本。
- Source Fingerprint 不可信边界、Canonical Fingerprint 版本和稳定性。
- firing/repeat/resolved、重复 resolved、乱序和状态版本冲突。
- Workflow Matcher 的确定性、Capability/Scope 检查和未知告警 Fallback。
- Namespace 越权必须拒绝，不能静默改写。

集成测试：

- 缺少或错误 Integration Credential 返回 401/403，合法身份映射正确 Tenant/Environment。
- 批次持久化成功后返回 202；数据库失败时返回可重试非 2xx。
- 受理后异步创建或复用 OpsTask，Task、Evidence、报告和 Audit ID 可追溯。
- GET 列表、详情、历史兼容接口和手工重跑遵守 Tenant/Scope。
- K8s、Prometheus、LLM 和 Audit 失败符合 C.12 Fallback Matrix。
- 所有 K8s/Prometheus 等环境观测 Evidence 必须为 live；Document 等非环境来源显式分类，Mock/Replay 环境 Evidence 为零，真实客户端失败时零 Mock 调用。

并发与压测：

- 同一 Canonical Fingerprint 并发投递只产生一个活动实例和一个有效任务。
- 不同 Tenant 的相同 Canonical Fingerprint 绝不合并。
- 告警风暴下验证 202 延迟、队列深度、Coalesce、限流和内存上限。
- Worker 在持久化、Task 创建、Evidence 保存各阶段崩溃后能够幂等恢复。
- firing/resolved 并发和乱序不会把已恢复实例错误重开。
- SSE 慢消费者不阻塞 Webhook 受理和 Worker。

LIVE 端到端：

```text
在受控 kind/Staging 集群部署真实故障
 -> Prometheus Rule 产生告警
 -> Alertmanager 发送 firing
 -> Athena 202 受理并创建 OpsTask
 -> Workflow 采集 LIVE Evidence 并完成诊断
 -> Test Harness 使用 Setup/Cleanup 身份修复或清理故障
 -> Alertmanager 发送 resolved
 -> Alert Instance 关闭且历史仍可查询
```

发布阻断条件：

- 生产模式存在无鉴权可达的 Webhook。
- 任意跨 Tenant/Environment 数据关联。
- LIVE 任务出现 Mock/Replay Evidence。
- 重复投递产生重复有效任务或写副作用。
- Annotation 能改变权限、Scope 或 Workflow 治理。
- 未审批写操作被告警链路触发。
- 已接受且创建诊断任务的 Event 无法关联 Task、Evidence 和 Audit。

这些测试接入 B 章既定的 Unit、Integration、Concurrency 和 `live_k8s` Job，不再建立一套平行 CI。只有比较 `legacy_react` 与 `policy_workflow` 诊断质量时，才把对应场景纳入 A 章 Benchmark。

### C.18 最简落地顺序

1. 修正当前 Namespace 静默替换、critical 审计语义和 LIVE 失败回 Mock；阻断 Mock Workflow 结果进入生产知识。
2. 扩展 `AlertWebhookParser` 为批量标准化，补齐 Schema、Fingerprint、firing/resolved 和脱敏测试。
3. 为 Webhook 增加 Integration Credential、`alerts:ingest` Scope、Environment 绑定和请求边界。
4. 使用 `AsyncTaskManager` 验证“非持久排队 + 后台只读诊断”，仅在 Demo/内部验证返回 `durability=process_local` 与 Task 引用；不返回 Durable `202`，并明确进程退出会丢失任务。
5. 接入 Phase 1 的 `OpsTask + WorkflowRunner + Evidence`，让 CrashLoop 告警先形成单一 LIVE 诊断链。
6. 随 Phase 4 落 PostgreSQL Receipt/Event/Instance、OpsTask Trigger、Task Lease、幂等恢复和 Tenant Repository，并在此时启用 Durable `202 Accepted`。
7. 把 `/history` 迁移为持久化查询，完成告警列表、详情、关联任务和 Evidence 联动。
8. 最后用真实 Prometheus Rule + Alertmanager + kind/Staging 跑 firing 到 resolved 的端到端门禁。

第一版可发布闭环的验收标准：

- Alertmanager 批量 Payload 不丢告警，受理结果可追踪。
- 机器身份唯一绑定 Tenant、LIVE Environment 和 Scope。
- 持久化成功才返回 202，进程重启后事件与任务可恢复。
- 重复和乱序投递不会产生重复任务或错误生命周期。
- 已知 K8s 告警能自动创建只读 OpsTask，并生成可追溯的 LIVE Evidence 报告。
- K8s 失联、LLM 不可用和指标缺失都有明确降级结果，绝不伪造 Mock 成功。
- firing、resolved、Task、Evidence 和 Audit 在存储与前端中可关联。
- 自动链路不能绕过 S0-S5、OperationPlan、Approval 和 Tenant 安全边界。
