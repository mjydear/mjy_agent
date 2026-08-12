# DeepSeek Provider 实验结果（脱敏公开版）

> 日期：2026-08-11。所有数据来自真实 DeepSeek Provider 返回的 usage；原始请求、响应和密钥未进入仓库。

## 结论摘要

- Provider A/B：16 次真实调用；四层记忆成功率与 Evidence 保留率均为 100%。
- 单轮上下文：相对 `full_history`，`deepseek/deepseek-v4-flash` 输入 Token 下降 94.55%；`deepseek/deepseek-v4-pro` 输入 Token 下降 95.90%。
- 完整 ReAct Runtime：2 个任务、4 种上下文策略、25 次真实调用；四层记忆成功率 100%，Evidence 保留率 100%。
- 复杂度路由：12 次真实调用，实际选择模型 `deepseek/deepseek-v4-pro`；4/4 个策略单元成功，本轮无失败策略单元。

## 单轮上下文 A/B

| 模型 | 输入基线 | 输入四层 | 输入下降 | 总 Token 基线 | 总 Token 四层 | 成本下降 | 成功率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `deepseek/deepseek-v4-flash` | 5617.5 | 306 | 94.55% | 5851.5 | 522.5 | 22.38% | 100% |
| `deepseek/deepseek-v4-pro` | 5538.5 | 227 | 95.90% | 6281 | 754 | 29.15% | 100% |

## 完整 Runtime / ReAct

- 实验单元：2 个任务 × 4 种策略；真实调用：25 次。
- 四层记忆成功率：100%；Evidence 保留率：100%；平均 Tick：3。

| 策略 | 输入 Token 均值 | 总 Token 均值 | 成本（USD） | 成功率 | 修复次数 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `four_layer` | 2891 | 3905 | 0.00097340 | 100% | 1 |
| `full_history` | 3642.5 | 4337.5 | 0.00098762 | 100% | 0 |
| `recent_window` | 3422.5 | 4385.5 | 0.00104098 | 100% | 1 |
| `summary_window` | 2173.5 | 2752 | 0.00058131 | 50% | 1 |

## 复杂度路由

复杂度平均评分约 `0.63`，偏好层级为 `quality`，实际选择 `heavy`。所有 Tick 使用 `deepseek/deepseek-v4-pro`。

| 策略 | 输入 Token 均值 | 总 Token 均值 | 成功率 | 修复次数 |
| --- | ---: | ---: | ---: | ---: |
| `four_layer` | 2506 | 3598 | 100% | 0 |
| `full_history` | 3519 | 4574 | 100% | 0 |
| `recent_window` | 3082 | 4665 | 100% | 0 |
| `summary_window` | 2898 | 4315 | 100% | 0 |

## 竞品对比状态

Claude Code、OpenClaw、Hermes Agent 和 Cow Agent 本轮没有写入性能结论：本机未发现可执行 CLI，且仓库尚无经过验证的 Adapter。统一对比协议和任务集已准备好，后续必须使用同一任务包、同一判定器和可审计的 Provider/System usage 才能纳入主表。

## 复现

```powershell
# 离线检查，不出网
python scripts/run_provider_benchmark.py
python scripts/run_runtime_provider_benchmark.py

# 真实 Provider：只从项目根目录 .env 读取凭证
python scripts/run_provider_benchmark.py --live `
  --provider litellm `
  --light-model deepseek/deepseek-v4-flash `
  --heavy-model deepseek/deepseek-v4-pro `
  --price-config benchmarks/agent-runtime/deepseek-prices-2026-08-11.json `
  --output artifacts/provider-benchmarks/deepseek-v4-context-YYYY-MM-DD.json

# 将本地原始结果导出为可提交的聚合报告
python scripts/export_public_benchmark_report.py
```

## 边界

成本按价格快照计算，不等同于 Provider 月度账单；P95 在单元数较少时只能作为本次运行的观测值。完整 Runtime 的真实网络延迟包含 Provider 网络耗时，不与本地 deterministic benchmark 混写。

价格来源：[DeepSeek Pricing](https://api-docs.deepseek.com/quick_start/pricing)。
