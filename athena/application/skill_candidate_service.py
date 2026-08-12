"""Application service for controlled Skill Candidate self-evolution."""

from __future__ import annotations

import hashlib
import re

from athena.api.repositories.skill_candidate_repository import (
    SkillCandidateRepository,
)
from athena.learning.skill_candidate import (
    CANDIDATE_STATUS,
    REJECTED_STATUS,
    REPLAY_PENDING_STATUS,
    REVIEW_PENDING_STATUS,
    SHADOW_STATUS,
    SkillCandidate,
    SkillCandidateBridge,
    SkillCandidateLifecycleError,
    SkillCandidateProposal,
    SkillCandidateSourceError,
    TrajectorySkillCandidateProposal,
    VerifiedLearningSource,
    VerifiedLearningSourceResolver,
    normalize_safe_summary,
    source_digest,
    trajectory_source_digest,
)
from athena.runtime.learning import TrajectoryStatus
from athena.runtime.tools import ReadOnlyToolCatalog
from athena.learning.skill_validation import SKILL_CANDIDATE_SCHEMA_VERSION
from athena.learning.skill_validation import CandidateValidationReport
from athena.application.skill_candidate_validator import SkillCandidateValidator


class SkillCandidateService:
    """Build and govern offline candidates from verified durable learning facts."""

    def __init__(
        self,
        repository: SkillCandidateRepository,
        source_resolver: VerifiedLearningSourceResolver | None = None,
        validator: SkillCandidateValidator | None = None,
    ) -> None:
        self._repository = repository
        self._source_resolver = source_resolver
        self._validator = validator or SkillCandidateValidator()

    async def propose(self, proposal: SkillCandidateProposal) -> SkillCandidate:
        """Create or return one candidate for a verified source set.

        The proposal carries references only.  Raw traces, prompts, model thoughts,
        and credentials have no input path into this method.
        """

        self._validate_proposal(proposal)
        if self._source_resolver is None:
            raise SkillCandidateSourceError("SKILL_CANDIDATE_SOURCE_UNAVAILABLE")
        source = await self._source_resolver.resolve(
            proposal.tenant_id,
            outcome_id=proposal.outcome_id,
            feedback_id=proposal.feedback_id,
            evidence_ids=proposal.evidence_ids,
        )
        self._validate_source(proposal, source)
        assert source is not None

        digest = source_digest(
            proposal.tenant_id,
            proposal.outcome_id,
            proposal.feedback_id,
            proposal.evidence_ids,
        )
        manifest, procedure, source_summary = self._build_candidate_payload(
            proposal, source
        )
        return await self._repository.create_or_get(
            candidate_id=f"skill-candidate-{digest[:32]}",
            tenant_id=proposal.tenant_id,
            name=self._normalize_name(proposal.name),
            workflow_type=proposal.workflow_type.strip(),
            environment_type=proposal.environment_type.strip(),
            capabilities=proposal.capabilities,
            manifest=manifest,
            procedure=procedure,
            source_outcome_id=proposal.outcome_id,
            source_feedback_id=proposal.feedback_id,
            evidence_ids=proposal.evidence_ids,
            source_digest=digest,
            source_summary=source_summary,
            created_by=proposal.created_by.strip(),
        )

    async def propose_from_trajectories(
        self, proposal: TrajectorySkillCandidateProposal
    ) -> SkillCandidate:
        """Persist a Candidate only after every source trajectory is Eligible."""

        self._validate_trajectory_proposal(proposal)
        trajectories = []
        for trajectory_id in proposal.source_trajectory_ids:
            trajectory = await self._repository.get_trajectory(
                proposal.tenant_id, trajectory_id
            )
            if trajectory is None:
                raise SkillCandidateSourceError(
                    "SKILL_CANDIDATE_TRAJECTORY_NOT_FOUND"
                )
            if (
                trajectory.status is not TrajectoryStatus.ELIGIBLE
                or not trajectory.admission.eligible
            ):
                raise SkillCandidateSourceError(
                    "SKILL_CANDIDATE_TRAJECTORY_NOT_ELIGIBLE"
                )
            trajectories.append(trajectory)

        digest = trajectory_source_digest(
            proposal.tenant_id, proposal.source_trajectory_ids
        )
        normalized_name = self._normalize_name(proposal.name)
        description = normalize_safe_summary(
            proposal.description, field="description"
        )
        trigger = self._normalize_safe_structure(proposal.trigger, "trigger")
        success_contract = self._normalize_safe_structure(
            proposal.success_contract, "success_contract"
        )
        procedure_steps = tuple(
            normalize_safe_summary(item, field="procedure")
            for item in proposal.procedure
        )
        failure_recovery = tuple(
            normalize_safe_summary(item, field="failure_recovery")
            for item in proposal.failure_recovery
        )
        evidence_requirements = tuple(
            normalize_safe_summary(item, field="evidence_requirements")
            for item in proposal.evidence_requirements
        )
        skill_id_digest = hashlib.sha256(
            f"{proposal.tenant_id}:{normalized_name}".encode("utf-8")
        ).hexdigest()[:24]
        skill_id = proposal.skill_id or f"candidate-skill-{skill_id_digest}"
        evidence_ids = tuple(
            dict.fromkeys(
                str(item["evidence_id"])
                for trajectory in trajectories
                for item in trajectory.evidence
            )
        )
        manifest: dict[str, object] = {
            "schema_version": SKILL_CANDIDATE_SCHEMA_VERSION,
            "skill_id": skill_id,
            "version": proposal.version,
            "name": normalized_name,
            "description": description,
            "trigger": trigger,
            "allowed_tools": list(proposal.allowed_tools),
            "failure_recovery": list(failure_recovery),
            "success_contract": success_contract,
            "evidence_requirements": list(evidence_requirements),
            "token_budget_hint": proposal.token_budget_hint,
            "source_trajectory_ids": list(proposal.source_trajectory_ids),
            "evaluation_status": "not_evaluated",
            "risk_level": proposal.risk_level,
            "candidate_only": True,
            "creates_tool": False,
            "readonly": True,
            "activation_allowed": False,
        }
        procedure: dict[str, object] = {
            "steps": list(procedure_steps),
            "execution_mode": "readonly_recommendation_only",
        }
        source_summary: dict[str, object] = {
            "source_type": "eligible_runtime_trajectories",
            "trajectory_ids": list(proposal.source_trajectory_ids),
            "source_task_ids": [item.source_task_id for item in trajectories],
            "quality_scores": [
                item.admission.quality_score for item in trajectories
            ],
            "raw_artifacts_included": False,
            "hidden_reasoning_included": False,
        }
        return await self._repository.create_or_get(
            candidate_id=f"skill-candidate-{digest[:32]}",
            tenant_id=proposal.tenant_id,
            name=normalized_name,
            workflow_type="repository_diagnosis",
            environment_type="repository",
            capabilities=("repository.read",),
            manifest=manifest,
            procedure=procedure,
            source_outcome_id=f"trajectory:{proposal.source_trajectory_ids[0]}",
            source_feedback_id="trajectory-admission",
            evidence_ids=evidence_ids,
            source_digest=digest,
            source_summary=source_summary,
            created_by=proposal.created_by.strip(),
            skill_id=skill_id,
            version=proposal.version,
            description=description,
            trigger=trigger,
            allowed_tools=proposal.allowed_tools,
            failure_recovery=failure_recovery,
            success_contract=success_contract,
            evidence_requirements=evidence_requirements,
            token_budget_hint=proposal.token_budget_hint,
            source_trajectory_ids=proposal.source_trajectory_ids,
            evaluation_status="not_evaluated",
            risk_level=proposal.risk_level,
        )

    async def mark_replay_pending(
        self, tenant_id: str, candidate_id: str
    ) -> SkillCandidate | None:
        candidate = await self._repository.get(tenant_id, candidate_id)
        if candidate is None or candidate.status == REPLAY_PENDING_STATUS:
            return candidate
        if (
            candidate.source_trajectory_ids
            and candidate.evaluation_status != "validation_passed"
        ):
            raise SkillCandidateLifecycleError("SKILL_CANDIDATE_VALIDATION_REQUIRED")
        try:
            return await self._repository.mark_replay_pending(tenant_id, candidate_id)
        except SkillCandidateLifecycleError:
            # A duplicate delivery may race with the worker that won the transition.
            current = await self._repository.get(tenant_id, candidate_id)
            if current is not None and current.status == REPLAY_PENDING_STATUS:
                return current
            raise

    async def validate_candidate(
        self, tenant_id: str, candidate_id: str
    ) -> CandidateValidationReport | None:
        """Run and persist static/security checks without loading the Candidate."""

        candidate = await self._repository.get(tenant_id, candidate_id)
        if candidate is None:
            return None
        report = await self._validator.validate(candidate, self._repository)
        return await self._repository.record_validation(report)

    async def get_validation(
        self, tenant_id: str, report_id: str
    ) -> CandidateValidationReport | None:
        return await self._repository.get_validation(tenant_id, report_id)

    async def record_replay(
        self,
        tenant_id: str,
        candidate_id: str,
        *,
        report_id: str,
        passed: bool,
    ) -> SkillCandidate | None:
        report_id = self._normalize_report_id(report_id)
        candidate = await self._repository.get(tenant_id, candidate_id)
        if candidate is None:
            return None
        if self._replay_result_matches(candidate, report_id, passed):
            return candidate
        try:
            return await self._repository.record_replay(
                tenant_id, candidate_id, report_id=report_id, passed=passed
            )
        except SkillCandidateLifecycleError:
            current = await self._repository.get(tenant_id, candidate_id)
            if current is not None and self._replay_result_matches(
                current, report_id, passed
            ):
                return current
            raise

    async def record_shadow(
        self,
        tenant_id: str,
        candidate_id: str,
        *,
        report_id: str,
        passed: bool,
    ) -> SkillCandidate | None:
        report_id = self._normalize_report_id(report_id)
        candidate = await self._repository.get(tenant_id, candidate_id)
        if candidate is None:
            return None
        if self._shadow_result_matches(candidate, report_id, passed):
            return candidate
        try:
            return await self._repository.record_shadow(
                tenant_id, candidate_id, report_id=report_id, passed=passed
            )
        except SkillCandidateLifecycleError:
            current = await self._repository.get(tenant_id, candidate_id)
            if current is not None and self._shadow_result_matches(
                current, report_id, passed
            ):
                return current
            raise

    async def reject(
        self,
        tenant_id: str,
        candidate_id: str,
        *,
        reviewed_by: str,
        note: str,
    ) -> SkillCandidate | None:
        if not reviewed_by.strip() or not note.strip():
            raise ValueError("reviewer and rejection note are required")
        return await self._repository.reject(
            tenant_id,
            candidate_id,
            reviewed_by=reviewed_by.strip(),
            note=note.strip(),
        )

    async def get_skill_repository_bridge(
        self, tenant_id: str, candidate_id: str
    ) -> SkillCandidateBridge | None:
        """Return review-gated audit data, never activate or register anything."""

        return await self._repository.get_bridge(tenant_id, candidate_id)

    @staticmethod
    def _validate_proposal(proposal: SkillCandidateProposal) -> None:
        if not isinstance(proposal, SkillCandidateProposal):
            raise TypeError("proposal must be a SkillCandidateProposal")
        for field_name in (
            "tenant_id",
            "name",
            "workflow_type",
            "environment_type",
            "outcome_id",
            "feedback_id",
            "created_by",
        ):
            value = getattr(proposal, field_name)
            if not isinstance(value, str) or not value.strip():
                raise SkillCandidateSourceError(
                    "SKILL_CANDIDATE_PROPOSAL_FIELD_REQUIRED"
                )
        if not proposal.evidence_ids:
            raise SkillCandidateSourceError("SKILL_CANDIDATE_EVIDENCE_REQUIRED")
        if len(set(proposal.evidence_ids)) != len(proposal.evidence_ids):
            raise SkillCandidateSourceError("SKILL_CANDIDATE_DUPLICATE_EVIDENCE")
        if not proposal.capabilities:
            raise SkillCandidateSourceError("SKILL_CANDIDATE_READONLY_CAPABILITY_REQUIRED")
        if any(
            not isinstance(capability, str) or not capability.endswith(".read")
            for capability in proposal.capabilities
        ):
            raise SkillCandidateSourceError("SKILL_CANDIDATE_WRITE_CAPABILITY_DENIED")

    @staticmethod
    def _validate_trajectory_proposal(
        proposal: TrajectorySkillCandidateProposal,
    ) -> None:
        if not isinstance(proposal, TrajectorySkillCandidateProposal):
            raise TypeError("proposal must be a TrajectorySkillCandidateProposal")
        for value in (
            proposal.tenant_id,
            proposal.name,
            proposal.description,
            proposal.created_by,
        ):
            if not isinstance(value, str) or not value.strip():
                raise SkillCandidateSourceError(
                    "SKILL_CANDIDATE_PROPOSAL_FIELD_REQUIRED"
                )
        if not proposal.source_trajectory_ids:
            raise SkillCandidateSourceError(
                "SKILL_CANDIDATE_TRAJECTORY_REQUIRED"
            )
        if len(set(proposal.source_trajectory_ids)) != len(
            proposal.source_trajectory_ids
        ):
            raise SkillCandidateSourceError(
                "SKILL_CANDIDATE_DUPLICATE_TRAJECTORY"
            )
        if not proposal.trigger or not proposal.procedure:
            raise SkillCandidateSourceError(
                "SKILL_CANDIDATE_PROCEDURE_AND_TRIGGER_REQUIRED"
            )
        if not proposal.failure_recovery or not proposal.success_contract:
            raise SkillCandidateSourceError(
                "SKILL_CANDIDATE_CONTRACT_REQUIRED"
            )
        if not proposal.evidence_requirements:
            raise SkillCandidateSourceError("SKILL_CANDIDATE_EVIDENCE_REQUIRED")
        if proposal.version < 1:
            raise SkillCandidateSourceError("SKILL_CANDIDATE_VERSION_INVALID")
        if not 1 <= proposal.token_budget_hint <= 120_000:
            raise SkillCandidateSourceError(
                "SKILL_CANDIDATE_TOKEN_BUDGET_INVALID"
            )
        if proposal.risk_level != "S1":
            raise SkillCandidateSourceError("SKILL_CANDIDATE_RISK_LEVEL_DENIED")
        readonly_tools = {
            item.name
            for item in ReadOnlyToolCatalog().declarations
            if item.readonly
        }
        if not proposal.allowed_tools or not set(proposal.allowed_tools).issubset(
            readonly_tools
        ):
            raise SkillCandidateSourceError(
                "SKILL_CANDIDATE_TOOL_NOT_READONLY_ALLOWLISTED"
            )

    @staticmethod
    def _validate_source(
        proposal: SkillCandidateProposal,
        source: VerifiedLearningSource | None,
    ) -> None:
        if source is None:
            raise SkillCandidateSourceError("SKILL_CANDIDATE_SOURCE_UNVERIFIED")
        if source.tenant_id != proposal.tenant_id:
            raise SkillCandidateSourceError("SKILL_CANDIDATE_SOURCE_TENANT_MISMATCH")
        if source.outcome_id != proposal.outcome_id or source.feedback_id != proposal.feedback_id:
            raise SkillCandidateSourceError("SKILL_CANDIDATE_SOURCE_REFERENCE_MISMATCH")
        if source.evidence_ids != proposal.evidence_ids:
            raise SkillCandidateSourceError("SKILL_CANDIDATE_SOURCE_REFERENCE_MISMATCH")
        if not source.outcome_verified or not source.feedback_verified:
            raise SkillCandidateSourceError("SKILL_CANDIDATE_SOURCE_UNVERIFIED")
        if not source.evidence:
            raise SkillCandidateSourceError("SKILL_CANDIDATE_EVIDENCE_REQUIRED")
        if len(set(source.evidence_ids)) != len(source.evidence_ids):
            raise SkillCandidateSourceError("SKILL_CANDIDATE_DUPLICATE_EVIDENCE")
        normalize_safe_summary(source.outcome_summary, field="outcome_summary")
        normalize_safe_summary(source.feedback_summary, field="feedback_summary")
        for evidence in source.evidence:
            if not evidence.evidence_id.strip():
                raise SkillCandidateSourceError("SKILL_CANDIDATE_EVIDENCE_REQUIRED")
            normalize_safe_summary(evidence.summary, field="evidence_summary")

    @staticmethod
    def _build_candidate_payload(
        proposal: SkillCandidateProposal,
        source: VerifiedLearningSource,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        outcome_summary = normalize_safe_summary(
            source.outcome_summary, field="outcome_summary"
        )
        feedback_summary = normalize_safe_summary(
            source.feedback_summary, field="feedback_summary"
        )
        evidence = [
            {
                "evidence_id": item.evidence_id,
                "summary": normalize_safe_summary(
                    item.summary, field="evidence_summary"
                ),
            }
            for item in source.evidence
        ]
        manifest: dict[str, object] = {
            "name": SkillCandidateService._normalize_name(proposal.name),
            "workflow_type": proposal.workflow_type.strip(),
            "environment_type": proposal.environment_type.strip(),
            "capabilities": list(proposal.capabilities),
            "candidate_only": True,
            "creates_tool": False,
            "readonly": True,
        }
        procedure: dict[str, object] = {
            "steps": [
                "Collect the referenced Evidence in the task scope.",
                f"Compare the observed facts with the verified Outcome: {outcome_summary}",
                f"Use the operator Feedback as the validation gate: {feedback_summary}",
            ],
            "evidence_refs": [item["evidence_id"] for item in evidence],
            "validation": "A human reviewer must verify the replay and shadow reports before Skill creation.",
            "execution_mode": "readonly_recommendation_only",
        }
        source_summary: dict[str, object] = {
            "source_type": "verified_outcome_feedback_evidence",
            "outcome_id": source.outcome_id,
            "feedback_id": source.feedback_id,
            "evidence": evidence,
            "outcome_summary": outcome_summary,
            "feedback_summary": feedback_summary,
        }
        return manifest, procedure, source_summary

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = re.sub(r"\s+", " ", name.strip())
        if len(normalized) > 160:
            raise ValueError("candidate name is too long")
        return normalized

    @staticmethod
    def _normalize_safe_structure(value: object, field: str) -> object:
        if isinstance(value, str):
            return normalize_safe_summary(value, field=field)
        if isinstance(value, dict):
            return {
                str(key): SkillCandidateService._normalize_safe_structure(
                    item, field
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [
                SkillCandidateService._normalize_safe_structure(item, field)
                for item in value
            ]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        raise SkillCandidateSourceError("SKILL_CANDIDATE_SCHEMA_VALUE_INVALID")

    @staticmethod
    def _normalize_report_id(report_id: str) -> str:
        if not isinstance(report_id, str) or not report_id.strip():
            raise ValueError("report_id must be non-empty")
        normalized = report_id.strip()
        if len(normalized) > 160:
            raise ValueError("report_id is too long")
        return normalized

    @staticmethod
    def _replay_result_matches(
        candidate: SkillCandidate, report_id: str, passed: bool
    ) -> bool:
        if candidate.replay_report_id != report_id:
            return False
        if passed:
            return candidate.status in {SHADOW_STATUS, REVIEW_PENDING_STATUS}
        return candidate.status == REJECTED_STATUS

    @staticmethod
    def _shadow_result_matches(
        candidate: SkillCandidate, report_id: str, passed: bool
    ) -> bool:
        if candidate.shadow_report_id != report_id:
            return False
        if passed:
            return candidate.status == REVIEW_PENDING_STATUS
        return candidate.status == REJECTED_STATUS


__all__ = [
    "SkillCandidateProposal",
    "SkillCandidateService",
    "TrajectorySkillCandidateProposal",
]
