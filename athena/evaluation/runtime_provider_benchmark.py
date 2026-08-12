"""Provider-backed, multi-tick Agent Runtime/ReAct benchmark.

This module deliberately lives outside the Runtime implementation.  It builds
the same production objects with dependency injection, then observes the LLM
client boundary while ``AgentRuntime.advance`` executes one bounded ReAct
tick at a time.  The default CLI uses the deterministic client below and never
opens a network connection.
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Mapping

from athena.infra.llm import LLMClient, LLMMessage, LLMResponse
from athena.infra.model_router import ModelRouter
from athena.infra.token_meter import TokenMeter
from athena.runtime.llm_engine import LLMDecisionEngine
from athena.runtime.memory import FourLayerRuntimeContextCompiler
from athena.runtime.models import AgentTask, ContextSnapshot, TaskProfile, TaskStatus
from athena.runtime.runtime import AgentRuntime
from athena.runtime.store import InMemoryRuntimeStore
from athena.runtime.tools import ReadOnlyToolCatalog, ToolDeclaration


class RuntimeContextStrategy(StrEnum):
    """Context policies executed through the real Runtime loop."""

    FULL_HISTORY = "full_history"
    RECENT_WINDOW = "recent_window"
    SUMMARY_WINDOW = "summary_window"
    FOUR_LAYER = "four_layer"


@dataclass(frozen=True)
class RuntimeBenchmarkCase:
    """Replayable task plus a small success oracle.

    ``tool_sequence`` is used by the offline client and is also included in
    the task goal in the checked-in fixture.  Live providers are free to plan
    differently; the oracle checks whether the required read-only evidence was
    actually collected rather than trusting the provider's prose.
    """

    case_id: str
    goal: str
    repository_root: str
    tool_sequence: tuple[str, ...]
    tool_arguments: tuple[dict[str, Any], ...]
    expected_tool_names: tuple[str, ...]
    profile: TaskProfile = TaskProfile.STANDARD
    max_ticks: int | None = None

    def __post_init__(self) -> None:
        if not self.case_id.strip() or not self.goal.strip():
            raise ValueError("case_id and goal must be non-empty")
        if len(self.tool_sequence) != len(self.tool_arguments):
            raise ValueError("tool_sequence and tool_arguments must have equal length")
        if not self.expected_tool_names:
            raise ValueError("expected_tool_names must not be empty")


@dataclass(frozen=True)
class RuntimeModelPair:
    """The two model IDs supplied to the Runtime's complexity router."""

    light_model: str
    heavy_model: str
    light_client: LLMClient
    heavy_client: LLMClient

    @property
    def label(self) -> str:
        return f"light={self.light_model};heavy={self.heavy_model}"


@dataclass(frozen=True)
class ModelPrice:
    """Price snapshot supplied by the experiment owner, in USD per million."""

    input_per_million: float = 0.0
    output_per_million: float = 0.0
    cached_input_per_million: float | None = None

    def cost_usd(self, *, input_tokens: int, output_tokens: int, cached_tokens: int) -> float | None:
        if min(
            self.input_per_million,
            self.output_per_million,
            self.cached_input_per_million
            if self.cached_input_per_million is not None
            else 0.0,
        ) < 0:
            raise ValueError("model prices must be non-negative")
        if self.input_per_million == 0 and self.output_per_million == 0:
            return None
        cached_price = (
            self.input_per_million
            if self.cached_input_per_million is None
            else self.cached_input_per_million
        )
        uncached_tokens = max(0, input_tokens - cached_tokens)
        value = (
            uncached_tokens * self.input_per_million
            + cached_tokens * cached_price
            + output_tokens * self.output_per_million
        ) / 1_000_000
        return round(value, 12)


@dataclass(frozen=True)
class RuntimeCallRecord:
    """One provider call, including a repair call when one was needed."""

    call_index: int
    model: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    total_tokens: int
    status: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_index": self.call_index,
            "model": self.model,
            "latency_ms": round(self.latency_ms, 3),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "status": self.status,
            "error": self.error,
        }


