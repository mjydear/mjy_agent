"""Application facade for the Runtime Skill candidate lifecycle."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from athena.api.repositories.skill_candidate_repository import (
    SkillCandidateRepository,
)
from athena.runtime.learning import (
    OperatorFeedback,
    ReplayCase,
    ReviewGate,
    RuntimeSkillLearningError,
    RuntimeSkillLearningLifecycle,
    RuntimeSkillLearningObserver,
    RuntimeSkillReplayEvaluator,
    RuntimeSkillShadowEvaluator,
    RuntimeSkillCandidate,
    ShadowCase,
    TrajectorySummary,
    TrajectorySummaryBuilder,
)


class RuntimeLearningService:
    """Keep candidates local to the Runtime adapter until durable Skill storage is added."""

    def __init__(
        self,
        store: Any,
        repository: SkillCandidateRepository | None = None,
        *,
        min_evidence: int = 3,
    ) -> None:
        self._store = store
        self._repository = repository
        self._trajectory_builder = TrajectorySummaryBuilder()
        self._observer = RuntimeSkillLearningObserver(min_evidence=min_evidence)
        self._lifecycle = RuntimeSkillLearningLifecycle()
        self._replay = RuntimeSkillReplayEvaluator()
        self._shadow = RuntimeSkillShadowEvaluator()
        self._candidates: dict[str, RuntimeSkillCandidate] = {}

    async def capture_trajectory(
        self, task_id: str, *, tenant_id: str
    ) -> dict[str, object]:
        summary = self._trajectory_builder.build(
            self._store.snapshot(task_id), tenant_id=tenant_id
        )
        if self._repository is not None:
            summary = await self._repository.save_trajectory(summary)
        return self._trajectory_view(summary)

    async def trajectory(
        self, trajectory_id: str, *, tenant_id: str
    ) -> dict[str, object]:
        if self._repository is None:
            raise RuntimeSkillLearningError("RUNTIME_TRAJECTORY_STORE_UNAVAILABLE")
        summary = await self._repository.get_trajectory(tenant_id, trajectory_id)
        if summary is None:
            raise RuntimeSkillLearningError("RUNTIME_TRAJECTORY_NOT_FOUND")
        result = self._trajectory_view(summary)
        result["events"] = list(
            await self._repository.list_trajectory_events(
                tenant_id, trajectory_id
            )
        )
        return result

    async def observe(
        self,
        task_id: str,
        *,
        tenant_id: str,
        feedback_id: str,
        accepted: bool,
        verified: bool,
        summary: str,
        submitted_by: str,
    ) -> dict[str, object]:
        snapshot = self._store.snapshot(task_id)
        trajectory = self._trajectory_builder.build(
            snapshot, tenant_id=tenant_id
        )
        if self._repository is not None:
            trajectory = await self._repository.save_trajectory(trajectory)
        if not trajectory.admission.eligible:
            return {
                "candidate": None,
                "blocked_reason": trajectory.admission.rejection_reasons[0],
                "details": {
                    "rejection_reasons": list(
                        trajectory.admission.rejection_reasons
                    ),
                    "quality_score": trajectory.admission.quality_score,
                },
                "trajectory": self._trajectory_view(trajectory),
            }
        result = self._observer.observe_completed_task(
            snapshot,
            OperatorFeedback(
                feedback_id=feedback_id,
                accepted=accepted,
                verified=verified,
                summary=summary,
                submitted_by=submitted_by,
            ),
        )
        if result.candidate is not None:
            self._candidates[result.candidate.candidate_id] = result.candidate
        return {
            "candidate": self._candidate_view(result.candidate) if result.candidate else None,
            "blocked_reason": result.blocked_reason,
            "details": result.details,
            "trajectory": self._trajectory_view(trajectory),
        }

    def list(self) -> dict[str, object]:
        return {"items": [self._candidate_view(item) for item in self._candidates.values()]}

    def detail(self, candidate_id: str) -> dict[str, object]:
        return self._candidate_view(self._candidate(candidate_id))

    def replay(self, candidate_id: str, cases: list[dict[str, object]]) -> dict[str, object]:
        candidate = self._candidate(candidate_id)
        pending = self._lifecycle.mark_replay_pending(candidate)
        report = self._replay.evaluate(pending, tuple(self._replay_case(item) for item in cases))
        updated = self._lifecycle.record_replay(pending, report)
        self._candidates[candidate_id] = updated
        return {"candidate": self._candidate_view(updated), "report": asdict(report)}

    def shadow(self, candidate_id: str, cases: list[dict[str, object]]) -> dict[str, object]:
        candidate = self._candidate(candidate_id)
        report = self._shadow.evaluate(
            candidate,
            tuple(self._shadow_case(item) for item in cases),
        )
        updated = self._lifecycle.record_shadow(candidate, report)
        self._candidates[candidate_id] = updated
        return {"candidate": self._candidate_view(updated), "report": asdict(report)}

    def review(
        self, candidate_id: str, *, reviewer: str, approved: bool, note: str
    ) -> dict[str, object]:
        updated = self._lifecycle.review(
            self._candidate(candidate_id),
            ReviewGate(reviewer=reviewer, approved=approved, note=note),
        )
        self._candidates[candidate_id] = updated
        return {"candidate": self._candidate_view(updated)}

    def handoff(self, candidate_id: str) -> dict[str, object]:
        handoff = self._lifecycle.handoff(self._candidate(candidate_id))
        return asdict(handoff)

    def _candidate(self, candidate_id: str) -> RuntimeSkillCandidate:
        try:
            return self._candidates[candidate_id]
        except KeyError as exc:
            raise RuntimeSkillLearningError("RUNTIME_SKILL_CANDIDATE_NOT_FOUND") from exc

    @staticmethod
    def _candidate_view(candidate: RuntimeSkillCandidate | None) -> dict[str, object] | None:
        if candidate is None:
            return None
        return {
            "id": candidate.candidate_id,
            "name": candidate.name,
            "status": candidate.status,
            "source_task_id": candidate.source_task_id,
            "source_evidence_ids": list(candidate.source_evidence_ids),
            "manifest": candidate.manifest,
            "procedure": candidate.procedure,
            "audit_events": list(candidate.audit_events),
            "replay_report_id": candidate.replay_report_id,
            "shadow_report_id": candidate.shadow_report_id,
            "reviewed_by": candidate.reviewed_by,
            "review_approved": candidate.review_approved,
            "handoff_ready": candidate.handoff_ready,
            "activation_allowed": False,
        }

    @staticmethod
    def _trajectory_view(summary: TrajectorySummary) -> dict[str, object]:
        return summary.to_dict()

    @staticmethod
    def _replay_case(value: dict[str, object]) -> ReplayCase:
        return ReplayCase(
            case_id=str(value.get("case_id", "replay-case")),
            expected_root_cause=str(value.get("expected_root_cause", "")),
            required_evidence_ids=tuple(str(item) for item in value.get("required_evidence_ids", [])),
        )

    @staticmethod
    def _shadow_case(value: dict[str, object]) -> ShadowCase:
        return ShadowCase(
            case_id=str(value.get("case_id", "shadow-case")),
            observed_root_cause=str(value.get("observed_root_cause", "")),
            observed_evidence_ids=tuple(str(item) for item in value.get("observed_evidence_ids", [])),
            effect_count=int(value.get("effect_count", 0)),
        )
