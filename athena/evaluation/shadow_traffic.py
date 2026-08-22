"""Production-shaped Shadow traffic capture and replay.

The collector stores a redacted, replayable observation of a completed main
Runtime task.  The worker runs the Candidate in a fresh AgentRuntime with a
replay-only tool catalog, so Shadow can observe real traffic without invoking
production side effects or changing the main task aggregate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from athena.evaluation.candidate_skill_loading import (
    CandidateSkillContextCompiler,
    candidate_index_name,
    candidate_reference_needed,
    candidate_trigger_matches,
)
from athena.learning.skill_candidate import SkillCandidate
from athena.runtime import (
    AgentRuntime,
    AgentTask,
    Artifact,
    ContextSnapshot,
    Decision,
    DecisionKind,
    Evidence,
    InMemoryRuntimeStore,
    ReadOnlyToolCatalog,
    RuntimeSnapshot,
    TaskBudget,
    TaskProfile,
    TaskStatus,
    ToolDeclaration,
)
from athena.runtime.models import utc_now
from athena.runtime.tools import ToolExecution

SHADOW_TRAFFIC_SCHEMA_VERSION = "athena.shadow-traffic.v1"
SHADOW_TRAFFIC_RUNNER_VERSION = "agent-runtime-shadow-traffic-replay-v1"
_MAX_CAPTURE_TEXT = 8_000
_SECRET_VALUE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth(?:orization)?|password|secret|token)\b\s*[:=]\s*)([^\s,;]+)"
)


@dataclass(frozen=True)
class ShadowBaselineMetrics:
    task_status: str
    task_success: bool
    evidence_sources: tuple[str, ...]
    evidence_retention: float
    tick_count: int
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: float
    result_digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "task_status": self.task_status,
            "task_success": self.task_success,
            "evidence_sources": list(self.evidence_sources),
            "evidence_retention": round(self.evidence_retention, 6),
            "tick_count": self.tick_count,
            "tool_call_count": self.tool_call_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": round(self.latency_ms, 3),
            "result_digest": self.result_digest,
        }


@dataclass(frozen=True)
class ShadowToolObservation:
    sequence: int
    tool_name: str
    arguments: dict[str, object]
    succeeded: bool
    artifact_content: dict[str, object] | None = None
    evidence_source: str | None = None
    evidence_summary: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "succeeded": self.succeeded,
            "artifact_content": self.artifact_content,
            "evidence_source": self.evidence_source,
            "evidence_summary": self.evidence_summary,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class ShadowTraceEnvelope:
    """Redacted input and observations needed to replay one real task."""

    observation_id: str
    tenant_id: str
    trace_id: str
    candidate_id: str
    candidate_digest: str
    goal: str
    repository_root: str
    profile: str
    budget: dict[str, int]
    decisions: tuple[Decision, ...]
    tool_observations: tuple[ShadowToolObservation, ...]
    baseline: ShadowBaselineMetrics
    redaction_count: int
    captured_at: datetime
    traceparent: str | None = None
    schema_version: str = SHADOW_TRAFFIC_SCHEMA_VERSION

    @classmethod
    def capture(
        cls,
        *,
        tenant_id: str,
        trace_id: str,
        snapshot: RuntimeSnapshot,
        candidate_id: str = "",
        candidate_digest: str = "",
        traceparent: str | None = None,
    ) -> "ShadowTraceEnvelope":
        if not tenant_id.strip() or not trace_id.strip():
            raise ValueError("tenant_id and trace_id must be non-empty")
        task = snapshot.task
        goal, goal_redactions = _redact_text(task.goal)
        observations: list[ShadowToolObservation] = []
        decisions: list[Decision] = []
        redactions = goal_redactions
        artifact_by_id = {item.artifact_id: item for item in snapshot.artifacts}
        evidence_by_id = {item.evidence_id: item for item in snapshot.evidence}
        for tick in snapshot.ticks:
            decision, decision_redactions = _safe_decision(tick.decision)
            decisions.append(decision)
            redactions += decision_redactions
            if tick.decision.kind is not DecisionKind.TOOL_CALL:
                continue
            event = next(
                (
                    item
                    for item in reversed(snapshot.events)
                    if item.tick_id == tick.tick_id
                    and item.kind in {"tool.succeeded", "tool.rejected"}
                ),
                None,
            )
            payload = event.payload if event is not None else {}
            if event is not None and event.kind == "tool.succeeded":
                artifact = artifact_by_id.get(str(payload.get("artifact_id") or ""))
                evidence = evidence_by_id.get(str(payload.get("evidence_id") or ""))
                content, content_redactions = _sanitize_json(
                    artifact.content if artifact is not None else {}
                )
                evidence_source, source_redactions = _redact_text(
                    evidence.source if evidence is not None else ""
                )
                evidence_summary, summary_redactions = _redact_text(
                    evidence.summary if evidence is not None else ""
                )
                redactions += (
                    content_redactions + source_redactions + summary_redactions
                )
                observations.append(
                    ShadowToolObservation(
                        sequence=tick.sequence,
                        tool_name=tick.decision.tool_name or "",
                        arguments=dict(decision.arguments),
                        succeeded=True,
                        artifact_content=(content if isinstance(content, dict) else {}),
                        evidence_source=evidence_source or None,
                        evidence_summary=evidence_summary or None,
                    )
                )
            else:
                error_code, error_redactions = _redact_text(
                    str(payload.get("reason_code") or "SHADOW_TOOL_FAILED")
                )
                redactions += error_redactions
                observations.append(
                    ShadowToolObservation(
                        sequence=tick.sequence,
                        tool_name=tick.decision.tool_name or "",
                        arguments=dict(decision.arguments),
                        succeeded=False,
                        error_code=error_code,
                    )
                )

        repository_digest = hashlib.sha256(
            task.repository_root.encode("utf-8", errors="ignore")
        ).hexdigest()[:24]
        observation_id = shadow_traffic_observation_id(
            tenant_id,
            trace_id,
            candidate_id,
            candidate_digest,
        )
        evidence_sources = tuple(sorted(item.source for item in snapshot.evidence))
        baseline = ShadowBaselineMetrics(
            task_status=task.status.value,
            task_success=task.status is TaskStatus.SUCCEEDED,
            evidence_sources=evidence_sources,
            evidence_retention=1.0 if evidence_sources else 0.0,
            tick_count=len(snapshot.ticks),
            tool_call_count=sum(
                item.decision.kind is DecisionKind.TOOL_CALL for item in snapshot.ticks
            ),
            input_tokens=sum(item.actual_input_tokens for item in snapshot.usage),
            output_tokens=sum(item.actual_output_tokens for item in snapshot.usage),
            total_tokens=sum(item.actual_tokens for item in snapshot.usage),
            latency_ms=max(
                0.0,
                (task.updated_at - task.created_at).total_seconds() * 1000,
            ),
            result_digest=_result_digest(task, evidence_sources),
        )
        return cls(
            observation_id=observation_id,
            tenant_id=tenant_id,
            trace_id=trace_id,
            candidate_id=candidate_id,
            candidate_digest=candidate_digest,
            goal=goal[:_MAX_CAPTURE_TEXT],
            repository_root=f"replay://{repository_digest}",
            profile=task.profile.value,
            budget={
                "total_tokens": task.budget.total_tokens,
                "max_ticks": task.budget.max_ticks,
                "output_reserve_tokens": task.budget.output_reserve_tokens,
            },
            decisions=tuple(decisions),
            tool_observations=tuple(observations),
            baseline=baseline,
            redaction_count=redactions,
            captured_at=utc_now(),
            traceparent=traceparent,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "tenant_id": self.tenant_id,
            "trace_id": self.trace_id,
            "candidate_id": self.candidate_id,
            "candidate_digest": self.candidate_digest,
            "goal": self.goal,
            "repository_root": self.repository_root,
            "profile": self.profile,
            "budget": dict(self.budget),
            "decisions": [_decision_dict(item) for item in self.decisions],
            "tool_observations": [item.to_dict() for item in self.tool_observations],
            "baseline": self.baseline.to_dict(),
            "redaction_count": self.redaction_count,
            "captured_at": self.captured_at.isoformat(),
            "traceparent": self.traceparent,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "ShadowTraceEnvelope":
        baseline_raw = dict(value.get("baseline") or {})
        baseline = ShadowBaselineMetrics(
            task_status=str(baseline_raw.get("task_status") or "failed"),
            task_success=bool(baseline_raw.get("task_success")),
            evidence_sources=tuple(
                str(item) for item in baseline_raw.get("evidence_sources", [])
            ),
            evidence_retention=float(baseline_raw.get("evidence_retention") or 0.0),
            tick_count=int(baseline_raw.get("tick_count") or 0),
            tool_call_count=int(baseline_raw.get("tool_call_count") or 0),
            input_tokens=int(baseline_raw.get("input_tokens") or 0),
            output_tokens=int(baseline_raw.get("output_tokens") or 0),
            total_tokens=int(baseline_raw.get("total_tokens") or 0),
            latency_ms=float(baseline_raw.get("latency_ms") or 0.0),
            result_digest=str(baseline_raw.get("result_digest") or ""),
        )
        return cls(
            observation_id=str(value.get("observation_id") or ""),
            tenant_id=str(value.get("tenant_id") or ""),
            trace_id=str(value.get("trace_id") or ""),
            candidate_id=str(value.get("candidate_id") or ""),
            candidate_digest=str(value.get("candidate_digest") or ""),
            goal=str(value.get("goal") or ""),
            repository_root=str(value.get("repository_root") or "replay://unknown"),
            profile=str(value.get("profile") or TaskProfile.STANDARD.value),
            budget={
                str(key): int(item)
                for key, item in dict(value.get("budget") or {}).items()
            },
            decisions=tuple(
                _decision_from_dict(dict(item))
                for item in value.get("decisions", [])
                if isinstance(item, dict)
            ),
            tool_observations=tuple(
                ShadowToolObservation(
                    sequence=int(item.get("sequence") or 0),
                    tool_name=str(item.get("tool_name") or ""),
                    arguments=dict(item.get("arguments") or {}),
                    succeeded=bool(item.get("succeeded")),
                    artifact_content=(
                        dict(item["artifact_content"])
                        if isinstance(item.get("artifact_content"), dict)
                        else None
                    ),
                    evidence_source=(
                        str(item["evidence_source"])
                        if item.get("evidence_source")
                        else None
                    ),
                    evidence_summary=(
                        str(item["evidence_summary"])
                        if item.get("evidence_summary")
                        else None
                    ),
                    error_code=(
                        str(item["error_code"]) if item.get("error_code") else None
                    ),
                )
                for item in value.get("tool_observations", [])
                if isinstance(item, dict)
            ),
            baseline=baseline,
            redaction_count=int(value.get("redaction_count") or 0),
            captured_at=_parse_datetime(value.get("captured_at")),
            traceparent=(
                str(value["traceparent"]) if value.get("traceparent") else None
            ),
            schema_version=str(
                value.get("schema_version") or SHADOW_TRAFFIC_SCHEMA_VERSION
            ),
        )


@dataclass(frozen=True)
class ShadowTrafficResult:
    observation_id: str
    status: str
    baseline_metrics: dict[str, object]
    candidate_metrics: dict[str, object]
    comparison: dict[str, object]
    failure_code: str | None
    started_at: datetime
    completed_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "status": self.status,
            "baseline_metrics": dict(self.baseline_metrics),
            "candidate_metrics": dict(self.candidate_metrics),
            "comparison": dict(self.comparison),
            "failure_code": self.failure_code,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
        }


class ReplayToolCatalog:
    """A read-only ToolCatalog that serves captured observations only."""

    def __init__(self, envelope: ShadowTraceEnvelope) -> None:
        self._declarations = ReadOnlyToolCatalog().declarations
        self._observations: dict[tuple[str, str], deque[ShadowToolObservation]] = (
            defaultdict(deque)
        )
        for item in envelope.tool_observations:
            self._observations[(item.tool_name, _canonical(item.arguments))].append(
                item
            )

    @property
    def declarations(self) -> tuple[ToolDeclaration, ...]:
        return self._declarations

    def has(self, tool_name: str) -> bool:
        return any(item.name == tool_name for item in self._declarations)

    def invoke(
        self,
        *,
        task_id: str,
        tick_id: str,
        repository_root: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolExecution:
        del repository_root
        if not self.has(tool_name):
            return ToolExecution(
                None, None, "SHADOW_TOOL_NOT_READONLY", "tool is blocked"
            )
        queue = self._observations.get((tool_name, _canonical(arguments)))
        if not queue:
            return ToolExecution(
                None,
                None,
                "SHADOW_TOOL_OBSERVATION_MISSING",
                "the captured read-only observation was not found",
            )
        observation = queue.popleft()
        if not observation.succeeded:
            return ToolExecution(
                None,
                None,
                observation.error_code or "SHADOW_TOOL_REPLAY_FAILED",
                "the captured tool call failed",
            )
        now = utc_now()
        artifact_id = _stable_id("shadow-artifact", task_id, tick_id, tool_name)
        evidence_id = _stable_id("shadow-evidence", task_id, tick_id, tool_name)
        content = dict(observation.artifact_content or {})
        artifact = Artifact(
            artifact_id=artifact_id,
            task_id=task_id,
            tick_id=tick_id,
            tool_name=tool_name,
            content=content,
            content_hash=hashlib.sha256(_canonical(content).encode()).hexdigest(),
            created_at=now,
        )
        evidence = Evidence(
            evidence_id=evidence_id,
            task_id=task_id,
            artifact_id=artifact_id,
            source=observation.evidence_source or f"tool:{tool_name}",
            summary=observation.evidence_summary or "Captured read-only Evidence.",
            created_at=now,
        )
        return ToolExecution(artifact, evidence)


class _TrafficDecisionEngine:
    def __init__(
        self, envelope: ShadowTraceEnvelope, candidate: SkillCandidate
    ) -> None:
        self._envelope = envelope
        self._candidate = candidate
        self.read_count = 0
        self.index_read_count = 0
        self.procedure_read_count = 0
        self.reference_read_count = 0
        self._loaded = False

    def decide(self, context: ContextSnapshot) -> Decision:
        if not self._loaded:
            index = context.payload.get("skill_index")
            if not isinstance(index, dict) or index.get("name") != candidate_index_name(
                self._candidate
            ):
                return Decision(
                    kind=DecisionKind.FAIL,
                    reason_code="CANDIDATE_CONTEXT_NOT_LOADED",
                    response="Candidate context was not loaded.",
                )
            self.index_read_count = 1
            goal = str(context.payload.get("task", {}).get("goal") or "")
            if candidate_trigger_matches(self._candidate, goal):
                if not isinstance(context.payload.get("skill_procedure"), dict):
                    return Decision(
                        kind=DecisionKind.FAIL,
                        reason_code="CANDIDATE_PROCEDURE_NOT_LOADED",
                        response="Candidate procedure was not loaded.",
                    )
                self.procedure_read_count = 1
            if candidate_reference_needed(self._candidate, goal):
                if not isinstance(context.payload.get("skill_reference"), dict):
                    return Decision(
                        kind=DecisionKind.FAIL,
                        reason_code="CANDIDATE_REFERENCE_NOT_LOADED",
                        response="Candidate reference was not loaded.",
                    )
                self.reference_read_count = 1
            self.read_count = 1
            self._loaded = True
        index = context.tick_sequence - 1
        if index >= len(self._envelope.decisions):
            return Decision(
                kind=DecisionKind.FAIL,
                reason_code="SHADOW_REPLAY_PLAN_EXHAUSTED",
                response="The captured Shadow plan was exhausted.",
            )
        planned = self._envelope.decisions[index]
        if planned.kind is DecisionKind.TOOL_CALL and planned.tool_name not in set(
            self._candidate.allowed_tools
        ):
            return Decision(
                kind=DecisionKind.FAIL,
                reason_code="CANDIDATE_TOOL_NOT_ALLOWED",
                response="Candidate policy rejected the captured tool call.",
            )
        return planned


class ShadowTrafficRunner:
    """Run one captured trace through an isolated, real AgentRuntime."""

    def __init__(self, repository_root: str | Path | None = None) -> None:
        self._repository_root = (
            Path(repository_root).resolve() if repository_root else None
        )

    def run(
        self, envelope: ShadowTraceEnvelope, candidate: SkillCandidate
    ) -> ShadowTrafficResult:
        if candidate.candidate_id != envelope.candidate_id:
            raise ValueError("candidate does not match the captured trace")
        if (
            candidate.status != "candidate"
            or candidate.evaluation_status != "replay_ab_passed"
        ):
            raise ValueError("candidate must pass Replay A/B before traffic Shadow")
        started_at = utc_now()
        started = time.perf_counter()
        store = InMemoryRuntimeStore()
        try:
            profile = TaskProfile(envelope.profile)
        except ValueError:
            profile = TaskProfile.STANDARD
        budget = TaskBudget(
            total_tokens=max(1, int(envelope.budget.get("total_tokens", 50_000))),
            max_ticks=max(1, int(envelope.budget.get("max_ticks", 6))),
            output_reserve_tokens=max(
                1, int(envelope.budget.get("output_reserve_tokens", 512))
            ),
        )
        task = AgentTask.create(
            goal=envelope.goal,
            repository_root=envelope.repository_root,
            profile=profile,
        )
        task = replace(
            task,
            task_id=_stable_id("shadow-traffic-task", envelope.observation_id),
            budget=budget,
        )
        engine = _TrafficDecisionEngine(envelope, candidate)
        runtime = AgentRuntime(
            store=store,
            decision_engine=engine,
            context_compiler=CandidateSkillContextCompiler(candidate),
            tools=ReplayToolCatalog(envelope),
        )
        store.create_task(task)
        while not task.status.terminal and task.status is not TaskStatus.WAITING_HUMAN:
            task = runtime.advance(task.task_id, lease_id="shadow-traffic-worker").task
        snapshot = store.snapshot(task.task_id)
        candidate_metrics = _runtime_metrics(
            snapshot,
            group="candidate",
            expected_evidence=envelope.baseline.evidence_sources,
            candidate_loaded=engine.read_count > 0,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        comparison = {
            "task_success_delta": int(candidate_metrics["task_success"])
            - int(envelope.baseline.task_success),
            "evidence_retention_delta": float(candidate_metrics["evidence_retention"])
            - envelope.baseline.evidence_retention,
            "tick_count_delta": int(candidate_metrics["tick_count"])
            - envelope.baseline.tick_count,
            "tool_call_count_delta": int(candidate_metrics["tool_call_count"])
            - envelope.baseline.tool_call_count,
            "input_tokens_delta": int(candidate_metrics["input_tokens"])
            - envelope.baseline.input_tokens,
            "output_tokens_delta": int(candidate_metrics["output_tokens"])
            - envelope.baseline.output_tokens,
            "total_tokens_delta": int(candidate_metrics["total_tokens"])
            - envelope.baseline.total_tokens,
            "latency_ms_delta": float(candidate_metrics["latency_ms"])
            - envelope.baseline.latency_ms,
            "main_result_preserved": True,
            "result_consistent": candidate_metrics["result_digest"]
            == envelope.baseline.result_digest,
            "shadow_side_effects_detected": False,
            "safety_violations": int(candidate_metrics["safety_violations"]),
        }
        return ShadowTrafficResult(
            observation_id=envelope.observation_id,
            status="succeeded",
            baseline_metrics=envelope.baseline.to_dict(),
            candidate_metrics=candidate_metrics,
            comparison=comparison,
            failure_code=None,
            started_at=started_at,
            completed_at=utc_now(),
        )


CandidateLoader = Callable[[str, str, str], Awaitable[SkillCandidate | None]]


class ShadowTrafficWorker:
    """Consume captured traces with at-least-once delivery and fail-closed metrics."""

    def __init__(
        self,
        repository,
        stream,
        candidate_loader: CandidateLoader,
        *,
        runner: ShadowTrafficRunner | None = None,
        worker_id: str,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._repository = repository
        self._stream = stream
        self._candidate_loader = candidate_loader
        self._runner = runner or ShadowTrafficRunner()
        self._worker_id = worker_id
        self._max_attempts = max_attempts

    async def run_once(self, *, count: int, block_ms: int, reclaim_idle_ms: int) -> int:
        messages = await self._stream.reclaim(
            self._worker_id, min_idle_ms=reclaim_idle_ms, count=count
        )
        messages = messages or await self._stream.consume(
            self._worker_id, count=count, block_ms=block_ms
        )
        processed = 0
        for message in messages:
            if message.event_type != "shadow.traffic.captured":
                await self._stream.ack(message.message_id)
                continue
            observation = await self._repository.claim(
                message.tenant_id, message.task_id, self._worker_id
            )
            if observation is None:
                await self._stream.ack(message.message_id)
                continue
            try:
                candidate = await self._candidate_loader(
                    observation.envelope.tenant_id,
                    observation.envelope.candidate_id,
                    observation.envelope.candidate_digest,
                )
                if candidate is None:
                    raise ValueError("SHADOW_CANDIDATE_NOT_FOUND")
                result = await asyncio.to_thread(
                    self._runner.run, observation.envelope, candidate
                )
                await self._repository.complete(
                    observation.envelope.tenant_id,
                    observation.observation_id,
                    self._worker_id,
                    result,
                )
                await self._stream.ack(message.message_id)
                processed += 1
            except (
                Exception
            ) as exc:  # noqa: BLE001 - fail closed at the worker boundary
                code = (
                    str(exc) or "SHADOW_EXECUTION_FAILED"
                    if isinstance(exc, ValueError)
                    else "SHADOW_EXECUTION_FAILED"
                )
                await self._repository.fail(
                    observation.envelope.tenant_id,
                    observation.observation_id,
                    self._worker_id,
                    code,
                )
                if observation.attempt_count >= self._max_attempts:
                    await self._stream.dead_letter(message, code)
                else:
                    await self._stream.ack(message.message_id)
                    await self._stream.publish(
                        message.task_id,
                        message.tenant_id,
                        message.traceparent,
                        "shadow.traffic.captured",
                    )
        return processed


def shadow_traffic_observation_id(
    tenant_id: str, trace_id: str, candidate_id: str, candidate_digest: str
) -> str:
    payload = json.dumps(
        {
            "tenant_id": tenant_id,
            "trace_id": trace_id,
            "candidate_id": candidate_id,
            "candidate_digest": candidate_digest,
            "runner": SHADOW_TRAFFIC_RUNNER_VERSION,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"shadow-traffic-{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def _runtime_metrics(
    snapshot: RuntimeSnapshot,
    *,
    group: str,
    expected_evidence: tuple[str, ...],
    candidate_loaded: bool,
    latency_ms: float,
) -> dict[str, object]:
    evidence_sources = tuple(sorted(item.source for item in snapshot.evidence))
    retention = (
        len(set(expected_evidence) & set(evidence_sources)) / len(expected_evidence)
        if expected_evidence
        else (1.0 if snapshot.task.status is TaskStatus.SUCCEEDED else 0.0)
    )
    rejected = [
        str(event.payload.get("reason_code") or "")
        for event in snapshot.events
        if event.kind == "tool.rejected"
    ]
    safety_violations = sum(
        code
        in {
            "SHADOW_TOOL_NOT_READONLY",
            "SHADOW_SIDE_EFFECT_BLOCKED",
            "CANDIDATE_TOOL_NOT_ALLOWED",
        }
        for code in rejected
    )
    return {
        "group": group,
        "task_status": snapshot.task.status.value,
        "task_success": snapshot.task.status is TaskStatus.SUCCEEDED,
        "candidate_loaded": candidate_loaded,
        "evidence_sources": list(evidence_sources),
        "evidence_retention": round(retention, 6),
        "tick_count": len(snapshot.ticks),
        "tool_call_count": sum(
            item.decision.kind is DecisionKind.TOOL_CALL for item in snapshot.ticks
        ),
        "input_tokens": sum(item.actual_input_tokens for item in snapshot.usage),
        "output_tokens": sum(item.actual_output_tokens for item in snapshot.usage),
        "total_tokens": sum(item.actual_tokens for item in snapshot.usage),
        "latency_ms": round(latency_ms, 3),
        "safety_violations": int(safety_violations),
        "failure_reason": rejected[-1] if rejected else None,
        "result_digest": _result_digest(snapshot.task, evidence_sources),
    }


def _result_digest(task: AgentTask, evidence_sources: tuple[str, ...]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "status": task.status.value,
                "evidence_sources": list(evidence_sources),
                "report_present": task.final_report is not None,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _safe_decision(decision: Decision) -> tuple[Decision, int]:
    arguments, redactions = _sanitize_json(decision.arguments)
    safe_arguments = arguments if isinstance(arguments, dict) else {}
    if decision.kind is DecisionKind.TOOL_CALL:
        return replace(decision, arguments=safe_arguments), redactions
    return (
        Decision(
            kind=decision.kind,
            reason_code=decision.reason_code,
            response="Shadow replay response omitted.",
        ),
        redactions,
    )


def _decision_dict(decision: Decision) -> dict[str, object]:
    return {
        "kind": decision.kind.value,
        "reason_code": decision.reason_code,
        "tool_name": decision.tool_name,
        "arguments": dict(decision.arguments),
    }


def _decision_from_dict(value: dict[str, object]) -> Decision:
    kind = DecisionKind(str(value.get("kind") or DecisionKind.FAIL.value))
    tool_name = str(value["tool_name"]) if value.get("tool_name") else None
    if kind is DecisionKind.TOOL_CALL:
        return Decision(
            kind=kind,
            reason_code=str(value.get("reason_code") or "SHADOW_REPLAY_STEP"),
            tool_name=tool_name,
            arguments=dict(value.get("arguments") or {}),
        )
    return Decision(
        kind=kind,
        reason_code=str(value.get("reason_code") or "SHADOW_REPLAY_STEP"),
        response="Shadow replay response omitted.",
    )


def _sanitize_json(value: object) -> tuple[object, int]:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        result: dict[str, object] = {}
        count = 0
        for key, item in value.items():
            safe_key, key_count = _redact_text(str(key))
            safe_item, item_count = _sanitize_json(item)
            result[safe_key] = safe_item
            count += key_count + item_count
        return result, count
    if isinstance(value, (list, tuple)):
        items = []
        count = 0
        for item in value:
            safe_item, item_count = _sanitize_json(item)
            items.append(safe_item)
            count += item_count
        return items, count
    if value is None or isinstance(value, (bool, int, float)):
        return value, 0
    return str(value), 0


def _redact_text(value: str) -> tuple[str, int]:
    value = value[:_MAX_CAPTURE_TEXT]
    count = 0

    def replace_secret(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}[REDACTED]"

    return _SECRET_VALUE.sub(replace_secret, value), count


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return utc_now()


__all__ = [
    "SHADOW_TRAFFIC_RUNNER_VERSION",
    "SHADOW_TRAFFIC_SCHEMA_VERSION",
    "ReplayToolCatalog",
    "ShadowBaselineMetrics",
    "ShadowToolObservation",
    "ShadowTraceEnvelope",
    "ShadowTrafficResult",
    "ShadowTrafficRunner",
    "ShadowTrafficWorker",
    "shadow_traffic_observation_id",
]
