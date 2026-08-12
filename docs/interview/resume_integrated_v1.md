# Athena Agent + 实习经历整合简历底稿 V1

> 本底稿的核心内容块为：Athena 个人项目、欧摩威实习、传音实习。欧摩威与传音正文按用户原文保留，不改事实、数字、技术边界或个人职责；只调整区块顺序、标题层级和技能区排版。

## 一、推荐版式

### Agent / LLM 应用岗位版

1. 姓名、联系方式、目标岗位
2. 两行个人简介
3. 核心技能
4. 核心项目：Athena Agent
5. 实习经历：欧摩威、传音
6. 教育经历与其他信息

Athena 放在实习前，先展示 Token 治理、自进化评测闭环和 Agent 工程化；传音的 300 ms / 50% 与 P95 400 ms / 60% 应尽量留在第一页。

### 后端 / 分布式岗位版

1. 姓名、联系方式、目标岗位
2. 两行个人简介
3. 核心技能
4. 实习经历：欧摩威、传音
5. 核心项目：Athena Agent
6. 教育经历与其他信息

后端版只调整区块顺序和技能词顺序，不改两段实习正文。欧摩威优先突出可靠写入与执行链路，传音优先突出流式 RPC、长连接和协作式取消，Athena 优先突出可恢复任务、持久化和可观测性。

### ATS 排版规则

- 使用单栏纯文本，不使用表格、文本框、双栏、图片、进度条或图标。
- 公司、职位、日期各占一行，欧摩威只出现一个任职标题。
- 技术关键词同时保留中文和标准英文拼写，例如 `gRPC server-streaming`、`WebSocket`、`SHA-256`、`ContextVar`。
- Athena 的未实测数字标记为 `【目标】`；不能与实习中的实测口径混写成同一类结果。

## 二、Athena Agent 项目内容

### 正式投递通用版

**Athena Agent｜面向 CloudOps 的可治理 Agent Runtime｜个人项目（Python）**

- 从零实现有界 ReAct Tick 执行内核与 Tool Gateway，将任务建模为显式状态机；以结构化 Decision、工具权限、Checkpoint / Lease fencing、幂等 Effect Journal 管理任务续跑、重试、审计和外部工具副作用隔离。
- 构建 Token / 上下文治理链路：使用 TokenMeter、输入预算、Evidence 摘要、四层记忆、检索去重和复杂度感知模型路由；分离 Artifact 原文与 Evidence 摘要并记录 Usage，为后续固定任务集 Provider Benchmark 的成本 / 质量对比提供可复现口径。
- 实现受控 Skill Candidate 闭环：已验证结果与人工反馈经过脱敏、去重后生成 Candidate，依次通过 Replay、无副作用 Shadow、人工 Review 和 Handoff；候选默认 `activation_allowed=false`，未经审核的策略不进入在线 Skill Memory。
- 建设 API / Worker 服务边界：提供 FastAPI 任务与事件接口、SQLAlchemy Task Repository、PostgreSQL schema / 适配、Redis Streams 任务传输适配、Outbox Relay、TraceContext、Prometheus Metrics、限流 / 重试 / 熔断与 RBAC / 审计，支持 mock-first CloudOps 诊断、成本和告警场景。

### 定向标题调整

- Agent / LLM 岗位：`Athena Agent｜面向 CloudOps 的可治理 Agent Runtime｜个人项目（Python）`
- 后端 / 分布式岗位：`Athena Agent｜可恢复的 Agent 任务执行平台｜个人项目（Python）`

两种版本使用同一组项目事实，只调整标题、技能关键词顺序和区块排序；不写“企业级”“生产级”“自动训练”“自动上线”。

### Athena 后续增强指标（内部目标，未实测）

| 方向 | 目标 | 验收口径 |
| --- | --- | --- |
| Token | `total_tokens / successful_task` 降低 30%-50%；单任务成本降低 20%-40%；P95 延迟降低 20%-35% | 同一任务集、模型、Prompt 版本、价格快照和质量门禁 |
| 自进化 | 留出集成功率提升 5-10 个百分点；回归通过率不低于 95%；未经审核自动激活为 0 | 固定数据集、策略版本、Replay / Shadow 报告和回滚记录 |
| 工程化 | Trace 覆盖率不低于 95%；可重试故障恢复率不低于 90%；控制面 P95 小于 300 ms | 明确工作负载、故障注入类型、是否排除模型推理耗时 |

## 三、实习经历（原文保留）

### 欧摩威 ｜ AI 应用开发实习生 ｜ 2026.05 — 至今

