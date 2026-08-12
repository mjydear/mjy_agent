"""Provider-backed benchmark primitives for real Agent Runtime experiments."""

from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from athena.infra.llm import LLMClient, LLMMessage


class ContextStrategy(StrEnum):
    """Context policies compared by the provider-backed benchmark."""

    FULL_HISTORY = "full_history"
    RECENT_WINDOW = "recent_window"
    SUMMARY_WINDOW = "summary_window"
    FOUR_LAYER = "four_layer"


@dataclass(frozen=True)
class ProviderBenchmarkCase:
    """A replayable case with an explicit evidence oracle."""

    case_id: str
    goal: str
    history: tuple[str, ...]
    summary: tuple[str, ...]
    evidence: tuple[dict[str, str], ...]
    expected_evidence_ids: tuple[str, ...]
    artifact_lines: int = 0

    def artifact(self) -> str:
        return "\n".join(
            f"diagnostic line {index:04d}: case={self.case_id} "
            "irrelevant provider output retained in Artifact"
            for index in range(self.artifact_lines)
        )


@dataclass(frozen=True)
class ModelPrice:
    """Provider price snapshot supplied by the experiment owner."""

    input_per_million: float = 0.0
    output_per_million: float = 0.0
    cached_input_per_million: float | None = None

    def cost_usd(
        self, *, input_tokens: int, output_tokens: int, cached_tokens: int
    ) -> float | None:
        if self.input_per_million < 0 or self.output_per_million < 0:
            raise ValueError("model prices must be non-negative")
        if self.input_per_million == 0 and self.output_per_million == 0:
            return None
        uncached = max(0, input_tokens - cached_tokens)
        input_cost = uncached * self.input_per_million / 1_000_000
        cached_price = (
            self.cached_input_per_million
            if self.cached_input_per_million is not None
            else self.input_per_million
        )
        input_cost += cached_tokens * cached_price / 1_000_000
        return round(
            input_cost + output_tokens * self.output_per_million / 1_000_000,
            12,
        )


@dataclass(frozen=True)
class ProviderBenchmarkRecord:
    model: str
    case_id: str
    strategy: str
    status: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int
    cost_usd: float | None
    expected_evidence_retained: bool
    response_format_valid: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "case_id": self.case_id,
            "strategy": self.strategy,
            "status": self.status,
            "latency_ms": round(self.latency_ms, 3),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "expected_evidence_retained": self.expected_evidence_retained,
            "response_format_valid": self.response_format_valid,
            "error": self.error,
        }


