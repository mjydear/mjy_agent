"""Human-gated Candidate to Active Skill release application module."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass

from athena.api.repositories.skill_candidate_repository import (
    SkillCandidateRepository,
)
from athena.api.repositories.skill_evaluation_repository import (
    SkillEvaluationRepository,
)
from athena.api.repositories.skill_release_repository import SkillReleaseRepository
from athena.api.repositories.skill_repository import (
    ACTIVE_STATUS,
    SkillLifecycleError,
    SkillRepository,
    SkillVersion,
)
from athena.learning.skill_candidate import (
    CANDIDATE_STATUS,
    REVIEW_PENDING_STATUS,
    SkillCandidateLifecycleError,
)


class SkillReleaseError(RuntimeError):
    """Stable, fail-closed errors for the release application seam."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class SkillReleaseResult:
    """Auditable result returned after a reviewed Skill version is Active."""

    release_id: str
    tenant_id: str
    candidate_id: str
    version: SkillVersion
    validation_report_id: str
    replay_report_id: str
    shadow_report_id: str
    reviewed_by: str
    gate_snapshot: dict[str, object]
    activation_allowed: bool = True


_AUTOMATED_REVIEWER_IDS = frozenset(
    {"auto", "automated", "agent", "bot", "runtime", "system"}
)
_release_locks: dict[tuple[str, str], asyncio.Lock] = {}
_release_locks_guard = asyncio.Lock()


async def _lock_for(tenant_id: str, candidate_id: str) -> asyncio.Lock:
    key = (tenant_id, candidate_id)
    async with _release_locks_guard:
        lock = _release_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _release_locks[key] = lock
        return lock


