"""Explicit Eligible-trajectory to validated Candidate generation orchestration."""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from time import perf_counter
from uuid import uuid4

from athena.api.repositories.skill_candidate_repository import (
    SkillCandidateRepository,
)
from athena.application.skill_candidate_service import SkillCandidateService
from athena.learning.candidate_generation import (
    CANDIDATE_GENERATOR_SCHEMA_VERSION,
    GENERATION_RULE_VERSION,
    CandidateGenerationError,
    CandidateGenerationPayload,
    CandidateGenerationRun,
    CandidateGenerator,
    TrajectoryDigest,
    TrajectoryDigestBuilder,
)
from athena.learning.skill_candidate import (
    SkillCandidate,
    SkillCandidateError,
    TrajectorySkillCandidateProposal,
    trajectory_source_digest,
)
from athena.runtime.learning import TrajectoryStatus
from athena.runtime.models import utc_now

_SAFE_FAILURE_MESSAGES = {
    "CANDIDATE_GENERATION_SOURCE_COUNT_INVALID": "The trajectory source count is invalid.",
    "CANDIDATE_GENERATION_SOURCE_NOT_FOUND": "A source trajectory was not found.",
    "CANDIDATE_GENERATION_SOURCE_NOT_ELIGIBLE": "Every source trajectory must be Eligible.",
    "CANDIDATE_GENERATION_TENANT_MISMATCH": "Trajectory tenant ownership does not match.",
    "CANDIDATE_GENERATION_NO_SAFE_TOOLS": "No observed read-only tool is available.",
    "CANDIDATE_GENERATION_DIGEST_TEXT_INVALID": "The redacted trajectory digest is invalid.",
    "CANDIDATE_GENERATION_TIMEOUT": "The Candidate generator timed out.",
    "CANDIDATE_GENERATION_PROVIDER_FAILED": "The Candidate generator provider failed.",
    "CANDIDATE_GENERATION_OUTPUT_INVALID": "The generator returned an invalid structured payload.",
    "CANDIDATE_GENERATION_OUTPUT_UNSAFE": "The generated payload failed source safety checks.",
    "CANDIDATE_GENERATION_TOOL_OVERREACH": "The generated payload requested an unobserved tool.",
    "CANDIDATE_VALIDATION_FAILED": "The generated Candidate failed static validation.",
    "CANDIDATE_GENERATION_INTERNAL_FAILED": "Candidate generation failed safely.",
}


