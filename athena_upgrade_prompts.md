# Athena Agent 企业级落地升级 · 全阶段 Copilot 开发提示词手册
> 配套《Athena Agent 企业级落地整体升级方案》，4 个阶段完整 Prompt 合集，可直接复制给 Copilot 增量开发
> 核心原则：全程自研架构，不引入 LangChain/AutoGen/LlamaIndex，分层解耦，保留 Mock 降级能力

---

## 一、整体规划概览
| 阶段 | 周期 | 核心目标 | 优先级 |
|------|------|----------|--------|
| 阶段一：生产底座加固 | 7 天 | 去 Mock 化、数据持久化、容器沙箱、可靠性验证 | P0 必做 |
| 阶段二：业务场景生产化 | 10 天 | SOP 规则兜底、运维知识库、高危回滚、量化评测 | P1 核心 |
| 阶段三：企业级工程能力升级 | 14 天 | 多租户 RBAC、无状态扩展、一键部署、企业系统集成 | P2 进阶 |
| 阶段四：长期运营迭代体系 | 按需 | 用户反馈闭环、业务收益量化、Skill 版本生态 | P4 长期 |

## 二、通用执行规则
1.  **按顺序执行**：从前到后逐步推进，每个阶段完成并通过验收后再进入下一阶段
2.  **增量开发**：所有改造基于现有 Athena 架构扩展，不推翻重写，不破坏原有 CLI/Web 功能
3.  **兼容降级**：所有真实能力都保留 Mock 降级模式，通过配置文件一键切换
4.  **分层解耦**：底层数据源、存储、沙箱全部通过 Adapter/Repository 抽象，上层业务无感知
5.  **质量要求**：完整类型注解、Google 文档字符串、单元测试覆盖正常/异常/边界场景

---

## 阶段一：生产底座加固（7天 · P0最高优先级）
```
【项目背景】
当前项目 Athena Agent 是自研云原生运维Agent，无第三方Agent框架依赖，现有架构包含自研ReAct执行器、四层记忆、GEPA自进化、基础沙箱、Web控制台、Mock版K8s/Prometheus工具。
本阶段为阶段一：生产底座加固，7天工期，核心目标消除Demo属性，完成真实基础设施对接、全量数据持久化、容器级沙箱、可靠性故障注入体系，所有改造增量扩展、不改动上层Agent业务逻辑，保留Mock降级模式保证演示稳定。

【本阶段核心任务清单】
1. 核心云运维工具去Mock化：实现K8sClientAdapter、PrometheusClientAdapter适配器层，对接本地Minikube、Docker Prometheus，通过config.yaml切换 mock/local/remote 三种数据源模式；封装官方SDK，支持Pod/Node/Event/Log/监控指标真实查询，原有工具调用接口完全不变，上层无感知。
2. 全量数据持久化层开发：新增Repository持久化抽象层，基于SQLAlchemy实现SQLite/PostgreSQL双存储后端，内存模式保留兼容；完成6张核心数据表设计（sessions、tasks、trace_events、audit_logs、memory_items、skills），实现会话、任务、执行轨迹、审计、记忆、技能落盘；支持任务断点持久化、重启恢复、分页历史查询。
3. 容器级安全沙箱重构：替换原Python语法沙箱，基于Docker SDK实现临时隔离容器执行；配置CPU/内存/执行时长限制、文件只读挂载、网络域名白名单；沙箱执行日志、资源消耗自动写入trace与审计模块，和原有PermissionManager高危确认逻辑打通。
4. 可靠性故障注入能力开发：在工具适配器、沙箱、工作流增加故障钩子，支持模拟网络超时、API报错、参数非法、资源超限；Web控制台新增任务状态标识（success/retry/failed/block/circuit_break），可视化展示重试、熔断、拦截全流程。

【统一技术约束】
1. 严格分层：接入层/编排层/能力层/数据层/基础设施层边界清晰，底层数据源、存储、沙箱全部通过适配器抽象，上层业务无侵入；
2. 异步优先，所有DB、容器、云API操作封装异步方法，全局统一异常、超时、重试、降级逻辑；
3. 完整类型注解、Google文档字符串，新增单元测试覆盖正常/异常/中断场景；
4. 配置统一从config.yaml读取，敏感信息支持环境变量覆盖，禁止硬编码集群地址、密钥；
5. 所有新增能力配套CLI/Web双入口，原有athena chat/athena web命令不受影响。

【新增/修改目录结构】
athena/
├── tools/
│   ├── builtin/k8s/client.py（重构：新增K8sClientAdapter）
│   ├── builtin/observability/prometheus.py（重构：PrometheusClientAdapter）
│   ├── sandbox.py（全量重构Docker容器沙箱）
│   └── executor.py（新增故障注入钩子）
├── infra/
│   ├── db/（新增数据库ORM模型、Repository层）
│   │   ├── __init__.py
│   │   ├── models.py
│   │   └── repository.py
│   └── docker_sdk.py（Docker容器工具封装）
├── agent/executor.py（更新断点持久化逻辑）
├── observability/tracer.py（更新任务状态标记）
├── api/routes/task.py（新增历史任务、断点恢复接口）
├── web/static/app.js（前端新增故障状态展示）
└── config.yaml（新增K8s/Prometheus/DB/沙箱配置段）

配套交付文件：
scripts/setup_local_env.py 一键搭建Minikube+Prometheus本地测试环境
examples/demo_real_k8s.py 真实集群故障排查演示脚本
docs/reliability_verify.md 可靠性验证文档

【验收标准，全部必须通过】
1. 切换local模式可真实拉取Minikube Pod异常信息、Prometheus指标；切mock模式原有Demo正常运行；
2. Web服务重启后，历史会话、任务、执行轨迹完整保留，中断任务可从checkpoint续跑；
3. Shell/Python代码在隔离临时容器执行，超限自动终止，高危操作无人工确认直接拦截并写入审计；
4. 注入网络故障可直观展示三级重试、熔断逻辑，Web界面区分各类任务状态；
5. 所有原有CLI、Web、TUI功能无回归Bug。

【注意事项】
1. 优先保证接口兼容，上层Agent、工作流代码禁止大幅修改；
2. Docker沙箱默认关闭外网、禁止挂载系统目录，安全策略收紧；
3. 数据库优先使用SQLite开箱即用，预留PostgreSQL扩展；
4. 所有故障注入功能增加全局开关，生产模式默认关闭。
```

