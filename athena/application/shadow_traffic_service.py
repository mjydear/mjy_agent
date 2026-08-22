"""Application boundary for capturing completed Runtime tasks for Shadow."""

from __future__ import annotations

from typing import Any

from athena.api.repositories.shadow_traffic_repository import (
    ShadowTrafficObservation,
    ShadowTrafficRepository,
)
from athena.application.shadow_traffic_ingress import ShadowTrafficIngressEvent
from athena.evaluation.shadow_traffic import ShadowTraceEnvelope
from athena.learning.skill_candidate import (
    CANDIDATE_STATUS,
    SkillCandidateLifecycleError,
)
from athena.runtime import TaskStatus


class ShadowTrafficService:
    """Validate a Candidate and enqueue a redacted completed Runtime Trace."""

    def __init__(
        self,
        repository: ShadowTrafficRepository,
        candidate_repository: Any,
        runtime_store: Any,
    ) -> None:
        self._repository = repository
        self._candidate_repository = candidate_repository
        self._runtime_store = runtime_store

    async def capture_runtime_task(
        self,
        tenant_id: str,
        candidate_id: str,
        task_id: str,
        trace_id: str,
        *,
        traceparent: str | None = None,
    ) -> ShadowTrafficObservation:
        validation = await self._require_shadow_candidate(tenant_id, candidate_id)
        snapshot = self._snapshot(task_id)
        return await self._capture_snapshot(
            tenant_id=tenant_id,
            trace_id=trace_id,
            traceparent=traceparent,
            snapshot=snapshot,
            candidate_id=candidate_id,
            candidate_digest=validation.candidate_digest,
        )

    async def _require_shadow_candidate(self, tenant_id: str, candidate_id: str):
        candidate = await self._candidate_repository.get(tenant_id, candidate_id)
        if candidate is None:
            raise SkillCandidateLifecycleError("SKILL_CANDIDATE_NOT_FOUND")
        validation = await self._candidate_repository.latest_validation_for_candidate(
            tenant_id, candidate_id
        )
        if validation is None or not validation.passed:
            raise SkillCandidateLifecycleError("SKILL_CANDIDATE_VALIDATION_REQUIRED")
        if (
            candidate.status != CANDIDATE_STATUS
            or candidate.evaluation_status != "replay_ab_passed"
            or candidate.online_eligible
        ):
            raise SkillCandidateLifecycleError(
                "SKILL_CANDIDATE_SHADOW_PRECONDITION_REQUIRED"
            )
        return validation

    def _snapshot(self, task_id: str):
        try:
            return self._runtime_store.snapshot(task_id)
        except KeyError as exc:
            raise SkillCandidateLifecycleError("RUNTIME_TRACE_NOT_FOUND") from exc

    async def _capture_snapshot(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        traceparent: str | None,
        snapshot,
        candidate_id: str,
        candidate_digest: str,
    ) -> ShadowTrafficObservation:
        envelope = ShadowTraceEnvelope.capture(
            tenant_id=tenant_id,
            trace_id=trace_id,
            traceparent=traceparent,
            snapshot=snapshot,
            candidate_id=candidate_id,
            candidate_digest=candidate_digest,
        )
        return await self._repository.capture(envelope)

    async def get(
        self, tenant_id: str, observation_id: str
    ) -> ShadowTrafficObservation | None:
        return await self._repository.get(tenant_id, observation_id)

    async def capture_ingress_event(
        self, event: ShadowTrafficIngressEvent
    ) -> ShadowTrafficObservation:
        """Resolve a bounded Runtime completion event against trusted state.

        The event intentionally contains no task contents.  The Runtime store
        remains the source of truth for the completed task and the existing
        capture path remains responsible for redaction and idempotency.
        """

        try:
            snapshot = self._runtime_store.snapshot(event.task_id)
        except KeyError as exc:
            raise SkillCandidateLifecycleError("RUNTIME_TRACE_NOT_FOUND") from exc
        if snapshot.task.status.value != event.task_status:
            raise SkillCandidateLifecycleError("SHADOW_INGRESS_TASK_STATUS_MISMATCH")
        return await self.capture_runtime_task(
            event.tenant_id,
            event.candidate_id or "",
            event.task_id,
            event.trace_id,
            traceparent=event.traceparent,
        )


__all__ = ["ShadowTrafficService"]
