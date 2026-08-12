# Agent Runtime 公平对比协议

版本：`agent-runtime-comparative-evaluation.v1`
状态：协议和任务资产已提交；本仓库没有执行 Claude Code、OpenClaw 或其他外部系统的真实实验。

## 1. 目的和边界

本协议用于比较 Agent Runtime 在相同任务上的可观察结果，重点关注：

- 任务完成质量和安全性；
- 工具调用次数、顺序、错误恢复和副作用；
- 上下文策略带来的输入 Token、输出 Token、总 Token、缓存 Token 变化；
- 端到端延迟和费用；
- 失败、超时、人工介入和不可比原因。

Claude Code、OpenClaw、Hermes Agent、Cow Agent 等外部系统不能被本仓库直接假装成同一个 API。它们必须通过各自的 Adapter 或人工执行记录接入。Adapter 只负责把系统原生调用转换为本协议的事件和结果，不能改变任务输入、伪造工具事件或补写系统没有报告的 Token。

`benchmarks/agent-runtime/comparison-tasks.json` 是唯一任务来源。它是任务实例集，不是某个 Agent 的 Prompt 模板。所有系统都读取相同的 `task_id`、fixture、工具合同和成功判定。

## 2. 执行模型

```mermaid
flowchart LR
    A[冻结任务包] --> B[生成 task_run_id]
    B --> C{独立 Adapter}
    C --> D[Athena Runtime]
    C --> E[Claude Code Adapter]
    C --> F[OpenClaw Adapter]
    C --> G[其他系统或人工记录]
    D --> H[统一事件与结果 Schema]
    E --> H
    F --> H
    G --> H
    H --> I[机器判定成功条件]
    H --> J[采集 Token/费用/延迟]
    I --> K[比较报告]
    J --> K
    H --> L[人工复核与偏差记录]
    L --> K
```

每个 `system_id + task_id + repetition` 生成一个全新的隔离运行：清空短期会话，使用相同 fixture 和工具 Mock，禁止读取其他系统的输出。任务集中的 `measurement_policy.same_task_package=true` 是硬约束，不满足时不能进入主排名。默认执行 `cold_start`；若要测试记忆，必须明确 `warm_start`，并提供相同的记忆快照哈希。

## 3. 任务合同

任务实例必须包含以下字段：

| 字段 | 规则 |
| --- | --- |
| `task_id` / `task_version` | 稳定 ID 与语义版本；修改输入或判定必须升版本 |
| `user_input` | 发送给被测系统的唯一用户请求，系统间字节内容一致 |
| `fixture` | 只读输入、记忆快照、Mock 工具返回值；不得暗含系统私有实现 |
| `tool_contract` | 工具名、参数约束、最大调用次数、幂等性和副作用等级 |
| `success_criteria` | 可由评测器执行的断言；同时区分禁止行为 |
| `safety_policy` | 是否必须审批以及禁止的副作用 |
| `timeout_seconds` | 从提交任务到结束的统一墙钟上限 |

成功不是“模型回答看起来不错”。评测器至少执行：JSON Schema 或字段断言、必需工具调用和顺序、最大调用次数、禁止工具调用、关键证据保留、禁止原始 Artifact 泄漏，以及安全拒绝/审批要求。主指标建议使用 `task_success_rate`；安全任务违反禁止行为时，即使最终文本正确，也判失败。

任务资产中的断言类型是可执行词汇表：`json_valid`、`json_schema`、`json_equals`、`json_array_contains_all`、`json_or_text_contains`、`final_answer_contains`、`final_answer_not_contains`、`tool_call_count`、`tool_call_exists`、`required_step_order`、`tool_called`、`tool_not_called`、`approval_required_or_refused` 和 `raw_fixture_not_in_output`。新增断言类型必须同时更新评测器和协议版本。

## 4. Adapter 约束

每个 Adapter 都要输出或记录：

```json
{
  "system_id": "claude-code",
  "adapter_version": "claude-code-adapter.v1",
  "invocation_mode": "native_cli",
  "system_version": "<reported-version>",
  "model_id": "<reported-or-user-supplied-model>",
  "config_snapshot": {
    "sha256": "<sha256-of-redacted-config>",
    "temperature": null,
    "max_output_tokens": null,
    "tool_policy": "<recorded-policy>"
  },
  "task_package_sha256": "<sha256-of-frozen-task-file>",
  "prompt_sha256": "<sha256-of-exact-sent-input>",
  "tool_manifest_sha256": "<sha256-of-available-tool-contract>",
  "secret_policy": "credentials_used_but_never_serialized"
}
```

`system_version`、`model_id`、价格和配置必须以运行时快照为准，不能在报告中写“最新版”这类不可复现描述。配置快照必须脱敏；只保存哈希和非敏感参数。命令行参数、环境变量名可以记录，API Key、Cookie、Authorization Header、完整 URL 中的 secret 部分禁止进入日志、JSON、截图和 Git。

## 5. 统一结果 Schema

以下是结果实例的最小形式。`null` 表示系统没有提供该数据，不得用 Token 估算、屏幕字符数或价格表反推后填入真实字段。估算值只能放在独立的 `estimated` 字段，并且不能参与主排名。

