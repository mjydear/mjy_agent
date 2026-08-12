# Agent Runtime 指标基准

这个基准用于生成简历和面试中可以复现的 Agent Runtime 指标。它测量执行平面和治理机制，不把没有真实 Provider 的本地 Demo 包装成生产性能。

## 一键运行

~~~powershell
python scripts/run_runtime_benchmark.py --runs 10
~~~

产物写入 artifacts/runtime-benchmarks/：

- runtime-benchmark-latest.json：机器可读原始数据。
- runtime-benchmark-latest.md：中文摘要和简历表述草稿。

artifacts/ 已加入 .gitignore。远程仓库提交测试夹具、脚本和测量口径，不提交带有本机路径、时间和临时延迟的本地产物。

## 测量范围

| 模块 | 测量内容 | 数据来源 |
| --- | --- | --- |
| 执行闭环 | 成功率、终态、Tick 数、端到端 P50/P95/Max | Runtime HTTP API 和固定本地仓库 |
| Token / 记忆 | 输入 Token、总 Token、压缩触发、目标保留、Evidence 保留 | Usage、Context Projection、TokenMeter |
| 上下文压缩 | 朴素全历史快照与结构化上下文的可控估算对比 | 同一最终快照的 A/B 估算 |
| Artifact 隔离 | 工具原文是否进入模型上下文 | artifact_content_policy=references_only 和序列化检查 |
| 工具治理 | 越权路径拒绝和工具调用结果 | tool.called、tool.rejected 事件 |
| 幂等恢复 | 工具完成后聚合提交崩溃，重试是否重复调用 | Effect Journal 崩溃窗口测试 |
| 自进化门禁 | Candidate、Replay、Shadow、Review、Handoff 结果 | Runtime Skill Learning 生命周期 |

## 自进化数据闭环

当前自进化不是“把一次回答直接写成 Skill”，而是先构造可审核的数据集：

~~~text
成功 Runtime 轨迹
  -> 已验证人工反馈
  -> Dataset Builder
  -> 脱敏与敏感信息检查
  -> Evidence 完整性门禁
  -> 轨迹指纹去重
  -> train / validation / test 确定性切分
  -> JSONL 离线评估或后续微调输入
  -> Candidate / Replay / Shadow / Review
  -> 人工创建 Skill 草稿
~~~

训练样本只保留任务目标、Evidence 引用、工具序列、根因和处置建议。完整 Artifact、原始日志和隐藏思维只留在受控运行存储中，不进入 JSONL。

这条链路的“训练感”来自数据工程和评估闭环，而不是声称当前项目已经完成大模型参数微调。当前 Benchmark 会同时验证：合格样本数、质量门禁拒绝数、重复样本去重数、数据切分数量、原始 Artifact 泄漏和隐藏思维泄漏。

## 指标口径

### Token 估算降幅

基准同时构造一个“朴素全历史上下文”：

~~~text
task + 全量事件 + Artifact 原文 + Evidence
~~~

然后与 Runtime 实际生成的结构化上下文比较：

~~~text
降幅 = 1 - optimized_context_tokens / naive_full_history_tokens
~~~

这个值是固定任务、固定 TokenMeter 和固定测试夹具下的工程估算，不能直接写成“API 账单降低多少”。只有接入真实模型 Token Usage 后，才能补充 Provider 级别的成本指标。

### Artifact 原文进入上下文比例

工具完整输出只保存为 Artifact，Context 只放 Evidence 引用、摘要和 Artifact ID。基准检查完整 Artifact 序列化内容是否进入 Context：

~~~text
raw_artifact_prompt_inclusion_rate = 进入 Context 的原文样本数 / Artifact 样本数
~~~

目标是 0%，而不是把长日志复制进每一次模型请求。

### 延迟

默认测量本地 TestClient 单进程的 HTTP 端到端时间，包含 Runtime 执行和固定测试工具，不包含外部模型网络延迟。面试时必须带上这个边界。

### 自进化

自动激活次数必须为 0。当前流程是：

~~~text
成功任务 + 已验证反馈
        -> Candidate
        -> Replay 无副作用
        -> Shadow 无副作用
        -> 人工 Review
        -> Handoff
        -> 人工创建 Skill 草稿
        -> 禁止自动激活
~~~

## 简历写法

运行基准后，把报告中的真实数值替换到下面的句式：

~~~text
设计并实现可持久化 Agent Runtime，基于固定仓库诊断任务实测成功率 X%，
端到端延迟 P95 为 Y ms。

实现四层记忆与 Token Governance，对比朴素全历史上下文，本地估算输入规模降低 Z%，
上下文压缩后目标和 Evidence 保留率均为 100%，Artifact 原文进入模型上下文比例为 0%。

实现只读 Tool Gateway 与 Effect Journal，越权路径拒绝率为 100%，
模拟提交崩溃后的重复工具调用为 0；Skill 通过 Replay、Shadow 和人工审核后仍不自动激活。
~~~

## 当前限制

- 默认是 memory-demo + deterministic-demo，不代表真实 LLM 的输出 Token、价格或网络延迟。
- 当前没有把轻量模型/重量模型分流写成性能结论；接入 Provider 后，再按相同任务集记录模型档位、路由原因、Provider Usage 和成本。
- 本地 P95 只适合描述可复现基线，不适合宣称生产吞吐。并发容量需要单独的压测任务和独立机器规格。
