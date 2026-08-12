"""Offline Skill Candidate lifecycle APIs; no endpoint activates a Skill."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from athena.api.auth import TenantContext
from athena.api.rbac import require_scope
from athena.api.response import ApiResponse
from athena.api.services import ApiServiceError
from athena.application.skill_candidate_service import SkillCandidateService
from athena.application.skill_candidate_generation_service import (
    SkillCandidateGenerationService,
)
from athena.learning.skill_candidate import (
    SkillCandidate,
    SkillCandidateBridge,
    SkillCandidateError,
    SkillCandidateProposal,
    TrajectorySkillCandidateProposal,
)
from athena.learning.skill_validation import CandidateValidationReport

router = APIRouter(prefix="/api/skill-candidates", tags=["skill-candidates"])


class SkillCandidateProposalRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    workflow_type: str = Field(min_length=1, max_length=80)
    environment_type: str = Field(default="kubernetes", min_length=1, max_length=80)
    capabilities: tuple[str, ...] = Field(min_length=1, max_length=20)
    outcome_id: str = Field(min_length=1, max_length=256)
    feedback_id: str = Field(min_length=1, max_length=256)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=100)


class ReplayRequest(BaseModel):
    report_id: str = Field(min_length=1, max_length=160)
    passed: bool


class TrajectorySkillCandidateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=2_000)
    trigger: dict[str, object]
    allowed_tools: tuple[str, ...] = Field(min_length=1, max_length=20)
    procedure: tuple[str, ...] = Field(min_length=1, max_length=50)
    failure_recovery: tuple[str, ...] = Field(min_length=1, max_length=20)
    success_contract: dict[str, object]
    evidence_requirements: tuple[str, ...] = Field(min_length=1, max_length=50)
    token_budget_hint: int = Field(ge=1, le=120_000)
    source_trajectory_ids: tuple[str, ...] = Field(min_length=1, max_length=50)
    version: int = Field(default=1, ge=1)
    risk_level: Literal["S1"] = "S1"


class RejectRequest(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class CandidateGenerationRequest(BaseModel):
    source_trajectory_ids: tuple[str, ...] = Field(min_length=1, max_length=20)


def _service(request: Request) -> SkillCandidateService:
    service = getattr(request.app.state, "skill_candidate_service", None)
    if service is None:
        raise ApiServiceError(
            "SKILL_CANDIDATE_SERVICE_UNAVAILABLE",
            "Skill Candidate persistence is not configured",
            status_code=503,
        )
    return service


def _generation_service(request: Request) -> SkillCandidateGenerationService:
    service = getattr(request.app.state, "skill_candidate_generation_service", None)
    if service is None:
        raise ApiServiceError(
            "SKILL_CANDIDATE_GENERATOR_UNAVAILABLE",
            "Skill Candidate generation is not configured",
            status_code=503,
        )
    return service


def _candidate_view(candidate: SkillCandidate) -> dict[str, object]:
    return {
        "id": candidate.candidate_id,
        "tenant_id": candidate.tenant_id,
        "name": candidate.name,
        "workflow_type": candidate.workflow_type,
        "environment_type": candidate.environment_type,
        "capabilities": list(candidate.capabilities),
        "manifest": candidate.manifest,
        "procedure": candidate.procedure,
        "status": candidate.status,
        "source_outcome_id": candidate.source_outcome_id,
        "source_feedback_id": candidate.source_feedback_id,
        "evidence_ids": list(candidate.evidence_ids),
        "replay_report_id": candidate.replay_report_id,
        "shadow_report_id": candidate.shadow_report_id,
        "online_eligible": candidate.online_eligible,
        "schema_version": candidate.schema_version,
        "skill_id": candidate.skill_id,
        "version": candidate.version,
        "description": candidate.description,
        "trigger": candidate.trigger or {},
        "allowed_tools": list(candidate.allowed_tools),
        "failure_recovery": list(candidate.failure_recovery),
        "success_contract": candidate.success_contract or {},
        "evidence_requirements": list(candidate.evidence_requirements),
        "token_budget_hint": candidate.token_budget_hint,
        "source_trajectory_ids": list(candidate.source_trajectory_ids),
        "evaluation_status": candidate.evaluation_status,
        "risk_level": candidate.risk_level,
        "audit_events": list(candidate.audit_events),
    }


def _bridge_view(bridge: SkillCandidateBridge) -> dict[str, object]:
    return {
        "candidate_id": bridge.candidate_id,
        "name": bridge.name,
        "environment_type": bridge.environment_type,
        "capabilities": list(bridge.capabilities),
        "manifest": bridge.manifest,
        "procedure": bridge.procedure,
        "source_outcome_id": bridge.source_outcome_id,
        "source_feedback_id": bridge.source_feedback_id,
        "evidence_ids": list(bridge.evidence_ids),
        "replay_report_id": bridge.replay_report_id,
        "shadow_report_id": bridge.shadow_report_id,
        "audit": bridge.audit,
        "activation_allowed": bridge.activation_allowed,
    }


def _validation_view(report: CandidateValidationReport) -> dict[str, object]:
    return report.to_dict()


def _candidate_error(exc: SkillCandidateError) -> ApiServiceError:
    status_code = 409 if "TRANSITION" in exc.error_code or "REVIEW" in exc.error_code else 422
    return ApiServiceError(exc.error_code, str(exc), status_code=status_code)


@router.post("", status_code=status.HTTP_201_CREATED)
async def propose_candidate(
    payload: SkillCandidateProposalRequest,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:write")),
) -> ApiResponse[dict[str, object]]:
    try:
        candidate = await _service(request).propose(
            SkillCandidateProposal(
                tenant_id=tenant.tenant_id,
                name=payload.name,
                workflow_type=payload.workflow_type,
                environment_type=payload.environment_type,
                capabilities=payload.capabilities,
                outcome_id=payload.outcome_id,
                feedback_id=payload.feedback_id,
                evidence_ids=payload.evidence_ids,
                created_by=tenant.tenant_id,
            )
        )
    except SkillCandidateError as exc:
        raise _candidate_error(exc) from exc
    return ApiResponse.ok(_candidate_view(candidate))


@router.post("/from-trajectories", status_code=status.HTTP_201_CREATED)
async def propose_candidate_from_trajectories(
    payload: TrajectorySkillCandidateRequest,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:write")),
) -> ApiResponse[dict[str, object]]:
    try:
        candidate = await _service(request).propose_from_trajectories(
            TrajectorySkillCandidateProposal(
                tenant_id=tenant.tenant_id,
                name=payload.name,
                description=payload.description,
                trigger=payload.trigger,
                allowed_tools=payload.allowed_tools,
                procedure=payload.procedure,
                failure_recovery=payload.failure_recovery,
                success_contract=payload.success_contract,
                evidence_requirements=payload.evidence_requirements,
                token_budget_hint=payload.token_budget_hint,
                source_trajectory_ids=payload.source_trajectory_ids,
                created_by=tenant.tenant_id,
                version=payload.version,
                risk_level=payload.risk_level,
            )
        )
    except SkillCandidateError as exc:
        raise _candidate_error(exc) from exc
    return ApiResponse.ok(_candidate_view(candidate))


@router.post("/generations", status_code=status.HTTP_201_CREATED)
async def generate_candidate_from_trajectories(
    payload: CandidateGenerationRequest,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:write")),
) -> ApiResponse[dict[str, object]]:
    run = await _generation_service(request).generate(
        tenant_id=tenant.tenant_id,
        source_trajectory_ids=payload.source_trajectory_ids,
        created_by=tenant.tenant_id,
    )
    return ApiResponse.ok(run.to_dict())


@router.get("/generations/{run_id}")
async def get_candidate_generation(
    run_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:read")),
) -> ApiResponse[dict[str, object]]:
    run = await _generation_service(request).get(tenant.tenant_id, run_id)
    if run is None:
        raise ApiServiceError(
            "SKILL_CANDIDATE_GENERATION_NOT_FOUND",
            "Candidate generation run not found",
            404,
        )
    return ApiResponse.ok(run.to_dict())


@router.post("/{candidate_id}/replay-pending")
async def mark_replay_pending(
    candidate_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:write")),
) -> ApiResponse[dict[str, object]]:
    candidate = await _service(request).mark_replay_pending(
        tenant.tenant_id, candidate_id
    )
    if candidate is None:
        raise ApiServiceError("SKILL_CANDIDATE_NOT_FOUND", "Skill Candidate not found", 404)
    return ApiResponse.ok(_candidate_view(candidate))


@router.post("/{candidate_id}/validate")
async def validate_candidate(
    candidate_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:write")),
) -> ApiResponse[dict[str, object]]:
    try:
        report = await _service(request).validate_candidate(
            tenant.tenant_id, candidate_id
        )
    except SkillCandidateError as exc:
        raise _candidate_error(exc) from exc
    if report is None:
        raise ApiServiceError(
            "SKILL_CANDIDATE_NOT_FOUND", "Skill Candidate not found", 404
        )
    return ApiResponse.ok(_validation_view(report))


@router.get("/validations/{report_id}")
async def get_candidate_validation(
    report_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:read")),
) -> ApiResponse[dict[str, object]]:
    report = await _service(request).get_validation(tenant.tenant_id, report_id)
    if report is None:
        raise ApiServiceError(
            "SKILL_CANDIDATE_VALIDATION_NOT_FOUND",
            "Candidate validation report not found",
            404,
        )
    return ApiResponse.ok(_validation_view(report))


@router.post("/{candidate_id}/replay")
async def record_replay(
    candidate_id: str,
    payload: ReplayRequest,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:write")),
) -> ApiResponse[dict[str, object]]:
    try:
        candidate = await _service(request).record_replay(
            tenant.tenant_id,
            candidate_id,
            report_id=payload.report_id,
            passed=payload.passed,
        )
    except SkillCandidateError as exc:
        raise _candidate_error(exc) from exc
    if candidate is None:
        raise ApiServiceError("SKILL_CANDIDATE_NOT_FOUND", "Skill Candidate not found", 404)
    return ApiResponse.ok(_candidate_view(candidate))


@router.post("/{candidate_id}/shadow")
async def record_shadow(
    candidate_id: str,
    payload: ReplayRequest,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:write")),
) -> ApiResponse[dict[str, object]]:
    try:
        candidate = await _service(request).record_shadow(
            tenant.tenant_id,
            candidate_id,
            report_id=payload.report_id,
            passed=payload.passed,
        )
    except SkillCandidateError as exc:
        raise _candidate_error(exc) from exc
    if candidate is None:
        raise ApiServiceError("SKILL_CANDIDATE_NOT_FOUND", "Skill Candidate not found", 404)
    return ApiResponse.ok(_candidate_view(candidate))


@router.post("/{candidate_id}/reject")
async def reject_candidate(
    candidate_id: str,
    payload: RejectRequest,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:write")),
) -> ApiResponse[dict[str, object]]:
    try:
        candidate = await _service(request).reject(
            tenant.tenant_id,
            candidate_id,
            reviewed_by=tenant.tenant_id,
            note=payload.note,
        )
    except SkillCandidateError as exc:
        raise _candidate_error(exc) from exc
    if candidate is None:
        raise ApiServiceError("SKILL_CANDIDATE_NOT_FOUND", "Skill Candidate not found", 404)
    return ApiResponse.ok(_candidate_view(candidate))


@router.get("/{candidate_id}/bridge")
async def candidate_bridge(
    candidate_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:read")),
) -> ApiResponse[dict[str, object]]:
    try:
        bridge = await _service(request).get_skill_repository_bridge(
            tenant.tenant_id, candidate_id
        )
    except SkillCandidateError as exc:
        raise _candidate_error(exc) from exc
    if bridge is None:
        raise ApiServiceError("SKILL_CANDIDATE_NOT_FOUND", "Skill Candidate not found", 404)
    return ApiResponse.ok(_bridge_view(bridge))


__all__ = ["router"]
