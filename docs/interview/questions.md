# 高频面试问题与标准答案

## 架构设计

1. 为什么不用第三方 Agent 框架？
答：项目目标是掌握和展示关键机制，所以执行循环、工具协议、记忆和自进化都自己实现。这样可解释、可测试，也能按企业安全和可观测要求定制。

2. ReActAgent 的核心流程是什么？
答：用户输入进入记忆，PromptAssembler 拼接上下文和工具描述，LLM 输出结构化 decision；如果有 action 就调用 ToolRegistry，再把 Observation 写入 scratchpad；如果有 final_answer 或达到最大步数就结束。

3. 为什么工具失败不直接抛异常？
答：工具失败本身是 Agent 需要观察的信息。用 ToolResult 返回 success/error，Agent 可以把失败作为 Observation，决定重试、换工具或解释限制。

4. 为什么使用依赖注入？
答：LLM、工具、记忆都可替换，测试能注入 mock，不需要真实模型或云账号，工程上也方便扩展不同 provider。

5. Web Console 和 CLI 是否走同一套逻辑？
答：核心能力走同一服务层和工具层。Web 通过 AthenaWebService 适配 session、trace 和 CloudOps，CLI 直接创建 Agent 或调用 demo 入口。

## 可靠性机制

6. 如何防止 Agent 无限循环？
答：ReActAgent 有 max_steps；WorkingMemory 和 Tracer 都有容量边界；工具调用失败会结构化返回。

7. 高危操作如何控制？
答：工具层和服务层都保留确认边界，例如 restart instance 必须 confirmed=True，否则返回 waiting_confirmation。

8. 如何处理上下文过长？
答：WorkingMemory 按 Token 预算和重要性裁剪，长期知识放进 LongTermMemory，稳定流程沉淀为 Skill。

9. 如何保证 demo 稳定？
答：面试 demo 默认使用 mock 数据和 deterministic runner，不依赖外部网络、真实云账号或真实集群。

10. 如何定位一次执行失败？
答：TraceEvent 记录 run_id、事件名和 payload；Web Console 或 debugger 可以按 task_id 查看步骤、状态和耗时。

## 自进化原理

11. GEPA 闭环是什么？
答：执行轨迹进入复杂度评分，达到阈值后提取工具和步骤，生成标准 Skill，写入 SkillLibrary，下次相似 query 通过语义检索召回。

12. 为什么不直接让 LLM 总结 Skill？
答：第一版规则生成更稳定、可测、可解释。后续可以把渲染部分替换成 LLM，但输出仍保持同一个 Skill 数据结构。

13. 如何避免低质量 Skill 污染库？
答：用 success、复杂度、工具数量、验证结果和未来的重复检测做门禁；失败或低置信度 Skill 不自动入库。

14. Skill 和长期记忆有什么区别？
答：长期记忆偏知识事实，Skill 是可执行流程，包含工具、步骤和验证方式。

15. 自进化带来的收益如何量化？
答：比较 Skill 召回前后的 Token、步骤数、成功率和端到端耗时，必须用固定任务集实测。

## 云场景落地

16. K8s 诊断怎么做？
答：先收集 pod、node、event、usage 快照，再对 CrashLoopBackOff、ImagePullBackOff、资源压力等模式做规则诊断，输出根因和建议。

17. 成本优化怎么做？
答：当前 demo 用低 CPU 阈值识别闲置实例并估算节省；真实系统应接账单、规格、业务低峰和利用率趋势。

18. 告警处置链路是什么？
答：Alertmanager webhook → AlertWebhookParser → FaultDiagnoseWorkflow → 上下文收集 → 根因分析 → 修复建议 → 知识沉淀。

19. 为什么 CloudOps 用 mock-first？
答：面试和本地演示需要稳定；真实接入通过 client 边界替换，不影响上层服务和 Web 展示。

20. 如何接入真实 Prometheus？
答：替换指标采集 client，将 PromQL 查询结果映射到当前 monitoring metrics 数据结构即可。

## 技术难点

21. 最难的工程问题是什么？
答：让 Agent 执行、记忆、工具、安全、观测和自进化形成闭环，而不是堆功能。TraceEvent 是连接这些模块的关键抽象。

22. 如何证明不是简单 demo？
答：有单元测试、服务层、API schema、Web Console、Benchmark、CloudOps workflow 和可替换边界，demo 复用真实模块而不是孤立脚本。

23. 如果面试官质疑性能数据？
答：明确区分已实测和待补录，不编数字；展示 benchmark engine、测试口径和下一步固定任务集方案。

24. 这个项目后续怎么优化？
答：补真实 benchmark、Skill 去重与版本治理、RBAC 审批、真实 Prometheus/账单接入、流式 CloudOps 执行。

25. 你最大的收获是什么？
答：Agent 工程化的核心是控制不确定性：结构化工具、记忆治理、安全边界、观测和评测，比单次回答效果更重要。