@dataclass(frozen=True)
class RuntimeRoundRecord:
    """One Runtime Tick and every provider call made during that Tick."""

    tick: int
    status: str
    decision_kind: str
    reason_code: str
    selected_tier: str | None
    preferred_tier: str | None
    routed_model: str | None
    route_reason: str | None
    complexity_score: float | None
    repair_attempts: int
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int
    calls: tuple[RuntimeCallRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "status": self.status,
            "decision_kind": self.decision_kind,
            "reason_code": self.reason_code,
            "selected_tier": self.selected_tier,
            "preferred_tier": self.preferred_tier,
            "routed_model": self.routed_model,
            "route_reason": self.route_reason,
            "complexity_score": self.complexity_score,
            "repair_attempts": self.repair_attempts,
            "latency_ms": round(self.latency_ms, 3),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "calls": [call.to_dict() for call in self.calls],
        }


@dataclass(frozen=True)
class RuntimeBenchmarkCell:
    """The auditable unit: one case x strategy x routed model pair."""

    cell_id: str
    case_id: str
    strategy: str
    model: str
    status: str
    success: bool
    tick_count: int
    repair_attempts: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int
    cost_usd: float | None
    evidence_retained: bool
    evidence_ids: tuple[str, ...]
    collected_tools: tuple[str, ...]
    routed_models: tuple[str, ...]
    latency_ms: float
    rounds: tuple[RuntimeRoundRecord, ...]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "case_id": self.case_id,
            "strategy": self.strategy,
            "model": self.model,
            "status": self.status,
            "success": self.success,
            "tick_count": self.tick_count,
            "repair_attempts": self.repair_attempts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "evidence_retained": self.evidence_retained,
            "evidence_ids": list(self.evidence_ids),
            "collected_tools": list(self.collected_tools),
            "routed_models": list(self.routed_models),
            "latency_ms": round(self.latency_ms, 3),
            "rounds": [round_item.to_dict() for round_item in self.rounds],
            "error": self.error,
        }


@dataclass(frozen=True)
class _CallObservation:
    model: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    total_tokens: int
    status: str
    error: str | None = None


class _RecordingClient:
    """Observe a client without logging prompts, responses, or credentials."""

    def __init__(
        self,
        model: str,
        delegate: LLMClient,
        observations: list[_CallObservation],
    ) -> None:
        self._model = model
        self._delegate = delegate
        self._all_observations = observations
        self.observations: list[_CallObservation] = []

    def _record(self, observation: _CallObservation) -> None:
        self.observations.append(observation)
        self._all_observations.append(observation)

    async def complete(self, messages: list[LLMMessage]) -> LLMResponse:
        started = time.perf_counter()
        try:
            response = await self._delegate.complete(messages)
        except Exception as exc:  # provider error is represented by its type only
            self._record(
                _CallObservation(
                    model=self._model,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    input_tokens=0,
                    output_tokens=0,
                    cached_tokens=0,
                    reasoning_tokens=0,
                    total_tokens=0,
                    status="failed",
                    error=type(exc).__name__,
                )
            )
            raise
        usage = _normalize_usage(response.usage)
        self._record(
            _CallObservation(
                model=self._model,
                latency_ms=(time.perf_counter() - started) * 1000,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cached_tokens=usage["cached_tokens"],
                reasoning_tokens=usage["reasoning_tokens"],
                total_tokens=usage["total_tokens"],
                status="succeeded",
            )
        )
        return response