---

## 阶段二：业务场景生产化（10天 · P1）
```
【项目背景】
Athena Agent已完成阶段一底座加固：真实K8s/Prometheus接入、持久化存储、容器沙箱、故障注入全部可用。
本阶段为阶段二：业务场景生产化，10天工期，核心解决大模型幻觉、运维场景不稳定问题，搭建SOP确定性兜底、知识库、高危回滚、量化Benchmark评估体系，复用阶段一所有底层底座，不改动持久化、沙箱、数据源适配器。

【本阶段核心任务清单】
1. K8s高频故障SOP规则引擎开发：内置10类主流故障SOP（CrashLoopBackOff、OOM、镜像拉取失败等），定义SOP匹配器、采集步骤、诊断规则、修复动作、风险分级；工作流执行时优先匹配SOP，未命中才进入通用LLM推理分支，SOP全流程写入trace链路。
2. 运维知识库完整模块：实现文档切片、向量化入库，支持手动导入故障案例；任务执行时自动召回相似历史案例作为上下文；成功排障任务自动归档为知识库样本；Web控制台提供知识库检索、质量审计页面。
3. 高危操作预检查+回滚闭环：新增OperationPlan预检查引擎，所有删除/变更类操作执行前校验副本、流量、依赖；自动生成回滚预案，执行失败支持一键回滚，所有预检查、审批、回滚步骤完整审计留痕。
4. CloudOps专属Benchmark量化评估系统：设计四类测试用例（常规查询/普通故障/复杂故障/异常容错），定义指标：任务成功率、根因准确率、SOP命中率、幻觉率、异常恢复率；支持批量自动测试、Markdown报告生成，Web面板可视化指标数据。

【统一技术约束】
1. SOP、知识库、回滚逻辑全部作为上层业务模块，不修改底层Agent执行核心；
2. 所有新增能力对接现有审计、追踪、持久化模块，每条操作生成完整trace_id；
3. SOP规则可配置、可扩展，硬编码故障类型抽离为配置常量；
4. Benchmark测试复用现有故障注入能力，自动构造异常场景；
5. 完整单元测试覆盖SOP命中/未命中、高危拦截、回滚、知识库召回场景。

【新增/修改目录结构】
athena/
├── agent/workflow/
│   └── sop_engine.py（新增SOP规则引擎）
├── memory/
│   └── knowledge_base.py（新增运维知识库）
├── tools/
│   └── operation_checker.py（新增预检查&回滚引擎）
├── evaluation/
│   ├── cloudops_bench.py（运维专属评测）
│   └── test_cases/k8s_faults/（10类故障用例）
├── observability/metrics.py（新增SOP命中率、幻觉率指标）
├── api/routes/
│   ├── knowledge.py（知识库管理接口）
│   ├── benchmark.py（评测执行接口）
│   └── rollback.py（回滚操作接口）
└── web/static（新增知识库、指标看板前端页面）

配套交付文件：
examples/demo_sop_fault.py SOP故障演示
examples/demo_rollback.py 高危操作回滚演示
docs/cloud_benchmark_report.md 评测指标说明

【验收标准，全部必须通过】
1. 10类预设故障可精准匹配对应SOP，采集、诊断、修复步骤标准化输出，无LLM自由乱推理；
2. 导入同类故障文档，Agent执行时自动召回历史解决方案；成功任务自动归档知识库；
3. 删除/修改类高危操作执行前弹出风险、预检查结果，执行失败可一键执行回滚流程；
4. 一键运行Benchmark，自动输出量化指标报告，Web面板展示各项统计数据；
5. 复用阶段一真实K8s数据源，整套SOP、知识库可对接真实集群运行。

【注意事项】
1. LLM仅作为补充总结，禁止绕过SOP直接生成破坏性操作；
2. 知识库切片、召回逻辑接入现有LongTermMemory向量库；
3. 回滚操作同样走沙箱、权限校验、审计链路，无特殊后门；
4. Benchmark指标可配置阈值，用于后续版本迭代对比。
```

