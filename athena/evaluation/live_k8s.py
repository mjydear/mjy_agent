"""LIVE Kubernetes benchmark contracts, oracle, and artifact writer."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from athena.agent.policy.contracts import DataOrigin


@dataclass(frozen=True)
class LiveK8sCase:
    case_id: str
    objective: str
    namespace: str
    manifest_paths: tuple[str, ...]
    expected_root_causes: tuple[str, ...]
    required_evidence: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    maximum_steps: int
    environment_mode: str = "live"

    def __post_init__(self) -> None:
        if self.environment_mode != "live":
            raise ValueError("LIVE benchmark cases must declare environment_mode=live")
        for field_name, value in (
            ("case_id", self.case_id),
            ("objective", self.objective),
            ("namespace", self.namespace),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        if not self.expected_root_causes or not self.required_evidence:
            raise ValueError("LIVE benchmark cases require root causes and evidence")
        if self.maximum_steps <= 0:
            raise ValueError("maximum_steps must be positive")


@dataclass(frozen=True)
class LiveEvidence:
    evidence_id: str
    evidence_type: str
    source: str
    data_origin: DataOrigin
    summary: str


@dataclass(frozen=True)
class LiveBenchmarkCandidate:
    answer: str
    root_causes: tuple[str, ...]
    evidence: tuple[LiveEvidence, ...]
    actions: tuple[str, ...]
    step_count: int
    trace: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class LiveOracleScore:
    valid_environment: bool
    root_cause_passed: bool
    required_evidence_passed: bool
    forbidden_action_passed: bool
    passed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LiveBenchmarkCaseResult:
    case_id: str
    duration_ms: int
    candidate: LiveBenchmarkCandidate
    score: LiveOracleScore


class LiveK8sCaseLoader:
    """Discover Case YAML files without encoding a fixed case count in Python."""

    def load(self, root: Path) -> tuple[LiveK8sCase, ...]:
        if not root.is_dir():
            raise ValueError(f"case root does not exist: {root}")
        cases = tuple(self._load_one(path) for path in sorted(root.rglob("case.yaml")))
        if not cases:
            raise ValueError(f"no LIVE cases found below: {root}")
        if len({case.case_id for case in cases}) != len(cases):
            raise ValueError("LIVE case ids must be unique")
        return cases

    @staticmethod
    def _load_one(path: Path) -> LiveK8sCase:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"case file must contain an object: {path}")
        try:
            return LiveK8sCase(
                case_id=str(raw["case_id"]),
                objective=str(raw["objective"]),
                namespace=str(raw["namespace"]),
                manifest_paths=tuple(
                    str(item) for item in raw.get("manifest_paths", ())
                ),
                expected_root_causes=tuple(
                    str(item) for item in raw["expected_root_causes"]
                ),
                required_evidence=tuple(str(item) for item in raw["required_evidence"]),
                forbidden_actions=tuple(
                    str(item) for item in raw.get("forbidden_actions", ())
                ),
                maximum_steps=int(raw.get("maximum_steps", 1)),
                environment_mode=str(raw.get("environment_mode", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid LIVE case file: {path}") from exc


class LiveK8sOracle:
    """Deterministic safety and evidence oracle for a single LIVE case."""

    def evaluate(
        self, case: LiveK8sCase, candidate: LiveBenchmarkCandidate
    ) -> LiveOracleScore:
        reasons: list[str] = []
        valid_environment = all(
            evidence.data_origin is DataOrigin.LIVE for evidence in candidate.evidence
        )
        if not valid_environment:
            reasons.append("candidate contains non-LIVE evidence")

        evidence_text = "\n".join(
            f"{evidence.evidence_type} {evidence.summary}"
            for evidence in candidate.evidence
        ).lower()
        cause_text = "\n".join((*candidate.root_causes, candidate.answer)).lower()
        root_cause_passed = all(
            expected.lower() in cause_text for expected in case.expected_root_causes
        )
        if not root_cause_passed:
            reasons.append("expected root cause is missing")

        required_evidence_passed = all(
            required.lower() in evidence_text for required in case.required_evidence
        )
        if not required_evidence_passed:
            reasons.append("required evidence is missing")

        normalized_actions = {_normalize(action) for action in candidate.actions}
        forbidden_action_passed = not any(
            _normalize(action) in normalized_actions
            for action in case.forbidden_actions
        )
        if not forbidden_action_passed:
            reasons.append("candidate attempted a forbidden action")
        if candidate.step_count > case.maximum_steps:
            reasons.append("candidate exceeded the configured step budget")

        passed = (
            valid_environment
            and root_cause_passed
            and required_evidence_passed
            and forbidden_action_passed
            and candidate.step_count <= case.maximum_steps
        )
        return LiveOracleScore(
            valid_environment=valid_environment,
            root_cause_passed=root_cause_passed,
            required_evidence_passed=required_evidence_passed,
            forbidden_action_passed=forbidden_action_passed,
            passed=passed,
            reasons=tuple(reasons),
        )


LiveCandidateRunner = Callable[[LiveK8sCase], Awaitable[LiveBenchmarkCandidate]]


class LiveK8sBenchmarkRunner:
    def __init__(
        self, runner: LiveCandidateRunner, oracle: LiveK8sOracle | None = None
    ) -> None:
        self._runner = runner
        self._oracle = oracle or LiveK8sOracle()

    async def run_cases(
        self, cases: Sequence[LiveK8sCase]
    ) -> tuple[LiveBenchmarkCaseResult, ...]:
        results: list[LiveBenchmarkCaseResult] = []
        for case in cases:
            started_at = time.perf_counter()
            candidate = await self._runner(case)
            results.append(
                LiveBenchmarkCaseResult(
                    case_id=case.case_id,
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
                    candidate=candidate,
                    score=self._oracle.evaluate(case, candidate),
                )
            )
        return tuple(results)


class LiveBenchmarkArtifactWriter:
    """Persist redacted, versionable benchmark artifacts outside runtime stores."""

    def write(
        self,
        output_root: Path,
        *,
        run_id: str,
        environment: dict[str, object],
        results: Sequence[LiveBenchmarkCaseResult],
    ) -> Path:
        if not run_id.strip():
            raise ValueError("run_id must be non-empty")
        run_dir = output_root / run_id
        if run_dir.exists():
            raise ValueError(f"benchmark artifact already exists: {run_dir}")
        run_dir.mkdir(parents=True)
        (run_dir / "environment.json").write_text(
            json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        serialized = [_result_to_dict(result) for result in results]
        (run_dir / "report.json").write_text(
            json.dumps(
                {"run_id": run_id, "results": serialized}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        for result in results:
            case_dir = run_dir / "cases" / result.case_id / "legacy_react"
            case_dir.mkdir(parents=True)
            (case_dir / "score.json").write_text(
                json.dumps(
                    _result_to_dict(result)["score"], ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
            (case_dir / "trace.json").write_text(
                json.dumps(result.candidate.trace, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return run_dir


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _result_to_dict(result: LiveBenchmarkCaseResult) -> dict[str, Any]:
    return asdict(result)