def build_messages(
    case: ProviderBenchmarkCase, strategy: ContextStrategy
) -> tuple[LLMMessage, ...]:
    """Build one model-visible prompt without calling a summarizer model."""

    system = LLMMessage(
        role="system",
        content=(
            "You are an evaluation agent. Return only JSON with keys "
            "answer and evidence_ids. Use only the supplied evidence."
        ),
    )
    recent = list(case.history[-3:])
    evidence = list(case.evidence)
    payload: dict[str, Any] = {"goal": case.goal}
    if strategy is ContextStrategy.FULL_HISTORY:
        payload.update(
            {"history": list(case.history), "artifact": case.artifact(), "evidence": evidence}
        )
    elif strategy is ContextStrategy.RECENT_WINDOW:
        payload.update({"history": recent, "evidence": evidence})
    elif strategy is ContextStrategy.SUMMARY_WINDOW:
        payload.update(
            {"summary": list(case.summary), "history": recent, "evidence": evidence}
        )
    else:
        payload.update(
            {
                "working_memory": {"goal": case.goal, "next_actions": recent[-2:]},
                "running_summary": list(case.summary),
                "evidence_memory": [
                    {"evidence_id": item["evidence_id"], "summary": item["summary"]}
                    for item in evidence
                ],
                "artifact_policy": "references_only",
                "artifact_ids": [item.get("artifact_id") for item in evidence],
            }
        )
    return (
        system,
        LLMMessage(
            role="user",
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )


class ProviderBenchmarkRunner:
    """Run bounded, one-call-per-cell Provider experiments."""

    def __init__(self, *, prices: dict[str, ModelPrice] | None = None) -> None:
        self._prices = prices or {}

    async def run(
        self,
        *,
        clients: dict[str, LLMClient],
        cases: tuple[ProviderBenchmarkCase, ...],
        strategies: tuple[ContextStrategy, ...] = tuple(ContextStrategy),
    ) -> tuple[ProviderBenchmarkRecord, ...]:
        records: list[ProviderBenchmarkRecord] = []
        for model, client in clients.items():
            price = self._prices.get(model, ModelPrice())
            for case in cases:
                for strategy in strategies:
                    records.append(
                        await self._run_one(model, client, case, strategy, price)
                    )
        return tuple(records)

    async def _run_one(
        self,
        model: str,
        client: LLMClient,
        case: ProviderBenchmarkCase,
        strategy: ContextStrategy,
        price: ModelPrice,
    ) -> ProviderBenchmarkRecord:
        started = time.perf_counter()
        try:
            response = await client.complete(build_messages(case, strategy))
            input_tokens = _usage_int(response.usage, "prompt_tokens", "input_tokens")
            output_tokens = _usage_int(
                response.usage, "completion_tokens", "output_tokens"
            )
            cached_tokens = _usage_int(
                response.usage, "cached_tokens", "cache_read_input_tokens"
            )
            total_tokens = _usage_int(response.usage, "total_tokens") or (
                input_tokens + output_tokens
            )
            try:
                parsed = json.loads(response.content)
            except (TypeError, ValueError) as exc:
                # A malformed model response is a quality failure, but the
                # provider usage is still valid and must remain billable data.
                return ProviderBenchmarkRecord(
                    model=model,
                    case_id=case.case_id,
                    strategy=strategy.value,
                    status="quality_failed",
                    latency_ms=(time.perf_counter() - started) * 1000,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    total_tokens=total_tokens,
                    cost_usd=price.cost_usd(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_tokens=cached_tokens,
                    ),
                    expected_evidence_retained=False,
                    response_format_valid=False,
                    error=type(exc).__name__,
                )
            evidence_ids = parsed.get("evidence_ids", []) if isinstance(parsed, dict) else []
            valid = isinstance(parsed, dict) and isinstance(evidence_ids, list)
            retained = valid and set(case.expected_evidence_ids).issubset(set(evidence_ids))
            return ProviderBenchmarkRecord(
                model=model,
                case_id=case.case_id,
                strategy=strategy.value,
                status="succeeded" if valid and retained else "quality_failed",
                latency_ms=(time.perf_counter() - started) * 1000,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                total_tokens=total_tokens,
                cost_usd=price.cost_usd(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                ),
                expected_evidence_retained=retained,
                response_format_valid=valid,
            )
        except Exception as exc:  # provider errors are data in a benchmark cell
            return ProviderBenchmarkRecord(
                model=model,
                case_id=case.case_id,
                strategy=strategy.value,
                status="provider_failed",
                latency_ms=(time.perf_counter() - started) * 1000,
                input_tokens=0,
                output_tokens=0,
                cached_tokens=0,
                total_tokens=0,
                cost_usd=None,
                expected_evidence_retained=False,
                response_format_valid=False,
                error=type(exc).__name__,
            )


def _usage_int(usage: Any, *names: str) -> int:
    for name in names:
        value = usage.get(name) if hasattr(usage, "get") else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def summarize_records(records: tuple[ProviderBenchmarkRecord, ...]) -> list[dict[str, Any]]:
    """Aggregate cells by actual model and context strategy."""

    groups: dict[tuple[str, str], list[ProviderBenchmarkRecord]] = {}
    for record in records:
        groups.setdefault((record.model, record.strategy), []).append(record)
    summaries: list[dict[str, Any]] = []
    for (model, strategy), items in sorted(groups.items()):
        latencies = sorted(item.latency_ms for item in items)
        costs = [item.cost_usd for item in items if item.cost_usd is not None]
        summaries.append(
            {
                "model": model,
                "strategy": strategy,
                "cases": len(items),
                "success_rate": round(
                    sum(item.status == "succeeded" for item in items) / len(items), 4
                ),
                "evidence_retention_rate": round(
                    sum(item.expected_evidence_retained for item in items) / len(items),
                    4,
                ),
                "response_format_rate": round(
                    sum(item.response_format_valid for item in items) / len(items), 4
                ),
                "input_tokens_avg": round(
                    statistics.mean(item.input_tokens for item in items), 2
                ),
                "output_tokens_avg": round(
                    statistics.mean(item.output_tokens for item in items), 2
                ),
                "total_tokens_avg": round(
                    statistics.mean(item.total_tokens for item in items), 2
                ),
                "cost_usd_total": round(sum(costs), 12) if costs else None,
                "latency_ms_p50": round(_percentile(latencies, 0.50), 3),
                "latency_ms_p95": round(_percentile(latencies, 0.95), 3),
            }
        )
    return summaries


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, math.ceil(len(values) * fraction) - 1))
    return values[index]