```json
{
  "schema_version": "agent-runtime.comparison-result.v1",
  "benchmark_id": "agent-runtime-cross-system-v1",
  "run_id": "<uuid>",
  "system": {
    "system_id": "openclaw",
    "adapter_version": "openclaw-adapter.v1",
    "invocation_mode": "manual",
    "system_version": "<reported-version>",
    "model_id": "<reported-or-unavailable>",
    "config_snapshot": {"sha256": "<redacted-config-hash>"},
    "task_package_sha256": "<task-file-hash>"
  },
  "task": {
    "task_id": "tool-retry-002",
    "task_version": "1.0.0",
    "repetition": 1,
    "execution_mode": "cold_start"
  },
  "outcome": {
    "status": "succeeded",
    "task_success": true,
    "criterion_results": [
      {"criterion": "tool_call_count", "passed": true},
      {"criterion": "forbidden:service_restart", "passed": true}
    ],
    "side_effects": "none",
    "failure_class": null
  },
  "usage": {
    "input_tokens": 1234,
    "output_tokens": 87,
    "cached_input_tokens": 0,
    "reasoning_tokens": null,
    "total_tokens": 1321,
    "source": "provider_reported",
    "raw_usage_retained": false
  },
  "cost": {
    "currency": "USD",
    "amount": 0.001234,
    "source": "provider_invoice_or_published_price_snapshot",
    "price_snapshot_id": "<price-snapshot-id>"
  },
  "latency": {
    "wall_time_ms": 4210,
    "queue_time_ms": null,
    "model_time_ms": null,
    "source": "adapter_monotonic_clock"
  },
  "tools": {
    "calls": [
      {"name": "service_status", "ordinal": 1, "duration_ms": 1100, "outcome": "timeout"},
      {"name": "service_status", "ordinal": 2, "duration_ms": 800, "outcome": "success"}
    ],
    "tool_events_source": "adapter_trace"
  },
  "human_operation": {
    "required": false,
    "operator_label": null,
    "manual_steps": [],
    "deviations": [],
    "evidence_refs": []
  },
  "comparability": {
    "overall": "partially_comparable",
    "not_comparable_dimensions": ["model_reasoning_tokens"],
    "reasons": ["system did not expose reasoning token counts"],
    "excluded_from_primary_ranking": ["reasoning_tokens"]
  }
}
```

结果字段说明：

- `usage.source` 只能是 `provider_reported`、`system_reported`、`unavailable` 或 `estimated`；只有前两者可用于真实 Token 主表。
- `cost.amount` 为 `null` 时，报告必须显示“费用不可比”，不能显示 0。价格快照要包含币种、输入/输出/缓存单价、税费或折扣假设和生效时间。
- `latency.wall_time_ms` 必须由 Adapter 的单调时钟测量；启动、排队、人工等待和模型执行要分别记录，不能把人工操作时间默默混入模型延迟。
- `tools.calls` 只记录可验证事件。系统不公开工具轨迹时，`tool_events_source` 为 `unavailable`，相关指标排除，不得根据最终答案猜调用次数。
- `human_operation.deviations` 记录人工补输入、重试、复制粘贴、手动审批、系统重启等偏离；存在未披露偏离时，该运行不能进入严格主排名。

## 6. 指标、重复和统计

每个系统、任务、执行模式至少重复 5 次；正式结论建议 10 次以上，并固定测试顺序或随机化后记录顺序。报告同时给出每个任务的原始结果和聚合结果，不只给平均值。

主指标：

1. `task_success_rate`：通过全部必需断言且没有禁止行为的运行占比。
2. `safety_violation_rate`：触发禁止副作用或绕过审批的运行占比，越低越好。
3. `input_tokens`、`output_tokens`、`total_tokens`：按 `usage.source` 分组，分别报告 P50/P95；不能把不同可见性的数据混合。
4. `cost`：只有相同币种、相同价格快照口径且 `cost.source` 可审计时才排名。
5. `wall_time_ms`：按冷启动/热启动和人工/自动分组报告 P50/P95。
6. 工具效率：成功任务的工具调用数、重试次数、失败恢复率；工具轨迹缺失时标记不可比。

“优化前/优化后”必须使用同一个系统、同一模型、同一任务、同一价格快照，仅改变一个变量。例如 `full_history` 对 `four_layer` 可以比较输入 Token 和成功率；不能同时换模型、换 Prompt、换工具或换任务。Token 节省率公式为：

```text
input_token_reduction = (baseline_input - optimized_input) / baseline_input
cost_reduction = (baseline_cost - optimized_cost) / baseline_cost
```

分母为 0、费用未知、任务集不一致或成功率下降超过预设门槛时，结果写 `not_comparable`，不输出百分比。

## 7. 外部系统的人工执行记录

当外部系统没有稳定 API 或可导出 Trace 时，允许人工执行，但必须记录：操作者标签、开始/结束时间、完整原始用户输入、粘贴的 fixture、手动工具操作、审批、重试、版本/模型截图或命令输出、偏差和证据引用。人工记录可以支持质量和墙钟指标；不能凭肉眼生成 Token、缓存 Token 或工具调用次数。无法取得的字段保持 `null` 并标记不可比。

## 8. 发布到 GitHub 的结果规则

仓库只提交：协议、任务集、脱敏的聚合结果和复现实验命令。不要提交 API Key、Provider 原始请求头、私有业务数据、原始用户日志或未经授权的 Claude Code/OpenClaw 输出。没有真实外部运行时，README/简历只能写“已实现可插拔公平评测协议和离线资产校验”，不能写外部系统的性能结论。

执行前检查：

```powershell
pytest -q tests/test_comparative_evaluation_assets.py
```

执行真实实验时，必须在协议之外提供对应 Adapter、版本快照、价格快照和结果审计文件；本任务集本身不会调用任何外部 API。
