# Athena 后端能力改造路线图

> 状态：待确认的实施蓝图。本文只描述设计、依赖和验收，不代表代码已经实现。
> 
> 目标：把 Athena 从“具备生产方向资产的 CloudOps Agent Demo”演进为“事件驱动、可恢复、可审计的多租户 Agent 任务平台”，并用可复现的测试、故障注入和压测证明结果。

## 1. 业务主线

唯一主线是云运维告警的诊断与受控处置，不新增电商、早餐店等无关业务模块。业务闭环如下：

```text
Alertmanager/Webhook
  -> 接入校验、规范化、指纹去重
  -> Durable Receipt 与诊断任务
  -> 队列调度与租约领取
  -> K8s/Prometheus Evidence 快照
  -> Policy Agent 分析与风险路由
  -> 报告、人工输入或审批
  -> 受控动作、结果验证与审计
  -> 知识沉淀、回放评测和指标复盘
```

每个阶段必须定义输入、输出、责任边界和失败策略。模型置信度只能影响排序或人工路由，不能授予写权限；高风险或证据冲突始终进入人工流程。

## 2. 目标运行边界

首期只保留两个可独立部署的进程：

| 部署单元 | 职责 | 不负责的事情 |
|---|---|---|
| `api` / Control Plane | 鉴权、租户边界、任务命令、幂等、Outbox、查询、SSE | 不执行长时间 Agent 任务，不持有唯一任务状态 |
| `worker` / Agent Worker | 消费任务、领取租约、执行 Workflow、写 Checkpoint、重试和恢复 | 不绕过 Repository、ToolRuntime 或审批门禁 |

基础设施职责：

- PostgreSQL 是 Task、Event、Snapshot、Idempotency、Outbox 和 Audit 的事实库。
- Redis Streams 负责短期任务传递、消费组、重试协调和 DLQ；Redis 不是事实库。
- Redis 还可承担限流、热点缓存和低延迟通知，但通知丢失后必须能从 PostgreSQL Event 回放。
- Evidence 元数据和哈希进入 PostgreSQL，大正文按保留策略进入对象存储。
- OpenTelemetry Collector 接收跨进程 Trace；Prometheus 负责服务、队列和业务指标。

在没有压测证据前，不拆成更多网络微服务，不引入 Kafka、Temporal 或通用 Workflow DSL。

## 3. 核心领域模型

第一版固定以下模型和不变量，后续代码只能通过版本化迁移演进：

| 模型 | 关键字段 | 不变量 |
|---|---|---|
| `AlertReceipt` | tenant、integration、payload_hash、received_at、traceparent | 同一租户/集成/外部事件 ID 可幂等重放 |
| `AlertInstance` | canonical_fingerprint、fingerprint_version、status、last_seen_at | 重复告警合并，不重复创建活动任务 |
| `OpsTask` | task_id、tenant、environment、status、phase、state_version | 状态只能按状态机合法转换 |
| `TaskExecutionSnapshot` | policy/config/model/tool/skill 版本、budget、scope | 一次任务执行期间配置不可漂移 |
| `TaskEvent` | tenant、task_id、sequence、type、redacted_data、trace_id | 序号单调递增，可从任意游标回放 |
| `Checkpoint` | task_id、tick、state_version、lease_generation、next_action | 只有持有有效租约的 Worker 才能提交 |
| `ToolEffect` | call_id、plan_hash、started、post_condition、result | 外部副作用至少一次投递、幂等落地 |
| `Evidence` | source、data_origin、observed_at、content_hash、content_ref | 原文受控，摘要和引用可审计 |
| `OutboxMessage` | aggregate_id、event_type、payload、published_at、attempts | 事务提交后可重试发布，不静默丢失 |

推荐的任务状态流转：

```text
queued -> running -> collect -> analyze -> report -> succeeded
                    |          |
                    v          v
                 waiting    failed
                    |
                    v
                 cancelled
```

写操作另需 `OperationPlan + Approval + plan_hash + post-condition`，默认关闭。外部系统不可参与数据库事务，不能宣称全链路 Exactly Once。

