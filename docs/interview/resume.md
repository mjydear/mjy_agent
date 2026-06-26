# 简历描述

## 精简版

- 从零实现 Athena Agent 企业级智能助手，覆盖 ReAct 执行循环、工具注册、四层记忆、GEPA 自进化、可观测性与云原生运维场景，核心链路不依赖第三方 Agent 框架。

## 详细版

- 独立设计并实现 Athena Agent：自研 ReAct Agent Core、Prompt 组装、装饰器式 Tool Registry、Token 感知 Working Memory、Long-term Memory 和 Skill Memory，实现可解释、可测试的 Agent 执行闭环。
- 构建 GEPA 自进化机制：基于执行轨迹做复杂度评分，从高价值成功路径中自动生成 Skill，并通过语义检索在后续任务中复用，降低重复推理成本。
- 落地 CloudOps 场景：实现 K8s 故障诊断、云成本优化、告警自动处置和高危操作确认机制，并提供 Web Console、Trace、Metrics 和 Benchmark 演示能力。

## 终极版

- 主导 Athena Agent 自进化企业级智能助手项目，从零实现 Agent 执行内核、工具协议、四层记忆、GEPA 自进化、企业安全沙箱和全链路可观测性平台；通过 mock-first CloudOps 设计落地 K8s CrashLoopBackOff 诊断、云成本优化、告警自动处置等 6 个可演示场景，形成“复杂任务执行 → 轨迹沉淀 → Skill 生成 → 下次召回复用”的闭环。项目核心不依赖 LangChain、AutoGen、LlamaIndex 等 Agent 框架，可在面试中完整讲清关键机制与工程取舍。

## 自我介绍

### 30 秒版

我最近做了 Athena Agent，一个从零实现的企业级智能助手。它不是简单套框架，而是自己实现 ReAct 执行循环、工具注册、四层记忆、自进化 Skill 和可观测性，并把能力落到 K8s 排障、成本优化、告警处置这些云运维场景里。这个项目最能体现我的系统设计、工程落地和 AI 应用能力。

### 2 分钟版

我的核心项目是 Athena Agent，自进化企业级智能助手。项目从 Agent 内核开始做：LLM 接入、Prompt 组装、工具协议、ReAct 循环、短期和长期记忆都自己实现，保证每个环节可解释、可测试。第二层是 GEPA 自进化闭环，会把成功执行轨迹量化成复杂度分数，再自动生成 Skill，下次相似任务直接召回复用。第三层是工程化能力，包括安全沙箱、高危操作确认、Trace、Metrics、Web Console 和 Benchmark。最后我把它落到云原生运维，做了 K8s 故障诊断、云成本优化、告警自动处置等 demo。这个项目的重点不是堆 API，而是把 Agent 从“能回答”推进到“能执行、能沉淀、能治理”。

### 5 分钟版

面试时按“为什么做、怎么做、难点、结果、后续”讲：我想验证企业 Agent 的核心不是一个聊天框，而是执行可靠性、记忆治理、安全边界和可观测性。所以 Athena 采用分层架构：接入层负责 CLI/Web/API；Agent 层实现 ReAct；工具层做注册、权限和沙箱；记忆层分 Working、Profile、Long-term、Skill；学习层做 GEPA 自进化；云运维层提供真实业务场景。最难的是把这些模块做成闭环而不是孤立功能，所以我用 TraceEvent 串起执行过程，用 ComplexityEvaluator 判断是否值得沉淀，再用 SkillGenerator 输出标准 Skill。CloudOps 采用 mock-first，确保面试和本地环境都能稳定跑通，同时保留真实云厂商 client 的替换边界。后续会重点补真实 benchmark 数据、Skill 去重、RBAC 和真实 Prometheus/账单接入。