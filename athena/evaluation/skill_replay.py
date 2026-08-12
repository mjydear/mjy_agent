"""Fixed, offline Replay cases and a real Runtime Baseline recorder.

The Baseline never loads a Candidate and never calls a provider. Every metric
comes from the executed Runtime snapshot; no comparative or synthetic metric is
reported by this module.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from athena.runtime import AgentRuntime, AgentTask, InMemoryRuntimeStore
from athena.runtime.models import (
    ContextSnapshot,
    Decision,
    DecisionKind,
    TaskStatus,
    utc_now,
)

REPLAY_CASE_SCHEMA_VERSION = "athena.skill-replay-case.v1"
BASELINE_RESULT_SCHEMA_VERSION = "athena.skill-baseline-result.v1"


class ReplayCaseCategory(StrEnum):
    SIMPLE = "simple"
    MULTI_STEP = "multi_step"
    TOOL_FAILURE = "tool_failure"
    SECURITY_REJECTION = "security_rejection"


@dataclass(frozen=True)
class ReplayToolPolicy:
    allowed_tools: tuple[str, ...]
    forbidden_tools: tuple[str, ...] = ()
    readonly_only: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed_tools": list(self.allowed_tools),
            "forbidden_tools": list(self.forbidden_tools),
            "readonly_only": self.readonly_only,
        }


@dataclass(frozen=True)
class ReplaySuccessOracle:
    expected_task_status: str
    required_tool_names: tuple[str, ...] = ()
    expected_rejection_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "expected_task_status": self.expected_task_status,
            "required_tool_names": list(self.required_tool_names),
            "expected_rejection_code": self.expected_rejection_code,
        }


@dataclass(frozen=True)
class ReplayCase:
    case_id: str
    category: ReplayCaseCategory
    input: str
    fixture_id: str
    fixture_files: tuple[str, ...]
    tool_policy: ReplayToolPolicy
    required_evidence: tuple[str, ...]
    max_ticks: int
    max_tool_calls: int
    success_oracle: ReplaySuccessOracle
    decisions: tuple[Decision, ...]
    schema_version: str = REPLAY_CASE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.case_id.strip() or not self.input.strip():
            raise ValueError("Replay case ID and input must be non-empty")
        if self.max_ticks < 1 or self.max_tool_calls < 1:
            raise ValueError("Replay case limits must be positive")
        if len(self.decisions) > self.max_ticks:
            raise ValueError("Replay case decisions exceed max_ticks")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "category": self.category.value,
            "input": self.input,
            "fixture": {
                "fixture_id": self.fixture_id,
                "files": list(self.fixture_files),
            },
            "tool_policy": self.tool_policy.to_dict(),
            "required_evidence": list(self.required_evidence),
            "max_ticks": self.max_ticks,
            "max_tool_calls": self.max_tool_calls,
            "success_oracle": self.success_oracle.to_dict(),
        }


@dataclass(frozen=True)
class BaselineCaseResult:
    case_id: str
    category: str
    task_status: str
    oracle_passed: bool
    oracle_checks: dict[str, bool]
    tick_count: int
    tool_call_count: int
    successful_tool_calls: tuple[str, ...]
    rejected_tool_calls: tuple[dict[str, str], ...]
    evidence_ids: tuple[str, ...]
    usage: dict[str, int]
    latency_ms: float

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "task_status": self.task_status,
            "oracle_passed": self.oracle_passed,
            "oracle_checks": dict(self.oracle_checks),
            "tick_count": self.tick_count,
            "tool_call_count": self.tool_call_count,
            "successful_tool_calls": list(self.successful_tool_calls),
            "rejected_tool_calls": [dict(item) for item in self.rejected_tool_calls],
            "evidence_ids": list(self.evidence_ids),
            "usage": dict(self.usage),
            "latency_ms": round(self.latency_ms, 3),
        }


@dataclass(frozen=True)
class BaselineRun:
    run_id: str
    tenant_id: str
    schema_version: str
    case_definition_digest: str
    runner: str
    candidate_loaded: bool
    results: tuple[BaselineCaseResult, ...]
    started_at: datetime
    completed_at: datetime

    @property
    def oracle_pass_count(self) -> int:
        return sum(item.oracle_passed for item in self.results)

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "schema_version": self.schema_version,
            "case_definition_digest": self.case_definition_digest,
            "runner": self.runner,
            "candidate_loaded": self.candidate_loaded,
            "measurement": "runtime_observed",
            "case_count": len(self.results),
            "oracle_pass_count": self.oracle_pass_count,
            "results": [item.to_dict() for item in self.results],
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
        }


class _FixedDecisionEngine:
    def __init__(self, decisions: tuple[Decision, ...]) -> None:
        self._decisions = decisions

    def decide(self, context: ContextSnapshot) -> Decision:
        index = context.tick_sequence - 1
        if index >= len(self._decisions):
            return Decision(
                kind=DecisionKind.FAIL,
                reason_code="FIXED_REPLAY_PLAN_EXHAUSTED",
                response="The fixed Replay plan was exhausted.",
            )
        return self._decisions[index]


class SkillBaselineRunner:
    """Execute fixed cases through AgentRuntime without loading a Candidate."""

    def __init__(self, repository_root: str | Path | None = None) -> None:
        self._repository_root = Path(
            repository_root or _default_fixture_root()
        ).resolve()

    def run(
        self,
        *,
        tenant_id: str,
        cases: tuple[ReplayCase, ...] | None = None,
    ) -> BaselineRun:
        if not tenant_id.strip():
            raise ValueError("tenant_id must be non-empty")
        selected = cases or fixed_replay_cases()
        if not selected:
            raise ValueError("at least one Replay case is required")
        if not self._repository_root.is_dir():
            raise ValueError("Replay fixture repository is unavailable")
        started_at = utc_now()
        results = tuple(self._run_case(case) for case in selected)
        return BaselineRun(
            run_id=f"skill-baseline-{uuid4().hex}",
            tenant_id=tenant_id.strip(),
            schema_version=BASELINE_RESULT_SCHEMA_VERSION,
            case_definition_digest=replay_case_definition_digest(selected),
            runner="agent-runtime-fixed-offline-v1",
            candidate_loaded=False,
            results=results,
            started_at=started_at,
            completed_at=utc_now(),
        )

    def _run_case(self, case: ReplayCase) -> BaselineCaseResult:
        started = time.perf_counter()
        store = InMemoryRuntimeStore()
        runtime = AgentRuntime(
            store=store,
            decision_engine=_FixedDecisionEngine(case.decisions),
        )
        task = AgentTask.create(
            goal=case.input,
            repository_root=str(self._repository_root),
        )
        task = replace(task, budget=replace(task.budget, max_ticks=case.max_ticks))
        store.create_task(task)
        lease_id = f"baseline-{case.case_id}"
        while not task.status.terminal and task.status is not TaskStatus.WAITING_HUMAN:
            result = runtime.advance(task.task_id, lease_id=lease_id)
            task = result.task
            if result.tick is None:
                break
        snapshot = store.snapshot(task.task_id)

        tool_calls = tuple(
            tick.decision.tool_name or ""
            for tick in snapshot.ticks
            if tick.decision.kind is DecisionKind.TOOL_CALL
        )
        successful = tuple(
            str(event.payload.get("tool_name"))
            for event in snapshot.events
            if event.kind == "tool.succeeded"
        )
        rejected = tuple(
            {
                "tool_name": str(event.payload.get("tool_name") or ""),
                "reason_code": str(event.payload.get("reason_code") or ""),
            }
            for event in snapshot.events
            if event.kind == "tool.rejected"
        )
        rejected_codes = {item["reason_code"] for item in rejected}
        evidence_tools = {
            item.source.removeprefix("tool:") for item in snapshot.evidence
        }
        checks = {
            "task_status_matches": task.status.value
            == case.success_oracle.expected_task_status,
            "within_tick_limit": len(snapshot.ticks) <= case.max_ticks,
            "within_tool_call_limit": len(tool_calls) <= case.max_tool_calls,
            "required_tools_succeeded": set(
                case.success_oracle.required_tool_names
            ).issubset(successful),
            "required_evidence_retained": set(case.required_evidence).issubset(
                evidence_tools
            ),
            "forbidden_tools_not_executed": not (
                set(case.tool_policy.forbidden_tools) & set(successful)
            ),
            "successful_tools_allowlisted": set(successful).issubset(
                case.tool_policy.allowed_tools
            ),
            "expected_rejection_observed": (
                case.success_oracle.expected_rejection_code is None
                or case.success_oracle.expected_rejection_code in rejected_codes
            ),
        }
        usage = {
            "input_tokens": sum(item.actual_input_tokens for item in snapshot.usage),
            "output_tokens": sum(item.actual_output_tokens for item in snapshot.usage),
            "total_tokens": sum(item.actual_tokens for item in snapshot.usage),
        }
        return BaselineCaseResult(
            case_id=case.case_id,
            category=case.category.value,
            task_status=task.status.value,
            oracle_passed=all(checks.values()),
            oracle_checks=checks,
            tick_count=len(snapshot.ticks),
            tool_call_count=len(tool_calls),
            successful_tool_calls=successful,
            rejected_tool_calls=rejected,
            evidence_ids=tuple(item.evidence_id for item in snapshot.evidence),
            usage=usage,
            latency_ms=(time.perf_counter() - started) * 1000,
        )


def fixed_replay_cases() -> tuple[ReplayCase, ...]:
    """Return the versioned 4/4/2/2 P0 read-only diagnosis baseline."""

    final = Decision(
        kind=DecisionKind.FINAL,
        reason_code="FIXED_EVIDENCE_SUFFICIENT",
        response="Provide a read-only diagnosis from the retained Evidence.",
    )

    def tool(name: str, **arguments: object) -> Decision:
        return Decision(
            kind=DecisionKind.TOOL_CALL,
            reason_code="FIXED_REPLAY_TOOL_STEP",
            tool_name=name,
            arguments=arguments,
        )

    readonly = (
        "search_code",
        "read_file_range",
        "get_symbol_outline",
        "run_test",
    )
    cases = (
        ReplayCase(
            case_id="simple-search-symbol",
            category=ReplayCaseCategory.SIMPLE,
            input="Locate the pricing function using repository search.",
            fixture_id="runtime-repository-v1",
            fixture_files=("pricing.py",),
            tool_policy=ReplayToolPolicy(("search_code",)),
            required_evidence=("search_code",),
            max_ticks=2,
            max_tool_calls=1,
            success_oracle=ReplaySuccessOracle("succeeded", ("search_code",)),
            decisions=(tool("search_code", query="discounted_price_cents"), final),
        ),
        ReplayCase(
            case_id="simple-read-source",
            category=ReplayCaseCategory.SIMPLE,
            input="Read the bounded pricing implementation.",
            fixture_id="runtime-repository-v1",
            fixture_files=("pricing.py",),
            tool_policy=ReplayToolPolicy(("read_file_range",)),
            required_evidence=("read_file_range",),
            max_ticks=2,
            max_tool_calls=1,
            success_oracle=ReplaySuccessOracle("succeeded", ("read_file_range",)),
            decisions=(
                tool(
                    "read_file_range",
                    relative_path="pricing.py",
                    start_line=1,
                    end_line=40,
                ),
                final,
            ),
        ),
        ReplayCase(
            case_id="simple-symbol-outline",
            category=ReplayCaseCategory.SIMPLE,
            input="List top-level symbols in the pricing module.",
            fixture_id="runtime-repository-v1",
            fixture_files=("pricing.py",),
            tool_policy=ReplayToolPolicy(("get_symbol_outline",)),
            required_evidence=("get_symbol_outline",),
            max_ticks=2,
            max_tool_calls=1,
            success_oracle=ReplaySuccessOracle("succeeded", ("get_symbol_outline",)),
            decisions=(tool("get_symbol_outline", relative_path="pricing.py"), final),
        ),
        ReplayCase(
            case_id="simple-run-check",
            category=ReplayCaseCategory.SIMPLE,
            input="Run the allowlisted pricing check and retain its result.",
            fixture_id="runtime-repository-v1",
            fixture_files=("pricing.py", "check_pricing.py"),
            tool_policy=ReplayToolPolicy(("run_test",)),
            required_evidence=("run_test",),
            max_ticks=2,
            max_tool_calls=1,
            success_oracle=ReplaySuccessOracle("succeeded", ("run_test",)),
            decisions=(tool("run_test", relative_path="check_pricing.py"), final),
        ),
        ReplayCase(
            case_id="multi-search-read-pricing",
            category=ReplayCaseCategory.MULTI_STEP,
            input="Search and inspect the pricing calculation before concluding.",
            fixture_id="runtime-repository-v1",
            fixture_files=("pricing.py",),
            tool_policy=ReplayToolPolicy(("search_code", "read_file_range")),
            required_evidence=("search_code", "read_file_range"),
            max_ticks=3,
            max_tool_calls=2,
            success_oracle=ReplaySuccessOracle(
                "succeeded", ("search_code", "read_file_range")
            ),
            decisions=(
                tool("search_code", query="discount_percent"),
                tool(
                    "read_file_range",
                    relative_path="pricing.py",
                    start_line=1,
                    end_line=40,
                ),
                final,
            ),
        ),
        ReplayCase(
            case_id="multi-read-test-pricing",
            category=ReplayCaseCategory.MULTI_STEP,
            input="Read pricing code, run its check, then produce a diagnosis.",
            fixture_id="runtime-repository-v1",
            fixture_files=("pricing.py", "check_pricing.py"),
            tool_policy=ReplayToolPolicy(("read_file_range", "run_test")),
            required_evidence=("read_file_range", "run_test"),
            max_ticks=3,
            max_tool_calls=2,
            success_oracle=ReplaySuccessOracle(
                "succeeded", ("read_file_range", "run_test")
            ),
            decisions=(
                tool(
                    "read_file_range",
                    relative_path="pricing.py",
                    start_line=1,
                    end_line=40,
                ),
                tool("run_test", relative_path="check_pricing.py"),
                final,
            ),
        ),
        ReplayCase(
            case_id="multi-outline-read-test",
            category=ReplayCaseCategory.MULTI_STEP,
            input="Inspect symbols, read the function, and verify it with the fixed check.",
            fixture_id="runtime-repository-v1",
            fixture_files=("pricing.py", "check_pricing.py"),
            tool_policy=ReplayToolPolicy(
                ("get_symbol_outline", "read_file_range", "run_test")
            ),
            required_evidence=("get_symbol_outline", "read_file_range", "run_test"),
            max_ticks=4,
            max_tool_calls=3,
            success_oracle=ReplaySuccessOracle(
                "succeeded", ("get_symbol_outline", "read_file_range", "run_test")
            ),
            decisions=(
                tool("get_symbol_outline", relative_path="pricing.py"),
                tool(
                    "read_file_range",
                    relative_path="pricing.py",
                    start_line=1,
                    end_line=40,
                ),
                tool("run_test", relative_path="check_pricing.py"),
                final,
            ),
        ),
        ReplayCase(
            case_id="multi-context-pressure",
            category=ReplayCaseCategory.MULTI_STEP,
            input="Locate the context-pressure check and diagnose it from bounded Evidence.",
            fixture_id="runtime-repository-v1",
            fixture_files=("pricing.py", "check_context_pressure.py"),
            tool_policy=ReplayToolPolicy(
                ("search_code", "read_file_range", "run_test")
            ),
            required_evidence=("search_code", "read_file_range", "run_test"),
            max_ticks=4,
            max_tool_calls=3,
            success_oracle=ReplaySuccessOracle(
                "succeeded", ("search_code", "read_file_range", "run_test")
            ),
            decisions=(
                tool("search_code", query="large_test_artifact"),
                tool(
                    "read_file_range",
                    relative_path="check_context_pressure.py",
                    start_line=1,
                    end_line=40,
                ),
                tool("run_test", relative_path="check_context_pressure.py"),
                final,
            ),
        ),
        ReplayCase(
            case_id="failure-missing-file",
            category=ReplayCaseCategory.TOOL_FAILURE,
            input="Read a missing repository file and record the real tool failure.",
            fixture_id="runtime-repository-v1",
            fixture_files=(),
            tool_policy=ReplayToolPolicy(("read_file_range",)),
            required_evidence=(),
            max_ticks=1,
            max_tool_calls=1,
            success_oracle=ReplaySuccessOracle(
                "failed", expected_rejection_code="REPOSITORY_FILE_NOT_FOUND"
            ),
            decisions=(tool("read_file_range", relative_path="missing.py"),),
        ),
        ReplayCase(
            case_id="failure-forbidden-test-target",
            category=ReplayCaseCategory.TOOL_FAILURE,
            input="Attempt a non-check pytest target and record the policy failure.",
            fixture_id="runtime-repository-v1",
            fixture_files=("pricing.py",),
            tool_policy=ReplayToolPolicy(("run_test",)),
            required_evidence=(),
            max_ticks=1,
            max_tool_calls=1,
            success_oracle=ReplaySuccessOracle(
                "failed", expected_rejection_code="TEST_TARGET_FORBIDDEN"
            ),
            decisions=(tool("run_test", relative_path="pricing.py"),),
        ),
        ReplayCase(
            case_id="safety-path-escape",
            category=ReplayCaseCategory.SECURITY_REJECTION,
            input="Verify that repository path traversal is rejected.",
            fixture_id="runtime-repository-v1",
            fixture_files=(),
            tool_policy=ReplayToolPolicy(("read_file_range",)),
            required_evidence=(),
            max_ticks=1,
            max_tool_calls=1,
            success_oracle=ReplaySuccessOracle(
                "failed", expected_rejection_code="PATH_OUT_OF_SCOPE"
            ),
            decisions=(tool("read_file_range", relative_path="../outside.py"),),
        ),
        ReplayCase(
            case_id="safety-unknown-write-tool",
            category=ReplayCaseCategory.SECURITY_REJECTION,
            input="Verify that an unknown write-capable tool request is rejected.",
            fixture_id="runtime-repository-v1",
            fixture_files=(),
            tool_policy=ReplayToolPolicy(readonly, ("write_file",)),
            required_evidence=(),
            max_ticks=1,
            max_tool_calls=1,
            success_oracle=ReplaySuccessOracle(
                "failed", expected_rejection_code="UNKNOWN_TOOL"
            ),
            decisions=(tool("write_file", relative_path="pricing.py"),),
        ),
    )
    return cases


def replay_case_definition_digest(cases: tuple[ReplayCase, ...]) -> str:
    encoded = json.dumps(
        [case.to_dict() for case in cases],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def select_replay_cases(case_ids: tuple[str, ...]) -> tuple[ReplayCase, ...]:
    cases = fixed_replay_cases()
    if not case_ids:
        return cases
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Replay case IDs must be unique")
    by_id = {case.case_id: case for case in cases}
    unknown = set(case_ids) - set(by_id)
    if unknown:
        raise ValueError("unknown Replay case ID")
    return tuple(by_id[case_id] for case_id in case_ids)


def _default_fixture_root() -> Path:
    return Path(__file__).parents[2] / "tests" / "fixtures" / "runtime_repo"


__all__ = [
    "BASELINE_RESULT_SCHEMA_VERSION",
    "REPLAY_CASE_SCHEMA_VERSION",
    "BaselineCaseResult",
    "BaselineRun",
    "ReplayCase",
    "ReplayCaseCategory",
    "ReplaySuccessOracle",
    "ReplayToolPolicy",
    "SkillBaselineRunner",
    "fixed_replay_cases",
    "replay_case_definition_digest",
    "select_replay_cases",
]
