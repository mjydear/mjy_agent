# Provider-backed Token Benchmark

这个 Benchmark 默认 dry-run，不会发起外部模型调用。只有显式传入 `--live`，并同时提供两个模型，才会进行真实实验。

## 离线检查

```powershell
python scripts/run_provider_benchmark.py
```

当前默认任务集有 2 个案例、4 种上下文策略和 2 个模型角色，共 16 个实验 cell；dry-run 的外部调用数必须为 0。

## 真实实验

模型名称由调用者提供，不写死过期的模型 ID；API Key 只从 Provider 环境变量读取，不写入结果文件。

```powershell
python scripts/run_provider_benchmark.py `
  --live `
  --provider litellm `
  --light-model <available-light-model> `
  --heavy-model <available-heavy-model> `
  --price-config path\to\prices.json
```

`prices.json` 使用每百万 Token 的价格快照：

```json
{
  "<available-light-model>": {
    "input_per_million": 0.0,
    "output_per_million": 0.0,
    "cached_input_per_million": 0.0
  },
  "<available-heavy-model>": {
    "input_per_million": 0.0,
    "output_per_million": 0.0,
    "cached_input_per_million": 0.0
  }
}
```

每个 cell 只调用一次模型，比较：

1. `full_history`：完整历史和 Artifact 原文。
2. `recent_window`：最近窗口和 Evidence。
3. `summary_window`：结构化摘要、最近窗口和 Evidence。
4. `four_layer`：Working、Summary、Evidence 引用和 Artifact 隔离。

结果记录 Provider 返回的输入/输出/缓存/总 Token、费用、延迟、JSON 格式通过率和 Evidence 保留率，并按实际模型名和策略聚合。

离线 deterministic Benchmark 不得替代真实账单数据；只有 `--live` 结果可以支持 Provider 级成本结论。