class RuntimeProviderBenchmarkRunner:
    """Execute complete Runtime cells in a deterministic order."""

    def __init__(self, *, prices: Mapping[str, ModelPrice] | None = None) -> None:
        self._prices = dict(prices or {})

    async def run(
        self,
        *,
        model_pairs: tuple[RuntimeModelPair, ...],
        cases: tuple[RuntimeBenchmarkCase, ...],
        strategies: tuple[RuntimeContextStrategy, ...] = tuple(RuntimeContextStrategy),
    ) -> tuple[RuntimeBenchmarkCell, ...]:
        cells: list[RuntimeBenchmarkCell] = []
        for model_pair in model_pairs:
            for case in cases:
                for strategy in strategies:
                    cells.append(
                        await asyncio.to_thread(
                            self._run_cell, model_pair, case, strategy
                        )
                    )
        return tuple(cells)

    def _run_cell(
        self,
        model_pair: RuntimeModelPair,
        case: RuntimeBenchmarkCase,
        strategy: RuntimeContextStrategy,
    ) -> RuntimeBenchmarkCell:
        started = time.perf_counter()
        all_observations: list[_CallObservation] = []
        light = _RecordingClient(
            model_pair.light_model,
            _bind_client(model_pair.light_client, case),
            all_observations,
        )
        heavy = _RecordingClient(
            model_pair.heavy_model,
            _bind_client(model_pair.heavy_client, case),
            all_observations,
        )
        rounds: list[RuntimeRoundRecord] = []
        try:
            catalog = _catalog_for_case(case)
            compiler = _compiler_for_strategy(strategy)
            engine = LLMDecisionEngine(ModelRouter(light, heavy), purpose="react_decision")
            store = InMemoryRuntimeStore()
            runtime = AgentRuntime(
                store=store,
                decision_engine=engine,
                context_compiler=compiler,
                tools=catalog,
            )
            task = AgentTask.create(
                goal=case.goal,
                repository_root=case.repository_root,
                profile=case.profile,
            )
            if case.max_ticks is not None:
                task = replace(task, budget=replace(task.budget, max_ticks=case.max_ticks))
            store.create_task(task)
            lease_id = f"benchmark_{task.task_id}"

            while not task.status.terminal and task.status is not TaskStatus.WAITING_HUMAN:
                before = len(all_observations)
                result = runtime.advance(task.task_id, lease_id)
                new_calls = all_observations[before:]
                if result.tick is not None:
                    routing = engine.last_routing
                    rounds.append(
                        _round_record(
                            result=result,
                            routing=routing,
                            calls=new_calls,
                        )
                    )
                task = result.task
                if result.tick is None:
                    break

            snapshot = store.snapshot(task.task_id)
            collected_tools = tuple(
                event.payload.get("tool_name")
                for event in snapshot.events
                if event.kind == "tool.succeeded" and isinstance(event.payload.get("tool_name"), str)
            )
            evidence_ids = tuple(snapshot.working_state.evidence_ids)
            expected_tools = set(case.expected_tool_names)
            evidence_retained = expected_tools.issubset(set(collected_tools)) and bool(evidence_ids)
            all_calls = [call for round_item in rounds for call in round_item.calls]
            input_tokens = sum(call.input_tokens for call in all_calls)
            output_tokens = sum(call.output_tokens for call in all_calls)
            cached_tokens = sum(call.cached_tokens for call in all_calls)
            total_tokens = sum(call.total_tokens for call in all_calls)
            cost = _cost_for_calls(all_calls, self._prices)
            success = task.status is TaskStatus.SUCCEEDED and evidence_retained
            status = "succeeded" if success else task.status.value
            if task.status is TaskStatus.SUCCEEDED and not evidence_retained:
                status = "quality_failed"
            return RuntimeBenchmarkCell(
                cell_id=_cell_id(case.case_id, strategy, model_pair.label),
                case_id=case.case_id,
                strategy=strategy.value,
                model=model_pair.label,
                status=status,
                success=success,
                tick_count=len(rounds),
                repair_attempts=sum(item.repair_attempts for item in rounds),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                total_tokens=total_tokens,
                cost_usd=cost,
                evidence_retained=evidence_retained,
                evidence_ids=evidence_ids,
                collected_tools=collected_tools,
                routed_models=tuple(dict.fromkeys(call.model for call in all_calls)),
                latency_ms=(time.perf_counter() - started) * 1000,
                rounds=tuple(rounds),
            )
        except Exception as exc:  # one bad cell must not discard the experiment
            all_calls = [_call_record(index, observation) for index, observation in enumerate(all_observations, start=1)]
            return RuntimeBenchmarkCell(
                cell_id=_cell_id(case.case_id, strategy, model_pair.label),
                case_id=case.case_id,
                strategy=strategy.value,
                model=model_pair.label,
                status="benchmark_failed",
                success=False,
                tick_count=len(rounds),
                repair_attempts=sum(item.repair_attempts for item in rounds),
                input_tokens=sum(item.input_tokens for item in all_calls),
                output_tokens=sum(item.output_tokens for item in all_calls),
                cached_tokens=sum(item.cached_tokens for item in all_calls),
                total_tokens=sum(item.total_tokens for item in all_calls),
                cost_usd=_cost_for_calls(all_calls, self._prices),
                evidence_retained=False,
                evidence_ids=(),
                collected_tools=(),
                routed_models=tuple(dict.fromkeys(item.model for item in all_calls)),
                latency_ms=(time.perf_counter() - started) * 1000,
                rounds=tuple(rounds),
                error=type(exc).__name__,
            )


