"""Runtime Skill candidate observation, evaluation, and human review gates."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace

from athena.runtime.models import RuntimeSnapshot, TaskStatus

from .models import (
    CANDIDATE_STATUS,
    REJECTED_STATUS,
    REPLAY_PENDING_STATUS,
    REVIEW_PENDING_STATUS,
    SHADOW_STATUS,
    ObservationResult,
    OperatorFeedback,
    ReplayCase,
    ReplayReport,
    ReplayResult,
    ReviewGate,
    RuntimeSkillCandidate,
    RuntimeSkillLearningError,
    ShadowCase,
    ShadowReport,
    ShadowResult,
    SkillCandidateHandoff,
    utc_now,
)

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|password|authorization|bearer|secret)"
    r"\s*[:=]\s*[^\s,;]+"
)
_HIDDEN_REASONING = re.compile(
    r"(?i)(?:hidden[\s_-]*thought|chain[\s_-]*of[\s_-]*thought|"
    r"raw[\s_-]*prompt|system[\s_-]*prompt)(?:\s*[:=].*)?"
)


def redact_summary(value: str, *, limit: int = 1_600) -> str:
    """Return a bounded public summary without secret or hidden-reasoning data."""

    if not isinstance(value, str):
        raise RuntimeSkillLearningError("SKILL_LEARNING_SUMMARY_REQUIRED")
    normalized = " ".join(value.split())
    if not normalized:
        raise RuntimeSkillLearningError("SKILL_LEARNING_SUMMARY_REQUIRED")
    redacted = _SENSITIVE_ASSIGNMENT.sub("[REDACTED_SECRET]", normalized)
    redacted = _HIDDEN_REASONING.sub("[REDACTED_REASONING]", redacted)
    return redacted[:limit].rstrip()


class RuntimeSkillLearningObserver:
    """Create a candidate only from verified success, feedback, and Evidence refs."""

    def __init__(
        self,
        *,
        min_evidence: int = 2,
        workflow_type: str = "repository_diagnosis",
        environment_type: str = "repository",
    ) -> None:
        if min_evidence < 1:
            raise ValueError("min_evidence must be at least one")
        self._min_evidence = min_evidence
        self._workflow_type = workflow_type
        self._environment_type = environment_type

    def observe_completed_task(
        self,
        snapshot: RuntimeSnapshot,
        feedback: OperatorFeedback | None,
    ) -> ObservationResult:
        task = snapshot.task
        if task.status is not TaskStatus.SUCCEEDED or task.final_report is None:
            return ObservationResult(candidate=None, blocked_reason="TASK_NOT_SUCCEEDED")
        if feedback is None or not feedback.verified or not feedback.accepted:
            return ObservationResult(
                candidate=None,
                blocked_reason="VERIFIED_OPERATOR_FEEDBACK_REQUIRED",
            )
        if not feedback.feedback_id.strip() or not feedback.submitted_by.strip():
            return ObservationResult(
                candidate=None,
                blocked_reason="VERIFIED_OPERATOR_FEEDBACK_REQUIRED",
            )

        evidence_by_id = {item.evidence_id: item for item in snapshot.evidence}
        evidence_ids = task.final_report.evidence_ids
        if len(set(evidence_ids)) < self._min_evidence:
            return ObservationResult(
                candidate=None,
                blocked_reason="EVIDENCE_THRESHOLD_NOT_MET",
                details={"minimum": self._min_evidence, "observed": len(set(evidence_ids))},
            )
        if any(item not in evidence_by_id for item in evidence_ids):
            return ObservationResult(
                candidate=None,
                blocked_reason="EVIDENCE_REFERENCE_UNVERIFIED",
            )

        root_cause = redact_summary(task.final_report.root_cause)
        recommendation = redact_summary(task.final_report.repair_recommendation)
        feedback_summary = redact_summary(feedback.summary)
        evidence_refs = [
            {
                "evidence_id": evidence_id,
                "source": evidence_by_id[evidence_id].source,
                "summary": redact_summary(evidence_by_id[evidence_id].summary),
            }
            for evidence_id in evidence_ids
        ]
        digest = self._digest(task.task_id, feedback.feedback_id, evidence_ids)
        candidate = RuntimeSkillCandidate(
            candidate_id=f"runtime-skill-candidate-{digest[:24]}",
            name="repository-diagnosis-recommendation",
            workflow_type=self._workflow_type,
            environment_type=self._environment_type,
            capabilities=("repository.read",),
            status=CANDIDATE_STATUS,
            source_task_id=task.task_id,
            source_evidence_ids=evidence_ids,
            feedback_id=feedback.feedback_id.strip(),
            manifest={
                "name": "repository-diagnosis-recommendation",
                "workflow_type": self._workflow_type,
                "environment_type": self._environment_type,
                "capabilities": ["repository.read"],
                "candidate_only": True,
                "creates_tool": False,
                "readonly": True,
                "activation_allowed": False,
            },
            procedure={
                "root_cause": root_cause,
                "recommendation": recommendation,
                "evidence_refs": [item["evidence_id"] for item in evidence_refs],
                "validation": "Replay, shadow mode, and human review are required.",
                "execution_mode": "readonly_recommendation_only",
            },
            source_summary={
                "source_type": "runtime_completed_task",
                "task_id": task.task_id,
                "feedback": {
                    "feedback_id": feedback.feedback_id.strip(),
                    "submitted_by": feedback.submitted_by.strip(),
                    "summary": feedback_summary,
                },
                "evidence": evidence_refs,
            },
            audit_events=(
                {
                    "kind": "candidate.observed",
                    "at": utc_now().isoformat(),
                    "source_task_id": task.task_id,
                    "evidence_ids": list(evidence_ids),
                    "feedback_id": feedback.feedback_id.strip(),
                },
            ),
        )
        return ObservationResult(candidate=candidate)

    @staticmethod
    def _digest(task_id: str, feedback_id: str, evidence_ids: tuple[str, ...]) -> str:
        encoded = json.dumps(
            {
                "task_id": task_id,
                "feedback_id": feedback_id.strip(),
                "evidence_ids": list(evidence_ids),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class RuntimeSkillReplayEvaluator:
    """Evaluate a candidate against fixed fixtures without invoking a tool."""

    def evaluate(
        self,
        candidate: RuntimeSkillCandidate,
        cases: tuple[ReplayCase, ...],
        *,
        min_pass_rate: float = 1.0,
    ) -> ReplayReport:
        if not cases:
            raise ValueError("at least one replay case is required")
        if not 0.0 <= min_pass_rate <= 1.0:
            raise ValueError("min_pass_rate must be between 0 and 1")
        results = tuple(self._evaluate_case(candidate, case) for case in cases)
        pass_rate = sum(item.passed for item in results) / len(results)
        return ReplayReport(
            report_id=self._report_id(candidate.candidate_id, "replay", results),
            candidate_id=candidate.candidate_id,
            passed=pass_rate >= min_pass_rate,
            pass_rate=pass_rate,
            results=results,
        )

    @staticmethod
    def _evaluate_case(
        candidate: RuntimeSkillCandidate, case: ReplayCase
    ) -> ReplayResult:
        if not set(case.required_evidence_ids).issubset(candidate.source_evidence_ids):
            return ReplayResult(case.case_id, False, "REPLAY_EVIDENCE_MISMATCH")
        if candidate.manifest.get("creates_tool") or not candidate.manifest.get("readonly"):
            return ReplayResult(case.case_id, False, "REPLAY_EFFECT_BOUNDARY_VIOLATION")
        root_cause = str(candidate.procedure.get("root_cause", ""))
        if root_cause != redact_summary(case.expected_root_cause):
            return ReplayResult(case.case_id, False, "REPLAY_ROOT_CAUSE_MISMATCH")
        return ReplayResult(case.case_id, True, "REPLAY_PASSED")

    @staticmethod
    def _report_id(
        candidate_id: str, phase: str, results: tuple[ReplayResult, ...]
    ) -> str:
        return _report_id(candidate_id, phase, results)


class RuntimeSkillShadowEvaluator:
    """Compare a candidate to observed cases while prohibiting all effects."""

    def evaluate(
        self,
        candidate: RuntimeSkillCandidate,
        cases: tuple[ShadowCase, ...],
        *,
        min_pass_rate: float = 1.0,
    ) -> ShadowReport:
        if not cases:
            raise ValueError("at least one shadow case is required")
        if not 0.0 <= min_pass_rate <= 1.0:
            raise ValueError("min_pass_rate must be between 0 and 1")
        results = tuple(self._evaluate_case(candidate, case) for case in cases)
        pass_rate = sum(item.passed for item in results) / len(results)
        return ShadowReport(
            report_id=_report_id(candidate.candidate_id, "shadow", results),
            candidate_id=candidate.candidate_id,
            passed=pass_rate >= min_pass_rate,
            pass_rate=pass_rate,
            results=results,
        )

    @staticmethod
    def _evaluate_case(
        candidate: RuntimeSkillCandidate, case: ShadowCase
    ) -> ShadowResult:
        if case.effect_count != 0:
            return ShadowResult(case.case_id, False, "SHADOW_EFFECT_DETECTED")
        if not set(candidate.source_evidence_ids).intersection(case.observed_evidence_ids):
            return ShadowResult(case.case_id, False, "SHADOW_EVIDENCE_MISMATCH")
        root_cause = str(candidate.procedure.get("root_cause", ""))
        if root_cause != redact_summary(case.observed_root_cause):
            return ShadowResult(case.case_id, False, "SHADOW_ROOT_CAUSE_MISMATCH")
        return ShadowResult(case.case_id, True, "SHADOW_PASSED")


class RuntimeSkillLearningLifecycle:
    """Enforce candidate -> replay -> shadow -> review without ACTIVE state."""

    def mark_replay_pending(
        self, candidate: RuntimeSkillCandidate
    ) -> RuntimeSkillCandidate:
        return self._transition(candidate, (CANDIDATE_STATUS,), REPLAY_PENDING_STATUS)

    def record_replay(
        self, candidate: RuntimeSkillCandidate, report: ReplayReport
    ) -> RuntimeSkillCandidate:
        self._validate_report(candidate, report.candidate_id, "replay")
        target = SHADOW_STATUS if report.passed else REJECTED_STATUS
        return self._transition(
            candidate,
            (REPLAY_PENDING_STATUS,),
            target,
            replay_report_id=report.report_id,
            report_passed=report.passed,
        )

    def record_shadow(
        self, candidate: RuntimeSkillCandidate, report: ShadowReport
    ) -> RuntimeSkillCandidate:
        self._validate_report(candidate, report.candidate_id, "shadow")
        target = REVIEW_PENDING_STATUS if report.passed else REJECTED_STATUS
        return self._transition(
            candidate,
            (SHADOW_STATUS,),
            target,
            shadow_report_id=report.report_id,
            report_passed=report.passed,
        )

    def review(
        self, candidate: RuntimeSkillCandidate, gate: ReviewGate
    ) -> RuntimeSkillCandidate:
        if candidate.status != REVIEW_PENDING_STATUS:
            raise RuntimeSkillLearningError("SKILL_CANDIDATE_NOT_REVIEW_READY")
        if not gate.reviewer.strip() or not gate.note.strip():
            raise RuntimeSkillLearningError("SKILL_CANDIDATE_REVIEWER_AND_NOTE_REQUIRED")
        target = REVIEW_PENDING_STATUS if gate.approved else REJECTED_STATUS
        reviewed = replace(
            candidate,
            status=target,
            reviewed_by=gate.reviewer.strip(),
            review_note=redact_summary(gate.note),
            review_approved=gate.approved,
            decided_at=utc_now(),
        )
        return reviewed.with_audit_event(
            "candidate.reviewed",
            reviewer=gate.reviewer.strip(),
            approved=gate.approved,
            activation_allowed=False,
        )

    def handoff(self, candidate: RuntimeSkillCandidate) -> SkillCandidateHandoff:
        if not candidate.handoff_ready:
            raise RuntimeSkillLearningError("SKILL_CANDIDATE_NOT_HUMAN_APPROVED")
        return SkillCandidateHandoff(
            candidate_id=candidate.candidate_id,
            manifest=dict(candidate.manifest),
            procedure=dict(candidate.procedure),
            audit={
                "action": "manual_human_draft_creation_required",
                "source_task_id": candidate.source_task_id,
                "source_evidence_ids": list(candidate.source_evidence_ids),
                "feedback_id": candidate.feedback_id,
                "replay_report_id": candidate.replay_report_id,
                "shadow_report_id": candidate.shadow_report_id,
                "reviewed_by": candidate.reviewed_by,
            },
        )

    @staticmethod
    def _validate_report(
        candidate: RuntimeSkillCandidate, report_candidate_id: str, phase: str
    ) -> None:
        if candidate.candidate_id != report_candidate_id:
            raise RuntimeSkillLearningError("SKILL_CANDIDATE_REPORT_MISMATCH")
        if phase == "replay" and candidate.status == SHADOW_STATUS:
            raise RuntimeSkillLearningError("SKILL_CANDIDATE_REPLAY_ALREADY_RECORDED")
        if phase == "shadow" and candidate.status == REVIEW_PENDING_STATUS:
            raise RuntimeSkillLearningError("SKILL_CANDIDATE_SHADOW_ALREADY_RECORDED")

    @staticmethod
    def _transition(
        candidate: RuntimeSkillCandidate,
        expected: tuple[str, ...],
        target: str,
        **details: object,
    ) -> RuntimeSkillCandidate:
        if candidate.status not in expected:
            raise RuntimeSkillLearningError("SKILL_CANDIDATE_INVALID_TRANSITION")
        candidate_fields = {
            key: value
            for key, value in details.items()
            if key in {"replay_report_id", "shadow_report_id"}
        }
        transition = replace(candidate, status=target, **candidate_fields)
        return transition.with_audit_event(
            "candidate.transition",
            from_status=candidate.status,
            to_status=target,
            **details,
        )


def _report_id(candidate_id: str, phase: str, results: tuple[object, ...]) -> str:
    encoded = json.dumps(
        {
            "candidate_id": candidate_id,
            "phase": phase,
            "results": [
                {
                    "case_id": getattr(item, "case_id"),
                    "passed": getattr(item, "passed"),
                    "reason_code": getattr(item, "reason_code"),
                }
                for item in results
            ],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"runtime-skill-{phase}-{hashlib.sha256(encoded).hexdigest()[:16]}"


__all__ = [
    "RuntimeSkillLearningLifecycle",
    "RuntimeSkillLearningObserver",
    "RuntimeSkillReplayEvaluator",
    "RuntimeSkillShadowEvaluator",
    "redact_summary",
]