---

## 阶段三：企业级工程能力升级（14天 · P2）
```
【项目背景】
Athena Agent已完成阶段一底座、阶段二场景SOP/知识库/评测体系，底层真实数据源、持久化、沙箱、场景能力全部稳定。
本阶段为阶段三：企业级工程能力升级，14天工期，面向多团队企业试用场景，开发RBAC多租户、任务异步队列、无状态水平扩展、Docker Compose/Helm一键部署、企业系统集成适配器（告警/CMDB/SSO），所有改造兼容现有单租户模式，可配置开关切换。

【本阶段核心任务清单】
1. RBAC多租户权限体系：新增租户、用户数据模型，三层角色（Viewer/Operator/Admin）；全链路tenant_id隔离，数据表、API、知识库、任务全部区分租户；Policy权限引擎拦截越权工具/操作；高危审批工作流，管理员审核变更操作。
2. 异步任务队列与无状态服务改造：后端任务Worker解耦，任务落库异步调度，支持并发、优先级、任务租约防重复执行；Web/API服务完全无状态，多实例共享数据库与向量库；新增/healthz、/readyz健康检查接口。
3. 标准化一键部署方案：编写docker-compose.yml，一键启动Agent、Web、SQLite、Milvus；提供基础Helm Chart适配K8s部署；统一环境变量配置分层，所有敏感配置外部注入，不写死代码。
4. 企业系统集成适配器：Alertmanager告警Webhook适配器、CMDB资产同步适配器、LDAP/OAuth2 SSO登录适配器；统一集成重试、限流、熔断、审计逻辑，可按需开启/关闭集成能力。

【统一技术约束】
1. 所有改造向下兼容，默认单租户模式无需改动原有使用流程；
2. 任务队列、多租户不修改原有Agent、SOP、沙箱核心业务逻辑，仅在接入层、数据层增加隔离条件；
3. 部署文件独立deploy目录，不侵入业务代码；
4. 所有第三方外部系统接入采用Adapter模式，可单独关闭不影响核心功能；
5. 完整集成测试：多实例并发、多租户数据隔离、告警自动触发排障全流程。

【新增/修改目录结构】
athena/
├── infra/
│   ├── queue.py（异步任务队列Worker）
│   └── auth/（RBAC、租户、权限引擎）
├── integration/
│   ├── alert_adapter.py（告警集成）
│   ├── cmdb_adapter.py（资产同步）
│   └── sso_adapter.py（登录集成）
├── api/
│   ├── security.py（全局租户/权限上下文注入）
│   └── routes/user.py（用户/租户管理接口）
deploy/
├── docker-compose.yml
├── .env.example
└── helm/athena-agent/（Helm部署包）

配套交付文件：
docs/deploy_guide.md 企业部署手册
docs/rbac_permission.md 权限设计文档
examples/demo_multi_tenant.py 多租户演示脚本

【验收标准，全部必须通过】
1. 多账号登录数据完全隔离，普通用户无法执行高危变更，需管理员审批；
2. 启动2个Web实例可同时处理任务，单实例关闭不中断正在执行的排障流程；
3. docker compose up一键拉起整套服务，无需手动安装依赖；
4. 接入Alertmanager告警后，自动触发Agent故障排查并返回结果；
5. 单租户模式开关开启后，原有所有Demo、命令行功能无变化。

【注意事项】
1. 多租户隔离不能遗漏知识库、Skill、审计日志等数据；
2. 异步Worker做好幂等控制，防止同一任务重复执行高危操作；
3. Helm仅基础部署模板，不做复杂集群定制，降低学习成本；
4. 所有企业集成为可选功能，关闭后项目完全独立运行。
```