class DryRunDecisionClient:
    """Offline model substitute that follows the case's read-only plan."""

    def __init__(self, case: RuntimeBenchmarkCase | None = None) -> None:
        self._case = case
        self._meter = TokenMeter()

    def for_case(self, case: RuntimeBenchmarkCase) -> "DryRunDecisionClient":
        return type(self)(case)

    async def complete(self, messages: list[LLMMessage]) -> LLMResponse:
        if self._case is None:
            raise RuntimeError("dry-run client must be bound to a benchmark case")
        payload = json.loads(messages[-1].content)
        evidence = _payload_evidence(payload)
        completed = {
            str(item.get("source", "")).removeprefix("tool:")
            for item in evidence
            if isinstance(item, Mapping)
        }
        next_index = next(
            (
                index
                for index, tool_name in enumerate(self._case.tool_sequence)
                if tool_name not in completed
            ),
            None,
        )
        if next_index is not None:
            decision = {
                "kind": "tool_call",
                "reason_code": f"STEP_{next_index + 1}",
                "tool_name": self._case.tool_sequence[next_index],
                "arguments": self._case.tool_arguments[next_index],
            }
        else:
            decision = {
                "kind": "final",
                "reason_code": "EVIDENCE_SUFFICIENT",
                "response": "The collected evidence is sufficient for a read-only recommendation.",
            }
        content = json.dumps(decision, ensure_ascii=False, separators=(",", ":"))
        input_tokens = self._meter.count("\n".join(item.content for item in messages))
        output_tokens = self._meter.count(content)
        return LLMResponse(
            content=content,
            model="dry-run",
            usage={
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        )


def summarize_runtime_cells(cells: tuple[RuntimeBenchmarkCell, ...]) -> list[dict[str, Any]]:
    """Aggregate cells while retaining model-pair and strategy identity."""

    groups: dict[tuple[str, str], list[RuntimeBenchmarkCell]] = {}
    for cell in cells:
        groups.setdefault((cell.model, cell.strategy), []).append(cell)
    summaries: list[dict[str, Any]] = []
    for (model, strategy), items in sorted(groups.items()):
        latencies = sorted(item.latency_ms for item in items)
        summaries.append(
            {
                "model": model,
                "strategy": strategy,
                "cases": len(items),
                "success_rate": _rate(item.success for item in items),
                "evidence_retention_rate": _rate(item.evidence_retained for item in items),
                "avg_tick_count": round(statistics.mean(item.tick_count for item in items), 2),
                "repair_attempts_total": sum(item.repair_attempts for item in items),
                "input_tokens_avg": round(statistics.mean(item.input_tokens for item in items), 2),
                "output_tokens_avg": round(statistics.mean(item.output_tokens for item in items), 2),
                "cached_tokens_avg": round(statistics.mean(item.cached_tokens for item in items), 2),
                "total_tokens_avg": round(statistics.mean(item.total_tokens for item in items), 2),
                "cost_usd_total": _sum_optional(item.cost_usd for item in items),
                "latency_ms_p50": round(_percentile(latencies, 0.50), 3),
                "latency_ms_p95": round(_percentile(latencies, 0.95), 3),
            }
        )
    return summaries


def _compiler_for_strategy(strategy: RuntimeContextStrategy) -> Any:
    if strategy is RuntimeContextStrategy.FOUR_LAYER:
        return _FourLayerBenchmarkCompiler()
    return _ComparisonContextCompiler(strategy)


class _FourLayerBenchmarkCompiler:
    """Use the production compiler with the parser compatibility projection."""

    def __init__(self) -> None:
        self._delegate = FourLayerRuntimeContextCompiler(
            model_window_tokens=16_384,
            safety_margin_tokens=1_024,
        )

    def compile(self, **kwargs: Any) -> ContextSnapshot:
        snapshot = self._delegate.compile(**kwargs)
        return replace(snapshot, tool_schemas=())


class _ComparisonContextCompiler:
    """Only the context policy varies; Runtime and decision contract stay fixed."""

    def __init__(self, strategy: RuntimeContextStrategy) -> None:
        self._strategy = strategy
        self._meter = TokenMeter()

    def compile(self, *, task, tick_sequence, working_state, events, evidence, tools):
        event_payloads = [
            {"kind": event.kind, "payload": event.payload} for event in events
        ]
        if self._strategy is RuntimeContextStrategy.RECENT_WINDOW:
            selected_events = event_payloads[-4:]
        elif self._strategy is RuntimeContextStrategy.SUMMARY_WINDOW:
            selected_events = event_payloads[-2:]
        else:
            selected_events = event_payloads
        evidence_payload = [
            {
                "evidence_id": item.evidence_id,
                "artifact_id": item.artifact_id,
                "source": item.source,
                "summary": item.summary,
            }
            for item in evidence
        ]
        payload = {
            "task": {
                "goal": task.goal,
                "repository_root": task.repository_root,
                "profile": task.profile.value,
                "budget_mode": task.budget.mode,
            },
            "working_state": {
                "plan": list(working_state.plan),
                "pending_items": list(working_state.pending_items),
                "evidence_ids": list(working_state.evidence_ids),
                "running_summary": working_state.running_summary,
                "human_input": working_state.human_input,
            },
            "evidence": evidence_payload,
            "selected_tool_schemas": [_tool_schema(item) for item in tools[:3]],
            "recent_events": selected_events,
        }
        if self._strategy is RuntimeContextStrategy.SUMMARY_WINDOW:
            payload["history_summary"] = {
                "event_count": len(events),
                "omitted_event_count": max(0, len(events) - len(selected_events)),
                "running_summary": working_state.running_summary,
            }
        estimated = max(1, self._meter.count(json.dumps(payload, ensure_ascii=False, sort_keys=True)))
        return ContextSnapshot(
            task_id=task.task_id,
            tick_sequence=tick_sequence,
            payload=payload,
            estimated_input_tokens=estimated,
            input_budget_tokens=max(0, 16_384 - task.budget.output_reserve_tokens - 1_024),
            output_reserve_tokens=task.budget.output_reserve_tokens,
            compacted=self._strategy is not RuntimeContextStrategy.FULL_HISTORY and len(selected_events) < len(events),
            omitted_event_count=max(0, len(events) - len(selected_events)),
            compaction_count=working_state.compaction_count,
            # LLMDecisionEngine's V1 parser reads the model-visible list from
            # payload.  Keep the optional compatibility tuple empty here so
            # the parser does not mistake the tuple for an unavailable tool.
            tool_schemas=(),
        )


def _round_record(*, result, routing, calls: list[_CallObservation]) -> RuntimeRoundRecord:
    call_records = tuple(_call_record(index, call) for index, call in enumerate(calls, start=1))
    return RuntimeRoundRecord(
        tick=result.tick.sequence,
        status=result.tick.status.value,
        decision_kind=result.tick.decision.kind.value,
        reason_code=result.tick.decision.reason_code,
        selected_tier=getattr(routing, "selected_tier", None),
        preferred_tier=getattr(routing, "preferred_tier", None),
        routed_model=(call_records[-1].model if call_records else getattr(routing, "model", None)),
        route_reason=getattr(routing, "route_reason", None),
        complexity_score=getattr(routing, "complexity_score", None),
        repair_attempts=int(bool(getattr(routing, "repair_attempted", False))),
        latency_ms=sum(call.latency_ms for call in call_records),
        input_tokens=sum(call.input_tokens for call in call_records),
        output_tokens=sum(call.output_tokens for call in call_records),
        cached_tokens=sum(call.cached_tokens for call in call_records),
        total_tokens=sum(call.total_tokens for call in call_records),
        calls=call_records,
    )


def _call_record(index: int, observation: _CallObservation) -> RuntimeCallRecord:
    return RuntimeCallRecord(
        call_index=index,
        model=observation.model,
        latency_ms=observation.latency_ms,
        input_tokens=observation.input_tokens,
        output_tokens=observation.output_tokens,
        cached_tokens=observation.cached_tokens,
        reasoning_tokens=observation.reasoning_tokens,
        total_tokens=observation.total_tokens,
        status=observation.status,
        error=observation.error,
    )


def _catalog_for_case(case: RuntimeBenchmarkCase) -> ReadOnlyToolCatalog:
    default = ReadOnlyToolCatalog()
    by_name = {item.name: item for item in default.declarations}
    ordered_names = list(dict.fromkeys((*case.tool_sequence, *by_name)))
    return ReadOnlyToolCatalog(tuple(by_name[name] for name in ordered_names if name in by_name))


def _bind_client(client: LLMClient, case: RuntimeBenchmarkCase) -> LLMClient:
    binder = getattr(client, "for_case", None)
    return binder(case) if callable(binder) else client


def _tool_schema(item: ToolDeclaration) -> dict[str, Any]:
    return {
        "name": item.name,
        "description": item.description,
        "input_schema": item.input_schema,
        "readonly": item.readonly,
    }


def _payload_evidence(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = payload.get("evidence", payload.get("evidence_memory", []))
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _normalize_usage(usage: Any) -> dict[str, int]:
    """Flatten common OpenAI-compatible nested usage fields locally.

    Core ``athena.infra.llm`` remains untouched by the benchmark-only adapter.
    This keeps provider-specific fields visible in experiment records even when
    a provider nests cache or reasoning counters under ``*_details``.
    """

    data = _as_mapping(usage)
    input_tokens = _first_int(data, "prompt_tokens", "input_tokens")
    output_tokens = _first_int(data, "completion_tokens", "output_tokens")
    cached_tokens = _first_int(
        data,
        "cached_tokens",
        "cache_read_input_tokens",
        "cache_read_tokens",
        "prompt_cache_hit_tokens",
    )
    for key in ("prompt_tokens_details", "input_tokens_details"):
        cached_tokens = cached_tokens or _first_int(
            _as_mapping(data.get(key)), "cached_tokens", "cache_read_input_tokens"
        )
    reasoning_tokens = _first_int(data, "reasoning_tokens")
    for key in ("completion_tokens_details", "output_tokens_details"):
        reasoning_tokens = reasoning_tokens or _first_int(
            _as_mapping(data.get(key)), "reasoning_tokens"
        )
    total_tokens = _first_int(data, "total_tokens") or input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
    }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, Mapping) else {}
    return {}