**Coding Agent 工具执行与可靠性工程（Python）**
项目级 Skill 以描述定义任务流程，由团队既有 Coding Agent Runtime 调用；Python CLI 作为其下游的确定性执行侧，完成参数解析校验与多文件配置写入。个人负责执行入口收口、多文件写入可靠执行层建设、存量解析工具加固，不含 Runtime 本体开发。技术栈：Python、CLI / EntryPoint 分层、SHA-256、文件锁、run-id 上下文（进程内 ContextVar）、自动化专项用例。

- **执行入口收口 ｜ 7 个公共 Skill 收敛至单一 CLI 调用路径，Git 审计净精简 866 行**：重构前 14 个启动/环境脚本各自维护初始化与环境准备，公共行为无统一承载点；重构后 Skill 只声明 EntryPoint，执行路径统一为 CLI → EntryPoint → Command/Pipeline，参数校验与上下文注入下沉公共层并被可靠执行层复用。866 行为该重构范围内 Git 审计净精简值，收敛范围限于这 7 个公共 Skill。

- **多文件写入可靠执行层 ｜ 把「写至第 N 个文件失败后新旧配置混合」收敛为可回滚、可续跑、可取证**：主导设计并接入真实写入链路，按目标文件集整体建模——写前快照、失败按快照补偿回滚、回滚后 SHA-256 比对、按指纹 checkpoint 续跑、文件锁串行化经本 CLI 的同一目标集写入、run-id 落盘为执行证据；以 run-id 为 opt-in 总开关接入，未启用时写入行为与加保护前一致。保证止于应用层快照与补偿，不承诺数据库级一致性；可靠性专项目录现有 119 项用例。

- **存量解析工具加固 ｜ 修复 14 类解析缺口，统一 JSON 中间表示成为下游 Skill 的消费契约**：对存量复杂 Excel 解析工具做解析正确性与容错加固，非从零开发，覆盖合并单元格非锚点取值、异构表头、异常字符、缺失字段等缺口。解析专项目录现有 165 项用例。

### 传音 ｜ 后端开发实习生 ｜ 2025.10 — 2026.02

**DialogueManager 实时 Agent 对话中控（Go）**
面向智能终端的延迟敏感实时语音/文本交互中控，承载输入接入、Agent 路由、会话管理与流式输出。个人参与主调用链、团队既有 TaskFlow 的业务接入、Agent 间调用链与 S2S 音频链路。技术栈：Go、gRPC server-streaming、Protobuf、JSON-RPC 2.0、WebSocket。

- **低延迟预取链路接入 ｜ 命中时首个有效响应提前约 300 ms，预测流命中率约 50%**：基于 ASR 中间结果提前创建预测流，VAD End 后将最终识别文本与预测输入做规范化比对，一致则复用，不一致则新建正式流并即时取消冗余流。300 ms 为命中子集收益、非全量平均，归因于下游调用启动时机前移。

- **多 Agent 接入与协议适配 ｜ 多个下游 Agent 收口至同一路由入口与协议适配层**：JSON-RPC 2.0 承载文本、文件与多轮上下文的请求交互，gRPC server-streaming + Protobuf 承载增量响应，按团队既有 A2A 规范对接 Task / Message / 状态 / Artifact 语义，中控侧统一转换为客户端 WebSocket 事件流。本质为服务端统一路由与协议转换，无自治协商分工。

- **可打断流式会话 ｜ 打断 P95 约 400 ms**：按 dialogueID 复用 WebSocket 长连接，统一承载音频输入、文本/音频输出与 Interrupt；同 Dialogue 新请求抢占旧流后经 Go Context 协作式取消停止产出并回收资源，同 Dialogue 多轮重复握手与连接初始化开销降约 60%。P95 起止为收到新请求或 Interrupt → 旧流停止输出，取消不强制终止 goroutine。

**技能关键词**
语言与协议：Go / Python / gRPC server-streaming / Protobuf / JSON-RPC 2.0 / WebSocket
方向：Agent 路由与协议适配 / 流式增量响应 / 可打断会话与协作式取消 / 长连接复用与资源回收 / 执行可靠性工程
实践：执行入口收口与 CLI 分层调用链 / 写前快照与补偿回滚 / 指纹 checkpoint 续跑 / SHA-256 校验 / 文件锁 / 执行证据落盘 / Excel 解析与 JSON 中间表示 / 自动化专项用例

## 四、后续优化只作用于 Athena

1. 先做固定任务集和真实 Provider 基线，记录 Token、成本、质量、首包和端到端 P50/P95。
2. 对 Evidence 压缩、分层记忆、缓存、检索去重和模型路由做消融实验。
3. 将自进化链路固化为 Candidate 版本、Replay、Shadow、人工审核、发布与回滚状态机。
4. 对 Checkpoint / Lease / Effect Journal 做故障注入、并发压测和 Trace 取证。
5. 实测完成后，只替换 Athena 中的 `【目标】` 指标；欧摩威和传音正文保持不动。