class SkillReleaseService:
    """Own the small, controlled Candidate release state machine.

    The interface intentionally exposes only ``release`` and ``rollback``.
    Candidate content never receives an execution hook; it is copied into a
    reviewed immutable SkillVersion and activated only by SkillRepository.
    """

    def __init__(
        self,
        candidate_repository: SkillCandidateRepository,
        evaluation_repository: SkillEvaluationRepository,
        skill_repository: SkillRepository,
        release_repository: SkillReleaseRepository | None = None,
    ) -> None:
        self._candidates = candidate_repository
        self._evaluations = evaluation_repository
        self._skills = skill_repository
        # Keep the service easy to compose in unit tests while production can
        # inject the explicit adapter from application startup.
        self._releases = release_repository or SkillReleaseRepository(
            skill_repository._sessions  # noqa: SLF001 - same repository boundary
        )

    async def release(
        self,
        tenant_id: str,
        candidate_id: str,
        *,
        reviewed_by: str,
        note: str = "",
    ) -> SkillReleaseResult:
        reviewer = self._require_human_reviewer(reviewed_by)
        lock = await _lock_for(tenant_id, candidate_id)
        async with lock:
            candidate = await self._candidates.get(tenant_id, candidate_id)
            if candidate is None:
                raise SkillReleaseError("SKILL_RELEASE_CANDIDATE_NOT_FOUND")

            existing = await self._releases.find_version_for_candidate(
                tenant_id, candidate_id
            )
            if existing is not None:
                return self._result_from_existing(candidate_id, existing)

            if candidate.status not in {CANDIDATE_STATUS, REVIEW_PENDING_STATUS}:
                raise SkillReleaseError("SKILL_RELEASE_CANDIDATE_NOT_REVIEW_PENDING")
            if candidate.status == CANDIDATE_STATUS and (
                candidate.evaluation_status != "replay_ab_passed"
                or not candidate.shadow_report_id
            ):
                raise SkillReleaseError("SKILL_RELEASE_CANDIDATE_NOT_REVIEW_PENDING")
            validation, replay, shadow = await self._load_passed_gates(
                tenant_id, candidate_id
            )
            if candidate.status == CANDIDATE_STATUS:
                try:
                    candidate = await self._candidates.mark_review_pending_after_shadow(
                        tenant_id, candidate_id
                    )
                except SkillCandidateLifecycleError as exc:
                    raise SkillReleaseError(
                        "SKILL_RELEASE_CANDIDATE_NOT_REVIEW_PENDING"
                    ) from exc
                if candidate is None:
                    raise SkillReleaseError("SKILL_RELEASE_CANDIDATE_NOT_FOUND")

            manifest = copy.deepcopy(candidate.manifest)
            manifest["capabilities"] = list(candidate.capabilities)
            manifest["candidate_only"] = False
            manifest["activation_allowed"] = True
            manifest["release_provenance"] = {
                "candidate_id": candidate.candidate_id,
                "source_digest": candidate.source_digest,
                "candidate_digest": validation.candidate_digest,
                "validation_report_id": validation.report_id,
                "replay_report_id": replay.run_id,
                "shadow_report_id": shadow.run_id,
            }
            procedure = copy.deepcopy(candidate.procedure)
            procedure["release_provenance"] = dict(manifest["release_provenance"])

            try:
                _, draft = await self._skills.create_draft(
                    tenant_id,
                    name=candidate.name,
                    owner=candidate.created_by,
                    environment_type=candidate.environment_type,
                    capabilities=candidate.capabilities,
                    manifest=manifest,
                    procedure=procedure,
                    created_by=candidate.created_by,
                    source_task_id=candidate.candidate_id,
                    benchmark_report_id=shadow.run_id,
                )
                pending = await self._skills.submit_review(tenant_id, draft.version_id)
                if pending is None:
                    raise SkillReleaseError("SKILL_RELEASE_VERSION_NOT_FOUND")
                active = await self._skills.approve(
                    tenant_id,
                    pending.version_id,
                    reviewed_by=reviewer,
                    note=note.strip(),
                )
                if active is None or active.status != ACTIVE_STATUS:
                    raise SkillReleaseError("SKILL_RELEASE_ACTIVATION_FAILED")
            except SkillReleaseError:
                raise
            except (SkillLifecycleError, ValueError, RuntimeError) as exc:
                raise SkillReleaseError("SKILL_RELEASE_FAILED") from exc

            return SkillReleaseResult(
                release_id=active.version_id,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                version=active,
                validation_report_id=validation.report_id,
                replay_report_id=replay.run_id,
                shadow_report_id=shadow.run_id,
                reviewed_by=reviewer,
                gate_snapshot={
                    "validation": validation.to_dict(),
                    "replay_ab": {
                        "run_id": replay.run_id,
                        "status": replay.status,
                        "gate_passed": replay.gate_passed,
                    },
                    "shadow": {
                        "run_id": shadow.run_id,
                        "status": shadow.status,
                        "gate_passed": shadow.gate_passed,
                    },
                },
            )

    async def rollback(
        self,
        tenant_id: str,
        *,
        skill_id: str,
        target_version_id: str,
        reviewed_by: str,
        note: str = "rollback",
    ) -> SkillReleaseResult:
        reviewer = self._require_human_reviewer(reviewed_by)
        if not note.strip():
            raise SkillReleaseError("SKILL_RELEASE_ROLLBACK_NOTE_REQUIRED")
        try:
            version = await self._skills.rollback(
                tenant_id,
                skill_id=skill_id,
                target_version_id=target_version_id,
                reviewed_by=reviewer,
                note=note.strip(),
            )
        except SkillLifecycleError as exc:
            raise SkillReleaseError("SKILL_RELEASE_ROLLBACK_FAILED") from exc
        if version is None or version.status != ACTIVE_STATUS:
            raise SkillReleaseError("SKILL_RELEASE_ROLLBACK_TARGET_NOT_FOUND")
        return SkillReleaseResult(
            release_id=f"rollback:{version.version_id}",
            tenant_id=tenant_id,
            candidate_id=version.source_task_id or "",
            version=version,
            validation_report_id="",
            replay_report_id="",
            shadow_report_id="",
            reviewed_by=reviewer,
            gate_snapshot={
                "action": "rollback",
                "target_version_id": version.version_id,
            },
        )

    async def _load_passed_gates(self, tenant_id: str, candidate_id: str):
        candidate = await self._candidates.get(tenant_id, candidate_id)
        if candidate is None:
            raise SkillReleaseError("SKILL_RELEASE_CANDIDATE_NOT_FOUND")
        validation = await self._candidates.latest_validation_for_candidate(
            tenant_id, candidate_id
        )
        if (
            validation is None
            or not validation.passed
            or validation.tenant_id != tenant_id
            or validation.candidate_id != candidate_id
        ):
            raise SkillReleaseError("SKILL_RELEASE_VALIDATION_GATE_REQUIRED")
        if not candidate.replay_report_id:
            raise SkillReleaseError("SKILL_RELEASE_REPLAY_GATE_REQUIRED")
        replay = await self._evaluations.get_replay_ab(
            tenant_id, candidate.replay_report_id
        )
        if (
            replay is None
            or replay.tenant_id != tenant_id
            or replay.candidate_id != candidate_id
            or replay.candidate_digest != validation.candidate_digest
            or replay.status != "passed"
            or not replay.gate_passed
        ):
            raise SkillReleaseError("SKILL_RELEASE_REPLAY_GATE_REQUIRED")
        if not candidate.shadow_report_id:
            raise SkillReleaseError("SKILL_RELEASE_SHADOW_GATE_REQUIRED")
        shadow = await self._evaluations.get_shadow(
            tenant_id, candidate.shadow_report_id
        )
        if (
            shadow is None
            or shadow.tenant_id != tenant_id
            or shadow.candidate_id != candidate_id
            or shadow.candidate_digest != validation.candidate_digest
            or shadow.status != "passed"
            or not shadow.gate_passed
        ):
            raise SkillReleaseError("SKILL_RELEASE_SHADOW_GATE_REQUIRED")
        return validation, replay, shadow

    @staticmethod
    def _require_human_reviewer(reviewed_by: str) -> str:
        reviewer = reviewed_by.strip() if isinstance(reviewed_by, str) else ""
        if not reviewer or reviewer.casefold() in _AUTOMATED_REVIEWER_IDS:
            raise SkillReleaseError("SKILL_RELEASE_HUMAN_REVIEWER_REQUIRED")
        return reviewer

    @staticmethod
    def _result_from_existing(candidate, version: SkillVersion) -> SkillReleaseResult:
        provenance = dict(version.manifest.get("release_provenance") or {})
        return SkillReleaseResult(
            release_id=version.version_id,
            tenant_id=version.tenant_id,
            candidate_id=candidate,
            version=version,
            validation_report_id=str(provenance.get("validation_report_id") or ""),
            replay_report_id=str(provenance.get("replay_report_id") or ""),
            shadow_report_id=str(provenance.get("shadow_report_id") or ""),
            reviewed_by=version.reviewed_by or "",
            gate_snapshot={"idempotent_replay": True},
        )


__all__ = ["SkillReleaseError", "SkillReleaseResult", "SkillReleaseService"]