---

## 阶段四：长期运营迭代体系（按需开发 · P4）
```
【项目背景】
Athena Agent已完成阶段一、二、三全部企业底座、场景、多租户部署能力，现有完整运维SOP、知识库、Benchmark评测、多租户RBAC体系。
本阶段为长期运营迭代模块，开发用户反馈闭环、业务收益量化统计、Skill版本灰度生态三大运营体系，作为企业持续迭代配套能力，不改动底层核心执行、沙箱、持久化逻辑。

【本阶段核心任务清单】
1. 用户反馈闭环系统：每条任务结果支持点赞/点踩/人工修正；反馈数据存入独立反馈表；后台复盘Worker自动归类低质量案例，生成SOP/知识库优化待办清单；Web增加反馈操作面板。
2. 业务收益量化统计模块：新增业务指标统计器，自动统计MTTR缩短时长、自动处理故障数量、月度云成本节约金额；按租户/团队生成业务价值报表，区分技术指标与业务指标。
3. Skill版本管理与灰度生态：Skill增加版本、质量分、灰度开关；支持灰度发布、异常自动降级；Benchmark自动校验新版本Skill准确率，低于阈值下线；Web提供Skill管理、版本对比页面。

【统一技术约束】
1. 全部为上层统计/管理模块，底层Agent、工作流无修改；
2. 所有统计数据持久入库，支持按月、按租户查询；
3. Skill灰度复用现有Benchmark评测能力自动校验；
4. 新增反馈、Skill管理接口接入原有RBAC权限控制。

【新增/修改目录结构】
athena/
├── learning/
│   ├── feedback.py（用户反馈采集复盘）
│   └── skill_version.py（Skill版本灰度）
├── infra/business_stat.py（业务收益统计）
├── api/routes/
│   ├── feedback.py
│   └── skill_mgmt.py
└── observability/business_metrics.py（业务指标面板）

配套交付文件：
docs/operation_iterate.md 运营迭代设计文档

【验收标准】
1. 任务执行后可提交反馈，后台自动汇总低质量案例用于优化；
2. 自动统计故障处理时长、成本节约，生成业务报表；
3. Skill支持多版本灰度上线，评测不达标自动下线；
4. 多租户环境下统计数据相互隔离。

【注意事项】
1. 不新增底层执行逻辑，仅做数据采集与统计展示；
2. Skill灰度降级不影响线上运维任务稳定性。
```