def _first_int(data: Mapping[str, Any], *names: str) -> int:
    for name in names:
        value = data.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _cost_for_calls(calls: list[RuntimeCallRecord], prices: Mapping[str, ModelPrice]) -> float | None:
    values: list[float] = []
    for call in calls:
        price = prices.get(call.model)
        if price is None:
            continue
        value = price.cost_usd(
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            cached_tokens=call.cached_tokens,
        )
        if value is not None:
            values.append(value)
    return round(sum(values), 12) if values else None


def _cell_id(case_id: str, strategy: RuntimeContextStrategy, model: str) -> str:
    safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", model)
    return f"{case_id}:{strategy.value}:{safe_model}"


def _rate(values) -> float:
    values = list(values)
    return round(sum(bool(value) for value in values) / len(values), 4) if values else 0.0


def _sum_optional(values) -> float | None:
    values = [value for value in values if value is not None]
    return round(sum(values), 12) if values else None


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, int((len(values) - 1) * fraction + 0.999999)))
    return values[index]


__all__ = [
    "DryRunDecisionClient",
    "ModelPrice",
    "RuntimeBenchmarkCase",
    "RuntimeBenchmarkCell",
    "RuntimeContextStrategy",
    "RuntimeModelPair",
    "RuntimeProviderBenchmarkRunner",
    "RuntimeRoundRecord",
    "RuntimeCallRecord",
    "_normalize_usage",
    "summarize_runtime_cells",
]
