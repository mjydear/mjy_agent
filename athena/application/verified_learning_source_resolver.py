"""Resolve only verified durable facts into Skill Candidate source material."""

from __future__ import annotations

from athena.api.repositories import DiagnosisOutcomeRepository, EvidenceRepository
from athena.learning.skill_candidate import (
    VerifiedEvidenceSummary,
    VerifiedLearningSource,
    VerifiedLearningSourceResolver,
)


class DurableVerifiedLearningSourceResolver(VerifiedLearningSourceResolver):
    """Bridge Outcome/Feedback/Evidence metadata without exposing raw content."""

    def __init__(
        self,
        outcomes: DiagnosisOutcomeRepository,
        evidence: EvidenceRepository,
    ) -> None:
        self._outcomes = outcomes
        self._evidence = evidence

    async def resolve(
        self,
        tenant_id: str,
        *,
        outcome_id: str,
        feedback_id: str,
        evidence_ids: tuple[str, ...],
    ) -> VerifiedLearningSource | None:
        if not self._valid_reference(tenant_id) or not self._valid_reference(
            outcome_id
        ) or not self._valid_reference(feedback_id):
            return None
        if not evidence_ids or any(
            not self._valid_reference(item) for item in evidence_ids
        ):
            return None
        if len(set(evidence_ids)) != len(evidence_ids):
            return None

        outcome = await self._outcomes.get(tenant_id, outcome_id)
        if outcome is None or not outcome.evidence_sufficient:
            return None

        feedbacks = await self._outcomes.get_feedback(
            tenant_id, outcome.task_id, outcome_id
        )
        feedback = next(
            (item for item in feedbacks if item.feedback_id == feedback_id), None
        )
        # Learning requires an operator assessment and observed Recovery. A
        # rejected or unverified diagnosis never becomes a candidate source.
        if (
            feedback is None
            or feedback.feedback_type not in {"confirmed", "corrected"}
            or feedback.recovery is None
        ):
            return None

        requested_ids = tuple(evidence_ids)
        if set(requested_ids) != set(outcome.supporting_evidence_ids):
            return None
        evidence_rows = await self._evidence.list_for_task(tenant_id, outcome.task_id)
        by_id = {item.evidence_id: item for item in evidence_rows}
        selected = tuple(by_id.get(item) for item in requested_ids)
        if any(item is None for item in selected):
            return None
        selected_rows = tuple(item for item in selected if item is not None)
        for item in selected_rows:
            if not await self._evidence.verify_content_hash(
                tenant_id, item.evidence_id
            ):
                return None

        outcome_summary = (
            f"Root Cause: {outcome.root_cause or 'not recorded'}; "
            f"Remediation Recommendation: "
            f"{outcome.remediation_recommendation or 'not recorded'}"
        )
        feedback_parts = [f"Operator Feedback: {feedback.feedback_type}"]
        if feedback.corrected_root_cause:
            feedback_parts.append(f"corrected Root Cause: {feedback.corrected_root_cause}")
        if feedback.corrected_remediation_recommendation:
            feedback_parts.append(
                "corrected Remediation Recommendation: "
                f"{feedback.corrected_remediation_recommendation}"
            )
        feedback_parts.append(f"Recovery: {feedback.recovery.summary}")

        return VerifiedLearningSource(
            tenant_id=tenant_id,
            outcome_id=outcome.outcome_id,
            feedback_id=feedback.feedback_id,
            outcome_verified=True,
            feedback_verified=True,
            outcome_summary=outcome_summary,
            feedback_summary="; ".join(feedback_parts),
            evidence=tuple(
                VerifiedEvidenceSummary(
                    evidence_id=item.evidence_id,
                    summary=item.summary,
                )
                for item in selected_rows
            ),
        )

    @staticmethod
    def _valid_reference(value: str) -> bool:
        return isinstance(value, str) and bool(value.strip()) and value == value.strip()


__all__ = ["DurableVerifiedLearningSourceResolver"]