## 4. 一致性和失败语义

### 4.1 事务边界

- T1：Task、初始 Event、Idempotency Record、Outbox 在同一个数据库事务中写入。
- T2：一个 Agent tick 的状态、Checkpoint、Event、Evidence 元数据、下一条 Outbox 原子写入。
- T3：工具副作用执行前写 `ToolEffect.started`，外部调用后校验 post-condition，再写结果和 Checkpoint。
- T4：告警 Receipt、去重索引、AlertInstance 和关联 Task 的创建必须可重试且幂等。

### 4.2 队列和租约

- Redis Streams 使用 Consumer Group；消费确认发生在 Checkpoint 事务提交之后。
- Worker 崩溃由 PEL reclaim 或数据库 Lease 到期接管。
- Lease 必须带 `lease_generation`；旧 Worker 即使恢复，也不能提交过期结果。
- 重试使用指数退避、最大尝试次数和 DLQ；人工可查看并重新投递 DLQ。
- 交付语义明确记录为 `at-least-once delivery + idempotent effect`。

### 4.3 锁的边界

分布式锁只用于短临界区，例如同一 Fingerprint 合并、同一资源的诊断互斥或 Leader 选举。实现需要唯一 token、租期、续租、Lua 比较删除和 fencing；数据库唯一约束、CAS 和状态机才是正确性来源。

## 5. 分阶段实施清单

### B0：事实基线和领域契约

- 固定 Alert、Task、Snapshot、Event、Evidence、Outbox 的 schema、错误码和状态机。
- 将当前压测拆为提交、排队、Stub Agent、真实 Tool 四类 workload。
- 修正文档中已实现、实验性和规划中能力的边界。
- 交付：领域模型图、状态转移表、API 契约、测试矩阵、可重复基线报告。

### B1：Durable Alert Ingest

- 接入鉴权、租户边界、规范化、canonical fingerprint 和请求幂等。
- 事务写入 Receipt、AlertInstance、初始 OpsTask、TaskEvent 和 Outbox。
- API 返回 Durable `202`，诊断不在 HTTP 请求中同步执行。
- 交付：重复/乱序/部分字段缺失/超大 payload/数据库提交中断测试。

### B2：PostgreSQL 事实库与事务快照

- 使用 SQLAlchemy 异步 Session、Alembic 和按领域划分的 Repository。
- 实现 `TaskExecutionSnapshot`，冻结 Policy、Config、Model、Tool、Skill、Scope 和 Budget。
- 所有 Repository 方法显式接收 `TenantContext`，禁止通过裸 ID 绕过租户校验。
- 交付：事务边界测试、迁移升级测试、并发幂等测试、租户隔离测试。

### B3：Redis Streams 任务队列

- 实现生产、消费、ACK、Pending 查询、自动 reclaim、延迟重试和 DLQ 适配层。
- 消息只携带任务引用、版本和 `traceparent`，不携带完整 Prompt 或敏感 Evidence。
- Outbox Relay 采用至少一次发布，消费者按 `task_id/call_id` 幂等。
- 交付：重复投递、Redis 重启、消费者断连、DLQ 重放和队列积压测试。

### B4：Worker Lease、Checkpoint 和恢复

- API 与 Worker 使用同一镜像、不同启动命令；Worker 不依赖进程内任务字典。
- 领取任务时写 Lease、过期时间和 generation；每个 tick 持久化 Checkpoint。
- 任务取消采用状态事件，Runner 在 Action 边界停止，不强杀外部 SDK 调用。
- 交付：随机杀 Worker、租约过期接管、旧 Worker 回写拒绝、从中间 tick 恢复测试。

### B5：Agent 决策边界和受控动作

- PolicyAgent 只能输出结构化 Action；ToolRuntime 是唯一 Tool 执行入口。
- 规则优先，Evidence 完整且低风险时自动生成只读报告；中低置信或证据冲突进入人工输入/审批。
- 写能力继续保持 Feature Flag 关闭，后续必须绑定不可变 Plan Hash、Approval 和 post-condition。
- 交付：风险路由、越权 Namespace、敏感信息脱敏、工具超时和人工接管测试。