class SkillCandidateGenerationService:
    """Run the model only through an explicit call and persist every outcome."""

    def __init__(
        self,
        repository: SkillCandidateRepository,
        candidate_service: SkillCandidateService,
        generator: CandidateGenerator,
        *,
        digest_builder: TrajectoryDigestBuilder | None = None,
    ) -> None:
        self._repository = repository
        self._candidate_service = candidate_service
        self._generator = generator
        self._digest_builder = digest_builder or TrajectoryDigestBuilder()

    async def generate(
        self,
        *,
        tenant_id: str,
        source_trajectory_ids: tuple[str, ...],
        created_by: str,
    ) -> CandidateGenerationRun:
        source_ids = tuple(
            dict.fromkeys(
                item.strip() for item in source_trajectory_ids if item.strip()
            )
        )
        source_digest = trajectory_source_digest(tenant_id, source_ids)
        existing_run = await self._repository.get_generation_by_source(
            tenant_id, source_digest
        )
        if existing_run is not None:
            return existing_run

        trajectories = []
        source_error: str | None = None
        if not source_ids or len(source_ids) > 20:
            source_error = "CANDIDATE_GENERATION_SOURCE_COUNT_INVALID"
        else:
            for trajectory_id in source_ids:
                trajectory = await self._repository.get_trajectory(
                    tenant_id, trajectory_id
                )
                if trajectory is None:
                    source_error = "CANDIDATE_GENERATION_SOURCE_NOT_FOUND"
                    break
                if (
                    trajectory.status is not TrajectoryStatus.ELIGIBLE
                    or not trajectory.admission.eligible
                ):
                    source_error = "CANDIDATE_GENERATION_SOURCE_NOT_ELIGIBLE"
                    break
                trajectories.append(trajectory)

        digest: TrajectoryDigest | None = None
        if source_error is None:
            try:
                digest = self._digest_builder.build(trajectories)
            except CandidateGenerationError as exc:
                source_error = exc.error_code

        run = CandidateGenerationRun(
            run_id=f"candidate-generation-{uuid4().hex}",
            tenant_id=tenant_id,
            source_digest=source_digest,
            source_trajectory_ids=source_ids,
            status="started",
            digest={} if digest is None else digest.to_dict(),
            generator=(
                f"{CANDIDATE_GENERATOR_SCHEMA_VERSION}:"
                f"{type(self._generator).__name__}"
            ),
            created_by=created_by,
            created_at=utc_now(),
        )
        persisted, created = await self._repository.create_generation_run(run)
        if not created:
            return persisted
        if source_error is not None:
            return await self._finish_failure(run, source_error, latency_ms=0)
        assert digest is not None

        exact_candidate = await self._repository.get_by_source_digest(
            tenant_id, source_digest
        )
        if exact_candidate is not None:
            return await self._finish(
                run,
                status="duplicate",
                candidate_id=exact_candidate.candidate_id,
                duplicate_of_candidate_id=exact_candidate.candidate_id,
                deduplication={
                    "rule_version": GENERATION_RULE_VERSION,
                    "kind": "exact_source",
                    "explanation": "The tenant-scoped trajectory source digest already exists.",
                    "model_called": False,
                },
                latency_ms=0,
            )

        started = perf_counter()
        try:
            output = await self._generator.generate(digest)
            latency_ms = max(0, round((perf_counter() - started) * 1000))
            if not set(output.payload.allowed_tools).issubset(digest.available_tools):
                return await self._finish_failure(
                    run,
                    "CANDIDATE_GENERATION_TOOL_OVERREACH",
                    model=output.model,
                    usage=output.usage,
                    latency_ms=latency_ms,
                    status="rejected",
                )

            duplicate, explanation = await self._rule_duplicate(
                tenant_id, output.payload
            )
            if duplicate is not None:
                return await self._finish(
                    run,
                    status="duplicate",
                    duplicate_of_candidate_id=duplicate.candidate_id,
                    candidate_id=duplicate.candidate_id,
                    deduplication=explanation,
                    model=output.model,
                    usage=output.usage,
                    latency_ms=latency_ms,
                )

            try:
                candidate = await self._candidate_service.propose_from_trajectories(
                    _proposal_from_output(
                        tenant_id=tenant_id,
                        source_trajectory_ids=source_ids,
                        created_by=created_by,
                        payload=output.payload,
                    )
                )
            except SkillCandidateError:
                return await self._finish_failure(
                    run,
                    "CANDIDATE_GENERATION_OUTPUT_UNSAFE",
                    model=output.model,
                    usage=output.usage,
                    latency_ms=latency_ms,
                    status="rejected",
                )

            report = await self._candidate_service.validate_candidate(
                tenant_id, candidate.candidate_id
            )
            if report is None:
                return await self._finish_failure(
                    run,
                    "CANDIDATE_GENERATION_INTERNAL_FAILED",
                    model=output.model,
                    usage=output.usage,
                    latency_ms=latency_ms,
                )
            return await self._finish(
                run,
                status="succeeded" if report.passed else "rejected",
                candidate_id=candidate.candidate_id,
                validation_report_id=report.report_id,
                deduplication={
                    "rule_version": GENERATION_RULE_VERSION,
                    "kind": "none",
                    "explanation": "No exact or rule-based duplicate matched.",
                    "model_called": True,
                },
                model=output.model,
                usage=output.usage,
                latency_ms=latency_ms,
                failure_code=None if report.passed else "CANDIDATE_VALIDATION_FAILED",
                failure_message=(
                    None
                    if report.passed
                    else _SAFE_FAILURE_MESSAGES["CANDIDATE_VALIDATION_FAILED"]
                ),
            )
        except CandidateGenerationError as exc:
            return await self._finish_failure(
                run,
                exc.error_code,
                model=exc.model,
                usage=exc.usage,
                latency_ms=max(0, round((perf_counter() - started) * 1000)),
            )
        except Exception:
            return await self._finish_failure(
                run,
                "CANDIDATE_GENERATION_INTERNAL_FAILED",
                latency_ms=max(0, round((perf_counter() - started) * 1000)),
            )

    async def get(self, tenant_id: str, run_id: str) -> CandidateGenerationRun | None:
        return await self._repository.get_generation(tenant_id, run_id)

    async def _rule_duplicate(
        self, tenant_id: str, payload: CandidateGenerationPayload
    ) -> tuple[SkillCandidate | None, dict[str, object]]:
        candidates = await self._repository.list_deduplication_candidates(tenant_id)
        payload_trigger = _canonical_trigger(payload.trigger.model_dump())
        payload_tools = frozenset(payload.allowed_tools)
        payload_procedure = _canonical_procedure(payload.procedure)
        best: tuple[float, SkillCandidate, bool, float] | None = None
        for candidate in candidates:
            trigger_equal = (
                _canonical_trigger(candidate.trigger or {}) == payload_trigger
            )
            tools_equal = frozenset(candidate.allowed_tools) == payload_tools
            similarity = SequenceMatcher(
                None,
                _canonical_procedure(tuple(_candidate_steps(candidate))),
                payload_procedure,
            ).ratio()
            if trigger_equal and tools_equal and similarity >= 0.82:
                score = round((2.0 + similarity) / 3.0, 4)
                if best is None or score > best[0]:
                    best = (score, candidate, tools_equal, similarity)
        if best is None:
            return None, {}
        score, candidate, tools_equal, similarity = best
        return candidate, {
            "rule_version": GENERATION_RULE_VERSION,
            "kind": "semantic_rule",
            "matched_candidate_id": candidate.candidate_id,
            "canonical_trigger_equal": True,
            "tool_set_equal": tools_equal,
            "procedure_similarity": round(similarity, 4),
            "threshold": 0.82,
            "score": score,
            "explanation": (
                "Canonical trigger and tool set are equal and normalized procedure "
                "similarity meets the fixed threshold."
            ),
            "model_called": True,
        }

    async def _finish_failure(
        self,
        run: CandidateGenerationRun,
        error_code: str,
        *,
        model: str | None = None,
        usage: dict[str, int] | None = None,
        latency_ms: int,
        status: str = "failed",
    ) -> CandidateGenerationRun:
        return await self._finish(
            run,
            status=status,
            model=model,
            usage=usage,
            latency_ms=latency_ms,
            failure_code=error_code,
            failure_message=_SAFE_FAILURE_MESSAGES.get(
                error_code, "Candidate generation failed safely."
            ),
        )

    async def _finish(
        self,
        run: CandidateGenerationRun,
        *,
        status: str,
        candidate_id: str | None = None,
        validation_report_id: str | None = None,
        duplicate_of_candidate_id: str | None = None,
        deduplication: dict[str, object] | None = None,
        model: str | None = None,
        usage: dict[str, int] | None = None,
        latency_ms: int | None = None,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> CandidateGenerationRun:
        completed = await self._repository.complete_generation_run(
            run.tenant_id,
            run.run_id,
            status=status,
            candidate_id=candidate_id,
            validation_report_id=validation_report_id,
            duplicate_of_candidate_id=duplicate_of_candidate_id,
            deduplication=deduplication,
            model=model,
            usage=usage,
            latency_ms=latency_ms,
            failure_code=failure_code,
            failure_message=failure_message,
        )
        if completed is None:
            raise RuntimeError("candidate generation run disappeared")
        return completed


def _proposal_from_output(
    *,
    tenant_id: str,
    source_trajectory_ids: tuple[str, ...],
    created_by: str,
    payload: CandidateGenerationPayload,
) -> TrajectorySkillCandidateProposal:
    return TrajectorySkillCandidateProposal(
        tenant_id=tenant_id,
        name=payload.name,
        description=payload.description,
        trigger=payload.trigger.model_dump(),
        allowed_tools=payload.allowed_tools,
        procedure=payload.procedure,
        failure_recovery=payload.failure_recovery,
        success_contract=payload.success_contract.model_dump(),
        evidence_requirements=payload.evidence_requirements,
        token_budget_hint=payload.token_budget_hint,
        source_trajectory_ids=source_trajectory_ids,
        created_by=created_by,
        version=payload.version,
        risk_level=payload.risk_level,
        skill_id=payload.skill_id,
    )


def _canonical_trigger(trigger: dict[str, object]) -> str:
    normalized = {
        str(key).casefold(): (
            sorted(str(item).strip().casefold() for item in value)
            if isinstance(value, (list, tuple))
            else str(value).strip().casefold()
        )
        for key, value in trigger.items()
    }
    return json.dumps(
        normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def _canonical_procedure(steps: tuple[str, ...]) -> str:
    return " ".join(re.findall(r"[a-z0-9_]+", " ".join(steps).casefold()))


def _candidate_steps(candidate: SkillCandidate) -> tuple[str, ...]:
    raw = candidate.procedure.get("steps", [])
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw)


__all__ = ["SkillCandidateGenerationService"]
