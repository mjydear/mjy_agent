"""Offline Skill replay evaluation before human review and activation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from athena.api.repositories.skill_repository import SkillVersion


@dataclass(frozen=True)
class SkillReplayCase:
    case_id: str
    workflow_type: str
    required_capabilities: frozenset[str]
    event_reasons: tuple[str, ...]
    expected_root_cause: str


@dataclass(frozen=True)
class SkillReplayResult:
    case_id: str
    passed: bool
    reason_code: str
    predicted_root_cause: str | None


@dataclass(frozen=True)
class SkillReplayReport:
    report_id: str
    version_id: str
    passed: bool
    pass_rate: float
    results: tuple[SkillReplayResult, ...]


class SkillReplayEvaluator:
    """Deterministic replay gate for readonly procedural Skills."""

    def evaluate(
        self,
        version: SkillVersion,
        cases: tuple[SkillReplayCase, ...],
        *,
        min_pass_rate: float = 1.0,
    ) -> SkillReplayReport:
        if not cases:
            raise ValueError("at least one replay case is required")
        if not 0.0 <= min_pass_rate <= 1.0:
            raise ValueError("min_pass_rate must be between 0 and 1")
        results = tuple(self._evaluate_case(version, case) for case in cases)
        pass_rate = sum(1 for result in results if result.passed) / len(results)
        passed = pass_rate >= min_pass_rate
        report_id = self._report_id(version, cases, results)
        return SkillReplayReport(
            report_id=report_id,
            version_id=version.version_id,
            passed=passed,
            pass_rate=pass_rate,
            results=results,
        )

    def _evaluate_case(
        self, version: SkillVersion, case: SkillReplayCase
    ) -> SkillReplayResult:
        manifest_capabilities = frozenset(
            str(item)
            for item in version.manifest.get("capabilities", [])
            if isinstance(item, str)
        )
        if not case.required_capabilities.issubset(manifest_capabilities):
            return SkillReplayResult(
                case_id=case.case_id,
                passed=False,
                reason_code="REPLAY_CAPABILITY_MISMATCH",
                predicted_root_cause=None,
            )
        if version.manifest.get("script") or version.manifest.get("creates_tool"):
            return SkillReplayResult(
                case_id=case.case_id,
                passed=False,
                reason_code="REPLAY_SCRIPT_BOUNDARY_VIOLATION",
                predicted_root_cause=None,
            )
        steps = tuple(
            str(step).lower()
            for step in version.procedure.get("steps", [])
            if isinstance(step, str)
        )
        if not any("event" in step for step in steps):
            return SkillReplayResult(
                case_id=case.case_id,
                passed=False,
                reason_code="REPLAY_PROCEDURE_MISSING_EVENT_EVIDENCE",
                predicted_root_cause=None,
            )
        predicted = self._predict_root_cause(case)
        return SkillReplayResult(
            case_id=case.case_id,
            passed=predicted == case.expected_root_cause,
            reason_code=(
                "REPLAY_PASSED"
                if predicted == case.expected_root_cause
                else "REPLAY_ROOT_CAUSE_MISMATCH"
            ),
            predicted_root_cause=predicted,
        )

    @staticmethod
    def _predict_root_cause(case: SkillReplayCase) -> str | None:
        # Replay uses a deliberately small, domain-neutral evidence reducer.
        # A production adapter can provide richer case-specific oracles without
        # coupling the Runtime to one backend domain.
        reason_text = " ".join(case.event_reasons).casefold()
        for marker, root_cause in (
            ("dependency", "dependency_unavailable"),
            ("timeout", "request_timeout"),
            ("schema", "schema_mismatch"),
            ("permission", "permission_denied"),
        ):
            if marker in reason_text:
                return root_cause
        return case.event_reasons[0] if len(case.event_reasons) == 1 else None

    @staticmethod
    def _report_id(
        version: SkillVersion,
        cases: tuple[SkillReplayCase, ...],
        results: tuple[SkillReplayResult, ...],
    ) -> str:
        encoded = json.dumps(
            {
                "version_id": version.version_id,
                "checksum": version.checksum,
                "cases": [case.case_id for case in cases],
                "results": [
                    {
                        "case_id": result.case_id,
                        "passed": result.passed,
                        "reason_code": result.reason_code,
                    }
                    for result in results
                ],
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"skill-replay-{hashlib.sha256(encoded).hexdigest()[:16]}"