### B6：Evidence 分层和分片演进

- PostgreSQL 保存摘要、来源、哈希、观察时间和 `content_ref`；大正文进入对象存储。
- 先完成时间分区、Tenant 索引、保留策略和异步清理，再进行物理分片。
- 只有基准证明单库瓶颈后，才实现基于 Tenant/虚拟桶的 Shard Router、迁移清单和跨分片查询限制。
- 交付：哈希校验、删除/Tombstone、保留策略、热点租户和最近事件查询压测。

### B7：批量、背压和限流

- 告警规范化和指纹计算支持微批；Evidence、Embedding、非关键 Event 写入使用有界批处理。
- 每个批处理配置最大批量、最大等待时间、最大并发和部分失败拆分策略。
- Redis Lua Token Bucket 按 global、tenant、route、model 分层，按 Token 成本加权；Worker 侧也实施配额。
- 交付：慢下游、限流重试、内存稳定性、批量失败隔离和 Retry-After 测试。

### B8：全链路观测

- 统一 W3C `traceparent`、`task_id`、`run_id`、`call_id` 和 `tenant_id` 的关联规则。
- 贯通 HTTP、Outbox、Redis Consumer、Worker、Repository、LLM、Tool、K8s/Prometheus。
- 记录 queue lag、lease reclaim、retry、DLQ、checkpoint、token cost、Evidence 延迟和任务终态。
- 原始 Prompt、Secret、隐藏 Thought 不进入持久化 Trace；错误和慢请求采用更高采样率。
- 交付：跨进程 Trace 验收、脱敏验收、Trace 丢失后的 Event 回放测试。

### B9：性能、故障注入和发布证据

- 单独测量 API、Queue、Worker、Tool 和存储，不用一个混合 QPS 代表全部性能。
- 重点验证同步 Redis I/O、事件 JSON 整体读改写、N+1 查询、连接池和 SSE 轮询。
- 固定机器、配置、数据集和 commit；报告 P50/P95/P99、吞吐、错误率、队列延迟、CPU、内存和下游压力。
- 交付：改造前后对照报告、10 万轻量任务恢复实验、备份/恢复演练和安全门禁。

### B10：秋招展示和长期治理

- 提供一条命令启动 API、Worker、Redis、PostgreSQL、Collector 和演示数据。
- 提供一条三分钟演示：重复告警 -> Durable 202 -> Worker 执行 -> 杀 Worker -> 租约接管 -> Trace/报告。
- 为每个 PR 保存 `problem -> spec -> AI plan -> human review -> tests -> benchmark -> decision`。
- 将失败案例、被拒绝的过度设计和回滚方式写入 ADR，不使用无法复现的性能数字。

## 6. 总体验收门槛

在任何阶段标记完成前，必须同时满足：

- 正常路径、拒绝路径、并发路径、重试路径和恢复路径都有测试。
- 任务状态、幂等、租户边界和副作用语义有稳定错误码和 Trace 关联。
- 至少一次真实 Redis/PostgreSQL 集成验证；不能只用内存实现宣称多副本正确性。
- 性能结果带 workload 和原始数据；未实测数字不得写入 README 或简历。
- 生产 Profile 下关键依赖失败必须显式 Not Ready 或 Fail-Closed，不得静默回退 Mock。

## 7. 确认项

开始写代码前需要确认以下范围：

1. 接受 CloudOps 告警诊断作为唯一业务主线。
2. 接受先做 API + Worker 两个部署单元，不提前拆成多个微服务。
3. 接受 PostgreSQL 为事实库、Redis Streams 为至少一次任务传递。
4. 接受“幂等副作用 + Checkpoint + 对账”而不是 Exactly Once 承诺。
5. 接受物理分片以压测为前置条件，先完成分区、路由抽象和数据生命周期。
6. 接受所有新任务先通过 focused tests、故障注入和 benchmark，再进入下一阶段。

