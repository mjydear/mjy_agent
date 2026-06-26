# Demo 讲解逐字稿

## 开场 30 秒

大家好，我演示的是 Athena Agent，一个从零实现的自进化企业级智能助手。它的重点不是套一个聊天框，而是把 Agent 执行、工具、记忆、自进化、安全和可观测性做成完整闭环。今天我会演示 6 个场景：代码助手、自进化、调试面板、K8s 排障、成本优化和告警处置。

## Demo 1：代码智能助手

现在运行 `python examples/demo1_code_analysis.py`。这里 Athena 通过 ToolRegistry 调用 Tree-sitter 代码解析工具，先生成目标文件的语法结构，再给出一个单元测试草案。这个 demo 想表达的是：Agent 的代码理解能力不只靠 LLM 猜，而是先用结构化工具降低不确定性。

## Demo 2：自进化

现在运行 `python examples/demo2_self_evolution.py`。脚本会模拟一次复杂排障轨迹，ComplexityEvaluator 判断这条轨迹值得沉淀，然后 SkillGenerator 提取工具和步骤生成 Skill，最后 SkillLibrary 用相似 query 把它召回。这个闭环就是项目里的 GEPA：执行经验可以变成下次可复用的能力。

## Demo 3：调试面板

现在运行 `python examples/demo3_debugger.py`。它展示一次任务里的 trace、token 统计和断点状态。企业级 Agent 不能只给最终答案，还要能回答“它为什么这么做、哪一步失败、花了多少成本”。

## Demo 4：K8s 故障排查

现在运行 `python examples/demo4_k8s_diagnose.py`。这里用 mock 集群数据模拟 CrashLoopBackOff 等问题，诊断器输出 pod、症状和修复建议。真实接入时替换 K8s client，上层诊断流程不变。

## Demo 5：云成本优化

现在运行 `python examples/demo5_cost_optimize.py`。它复用 WebService 的 CloudOps cost 模式，扫描低利用率实例，输出闲置资源和预估节省。真实系统可以接账单 API 和利用率趋势。

## Demo 6：告警自动处置

现在运行 `python examples/demo6_alert_auto_handle.py`。Alertmanager 风格 payload 先被解析成内部告警对象，再触发故障排查 workflow。这里展示的是从告警进入系统，到上下文收集、根因分析和知识沉淀的一条闭环。

## 收尾

这个项目我最想强调三点：第一，核心 Agent 机制是自研的，关键路径可解释；第二，它有工程化边界，包括安全确认、trace、metrics 和测试；第三，它落到了云运维场景，不停留在玩具问答。后续我会继续补真实 benchmark 数据、Skill 版本治理和真实云平台接入。