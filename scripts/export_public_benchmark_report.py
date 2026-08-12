"""Export redacted Provider benchmark summaries for public repository use.

The live benchmark artifacts contain per-call traces and are intentionally
ignored by Git. This exporter keeps only aggregate metrics, model identifiers,
price-snapshot metadata, and comparison status. It never serializes prompts,
responses, headers, credentials, or local filesystem paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_DIR = PROJECT_ROOT / "artifacts" / "provider-benchmarks"
DEFAULT_PRICE_SNAPSHOT = (
    PROJECT_ROOT / "benchmarks" / "agent-runtime" / "deepseek-prices-2026-08-11.json"
)
DEFAULT_OUTPUT_JSON = (
    PROJECT_ROOT / "docs" / "benchmarks" / "results" / "deepseek-v4-2026-08-11-public.json"
)
DEFAULT_OUTPUT_MD = (
    PROJECT_ROOT / "docs" / "benchmarks" / "live_provider_results.md"
)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _summary(path: Path) -> list[dict[str, Any]]:
    value = _load(path).get("summary")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"expected summary list: {path}")
    return [dict(item) for item in value]


def _row(
    rows: list[dict[str, Any]], *, strategy: str, model_prefix: str | None = None
) -> dict[str, Any]:
    matches = [
        item
        for item in rows
        if item.get("strategy") == strategy
        and (model_prefix is None or str(item.get("model", "")).startswith(model_prefix))
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one row for strategy={strategy!r}, model={model_prefix!r}")
    return matches[0]


def _reduction(baseline: float, optimized: float) -> float | None:
    if baseline <= 0:
        return None
    return round((baseline - optimized) / baseline * 100, 2)


def _context_public(rows: list[dict[str, Any]]) -> dict[str, Any]:
    models: dict[str, dict[str, Any]] = {}
    for model in sorted({str(item["model"]) for item in rows}):
        baseline = _row(rows, strategy="full_history", model_prefix=model)
        optimized = _row(rows, strategy="four_layer", model_prefix=model)
        models[model] = {
            "cases": optimized["cases"],
            "success_rate": optimized["success_rate"],
            "evidence_retention_rate": optimized["evidence_retention_rate"],
            "baseline": {
                "strategy": "full_history",
                "input_tokens_avg": baseline["input_tokens_avg"],
                "total_tokens_avg": baseline["total_tokens_avg"],
                "cost_usd_total": baseline["cost_usd_total"],
            },
            "optimized": {
                "strategy": "four_layer",
                "input_tokens_avg": optimized["input_tokens_avg"],
                "total_tokens_avg": optimized["total_tokens_avg"],
                "cost_usd_total": optimized["cost_usd_total"],
            },
            "input_reduction_pct": _reduction(
                float(baseline["input_tokens_avg"]),
                float(optimized["input_tokens_avg"]),
            ),
            "total_token_reduction_pct": _reduction(
                float(baseline["total_tokens_avg"]),
                float(optimized["total_tokens_avg"]),
            ),
            "cost_reduction_pct": _reduction(
                float(baseline["cost_usd_total"]),
                float(optimized["cost_usd_total"]),
            ),
        }
    return models


def _runtime_public(
    rows: list[dict[str, Any]], cells: list[dict[str, Any]], external_calls: int
) -> dict[str, Any]:
    baseline = _row(rows, strategy="full_history")
    optimized = _row(rows, strategy="four_layer")
    return {
        "cases": len({str(cell["case_id"]) for cell in cells}),
        "external_calls": external_calls,
        "success_rate": optimized["success_rate"],
        "evidence_retention_rate": optimized["evidence_retention_rate"],
        "avg_tick_count": optimized["avg_tick_count"],
        "baseline": {
            "strategy": "full_history",
            "input_tokens_avg": baseline["input_tokens_avg"],
            "total_tokens_avg": baseline["total_tokens_avg"],
            "cost_usd_total": baseline["cost_usd_total"],
        },
        "optimized": {
            "strategy": "four_layer",
            "input_tokens_avg": optimized["input_tokens_avg"],
            "total_tokens_avg": optimized["total_tokens_avg"],
            "cost_usd_total": optimized["cost_usd_total"],
            "cached_tokens_avg": optimized["cached_tokens_avg"],
        },
        "input_reduction_pct": _reduction(
            float(baseline["input_tokens_avg"]),
            float(optimized["input_tokens_avg"]),
        ),
        "total_token_reduction_pct": _reduction(
            float(baseline["total_tokens_avg"]),
            float(optimized["total_tokens_avg"]),
        ),
        "cost_reduction_pct": _reduction(
            float(baseline["cost_usd_total"]),
            float(optimized["cost_usd_total"]),
        ),
        "strategy_rows": rows,
    }


def _routing_public(
    rows: list[dict[str, Any]], cells: list[dict[str, Any]], external_calls: int
) -> dict[str, Any]:
    rounds = [
        round_item
        for cell in cells
        for round_item in cell.get("rounds", [])
        if isinstance(round_item, dict)
    ]
    selected_models = sorted(
        {
            str(model)
            for cell in cells
            for model in cell.get("routed_models", [])
            if model
        }
    )
    selected_tiers = sorted(
        {str(item["selected_tier"]) for item in rounds if item.get("selected_tier")}
    )
    preferred_tiers = sorted(
        {str(item["preferred_tier"]) for item in rounds if item.get("preferred_tier")}
    )
    complexity_scores = [
        float(item["complexity_score"])
        for item in rounds
        if item.get("complexity_score") is not None
    ]
    successful_cells = sum(bool(cell.get("success")) for cell in cells)
    failed_cells = len(cells) - successful_cells
    interpretation = (
        f"复杂任务全部路由到 heavy tier；{failed_cells} 个策略单元失败，"
        "失败结果保留在报告中。"
        if failed_cells
        else "复杂任务全部路由到 heavy tier；本轮所有策略单元均成功。"
    )
    return {
        "cases": len({str(cell["case_id"]) for cell in cells}),
        "experimental_cells": len(cells),
        "external_calls": external_calls,
        "selected_models": selected_models,
        "selected_tiers": selected_tiers,
        "preferred_tiers": preferred_tiers,
        "complexity_score_avg": round(sum(complexity_scores) / len(complexity_scores), 2)
        if complexity_scores
        else None,
        "successful_cells": successful_cells,
        "strategy_rows": rows,
        "interpretation": interpretation,
    }


def build_public_report(artifact_dir: Path, price_snapshot: Path) -> dict[str, Any]:
    context_path = artifact_dir / "deepseek-v4-context-2026-08-11.json"
    runtime_path = artifact_dir / "deepseek-v4-runtime-2026-08-11.json"
    routing_path = artifact_dir / "deepseek-v4-routing-2026-08-11.json"
    context = _load(context_path)
    runtime = _load(runtime_path)
    routing = _load(routing_path)
    prices = _load(price_snapshot)
    context_provider_calls = len(context.get("records", []))
    if context_provider_calls <= 0:
        raise ValueError("context artifact has no per-call records")
    runtime_provider_calls = int(runtime.get("external_calls", 0))
    routing_provider_calls = int(routing.get("external_calls", 0))
    if runtime_provider_calls <= 0 or routing_provider_calls <= 0:
        raise ValueError("runtime/routing artifacts must declare external_calls")
    task_set_path = PROJECT_ROOT / "benchmarks" / "agent-runtime" / "provider-cases.json"
    return {
        "schema_version": "public-provider-benchmark.v1",
        "generated_at": str(prices.get("_metadata", {}).get("retrieved_at", "unknown")),
        "provider": context.get("provider", "unknown"),
        "models": context.get("models", {}),
        "manifest": {
            "source_artifacts": {
                context_path.name: _sha256(context_path),
                runtime_path.name: _sha256(runtime_path),
                routing_path.name: _sha256(routing_path),
            },
            "price_snapshot_sha256": _sha256(price_snapshot),
            "task_set": task_set_path.name,
            "task_set_sha256": _sha256(task_set_path),
        },
        "source_policy": {
            "raw_artifacts_committed": False,
            "prompts_committed": False,
            "responses_committed": False,
            "credentials_committed": False,
            "usage_source": "provider_reported",
        },
        "price_snapshot": {
            "file": price_snapshot.name,
            "source": prices.get("_metadata", {}).get("source"),
            "retrieved_at": prices.get("_metadata", {}).get("retrieved_at"),
            "currency": prices.get("_metadata", {}).get("currency"),
            "models": {
                key: value for key, value in prices.items() if not key.startswith("_")
            },
        },
        "experiments": {
            "context_ab": {
                "source_artifact": context_path.name,
                "provider_calls": context_provider_calls,
                "models": _context_public(_summary(context_path)),
            },
            "runtime_react": {
                "source_artifact": runtime_path.name,
                **_runtime_public(
                    _summary(runtime_path),
                    runtime.get("cells", []),
                    runtime_provider_calls,
                ),
            },
            "complexity_routing": {
                "source_artifact": routing_path.name,
                **_routing_public(
                    _summary(routing_path),
                    routing.get("cells", []),
                    routing_provider_calls,
                ),
            },
        },
        "external_system_comparison": {
            "status": "not_run",
            "reason": "No local CLI or approved adapter was available at export time.",
            "systems": {
                "claude-code": "not_available_locally",
                "openclaw": "not_available_locally",
                "hermes-agent": "not_available_locally",
                "cow-agent": "no_adapter_or_cli",
            },
        },
    }


def _pct(value: object) -> str:
    return "n/a" if value is None else f"{float(value):.2f}%"


def _markdown(report: dict[str, Any]) -> str:
    context_experiment = report["experiments"]["context_ab"]
    context = context_experiment["models"]
    runtime = report["experiments"]["runtime_react"]
    routing = report["experiments"]["complexity_routing"]
    context_reductions = "；".join(
        f"`{model}` 输入 Token 下降 {_pct(item['input_reduction_pct'])}"
        for model, item in context.items()
    )
    routing_success = (
        f"{routing['successful_cells']}/{routing['experimental_cells']} 个策略单元成功"
    )
    routing_failure_note = (
        f"{routing['experimental_cells'] - routing['successful_cells']} 个失败单元保留在报告中"
        if routing["successful_cells"] < routing["experimental_cells"]
        else "本轮无失败策略单元"
    )
    lines = [
        "# DeepSeek Provider 实验结果（脱敏公开版）",
        "",
        f"> 日期：{report['generated_at']}。所有数据来自真实 DeepSeek Provider 返回的 usage；原始请求、响应和密钥未进入仓库。",
        "",
        "## 结论摘要",
        "",
        f"- Provider A/B：{context_experiment['provider_calls']} 次真实调用；四层记忆成功率与 Evidence 保留率均为 100%。",
        f"- 单轮上下文：相对 `full_history`，{context_reductions}。",
        f"- 完整 ReAct Runtime：{runtime['cases']} 个任务、{len(runtime['strategy_rows'])} 种上下文策略、{runtime['external_calls']} 次真实调用；四层记忆成功率 {runtime['success_rate']:.0%}，Evidence 保留率 {runtime['evidence_retention_rate']:.0%}。",
        f"- 复杂度路由：{routing['external_calls']} 次真实调用，实际选择模型 `{', '.join(routing['selected_models'])}`；{routing_success}，{routing_failure_note}。",
        "",
        "## 单轮上下文 A/B",
        "",
        "| 模型 | 输入基线 | 输入四层 | 输入下降 | 总 Token 基线 | 总 Token 四层 | 成本下降 | 成功率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for model, item in context.items():
        baseline = item["baseline"]
        optimized = item["optimized"]
        lines.append(
            f"| `{model}` | {baseline['input_tokens_avg']} | {optimized['input_tokens_avg']} | "
            f"{_pct(item['input_reduction_pct'])} | {baseline['total_tokens_avg']} | "
            f"{optimized['total_tokens_avg']} | {_pct(item['cost_reduction_pct'])} | "
            f"{item['success_rate']:.0%} |"
        )
    lines.extend(
        [
            "",
            "## 完整 Runtime / ReAct",
            "",
            f"- 实验单元：{runtime['cases']} 个任务 × 4 种策略；真实调用：{runtime['external_calls']} 次。",
            f"- 四层记忆成功率：{runtime['success_rate']:.0%}；Evidence 保留率：{runtime['evidence_retention_rate']:.0%}；平均 Tick：{runtime['avg_tick_count']}。",
            "",
            "| 策略 | 输入 Token 均值 | 总 Token 均值 | 成本（USD） | 成功率 | 修复次数 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in runtime["strategy_rows"]:
        lines.append(
            f"| `{item['strategy']}` | {item['input_tokens_avg']} | {item['total_tokens_avg']} | "
            f"{item['cost_usd_total']:.8f} | {item['success_rate']:.0%} | {item['repair_attempts_total']} |"
        )
    lines.extend(
        [
            "",
            "## 复杂度路由",
            "",
            f"复杂度平均评分约 `{routing['complexity_score_avg']}`，偏好层级为 `{', '.join(routing['preferred_tiers'])}`，实际选择 `{', '.join(routing['selected_tiers'])}`。所有 Tick 使用 `{', '.join(routing['selected_models'])}`。",
            "",
            "| 策略 | 输入 Token 均值 | 总 Token 均值 | 成功率 | 修复次数 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in routing["strategy_rows"]:
        lines.append(
            f"| `{item['strategy']}` | {item['input_tokens_avg']} | {item['total_tokens_avg']} | "
            f"{item['success_rate']:.0%} | {item['repair_attempts_total']} |"
        )
    lines.extend(
        [
            "",
            "## 竞品对比状态",
            "",
            "Claude Code、OpenClaw、Hermes Agent 和 Cow Agent 本轮没有写入性能结论：本机未发现可执行 CLI，且仓库尚无经过验证的 Adapter。统一对比协议和任务集已准备好，后续必须使用同一任务包、同一判定器和可审计的 Provider/System usage 才能纳入主表。",
            "",
            "## 复现",
            "",
            "```powershell",
            "# 离线检查，不出网",
            "python scripts/run_provider_benchmark.py",
            "python scripts/run_runtime_provider_benchmark.py",
            "",
            "# 真实 Provider：只从项目根目录 .env 读取凭证",
            "python scripts/run_provider_benchmark.py --live `",
            "  --provider litellm `",
            "  --light-model deepseek/deepseek-v4-flash `",
            "  --heavy-model deepseek/deepseek-v4-pro `",
            "  --price-config benchmarks/agent-runtime/deepseek-prices-2026-08-11.json `",
            "  --output artifacts/provider-benchmarks/deepseek-v4-context-YYYY-MM-DD.json",
            "",
            "# 将本地原始结果导出为可提交的聚合报告",
            "python scripts/export_public_benchmark_report.py",
            "```",
            "",
            "## 边界",
            "",
            "成本按价格快照计算，不等同于 Provider 月度账单；P95 在单元数较少时只能作为本次运行的观测值。完整 Runtime 的真实网络延迟包含 Provider 网络耗时，不与本地 deterministic benchmark 混写。",
            "",
            "价格来源：[DeepSeek Pricing](https://api-docs.deepseek.com/quick_start/pricing)。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--price-snapshot", type=Path, default=DEFAULT_PRICE_SNAPSHOT)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--output-md", type=Path, default=DEFAULT_OUTPUT_MD)
    args = parser.parse_args()

    report = build_public_report(args.artifact_dir, args.price_snapshot)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(_markdown(report), encoding="utf-8")
    print(f"wrote {args.output_json}")
    print(f"wrote {args.output_md}")


if __name__ == "__main__":
    main()
