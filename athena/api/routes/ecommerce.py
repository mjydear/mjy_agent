"""HTTP entry points for the read-only ecommerce diagnosis slice."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from athena.api.auth import TenantContext
from athena.api.rbac import require_scope
from athena.api.response import ApiResponse
from athena.api.errors import ApiServiceError
from athena.application.ecommerce_skill_trace import EcommerceSkillTraceService
from athena.evaluation.backend_replay import (
    EcommerceDiagnosisCase,
    EcommerceDiagnosisEvaluation,
    case_definition_digest,
    fixed_ecommerce_diagnosis_cases,
    run_ecommerce_replay,
)

router = APIRouter(prefix="/api/ecommerce/diagnosis", tags=["ecommerce-diagnosis"])


class EcommerceReplayRequest(BaseModel):
    case_id: str | None = Field(default=None, min_length=1, max_length=128)


def _case_view(case: EcommerceDiagnosisCase) -> dict[str, object]:
    return {
        "case_id": case.case_id,
        "category": case.category.value,
        "goal": case.task_goal,
        "fixture_id": case.fixture_id,
        "allowed_tools": list(case.safety_oracle.allowed_readonly_tool_names),
        "required_evidence": list(case.required_evidence),
        "max_ticks": case.max_ticks,
        "max_tool_calls": case.max_tool_calls,
        "schema_version": case.schema_version,
    }


def _evaluation_view(item: EcommerceDiagnosisEvaluation) -> dict[str, object]:
    return item.to_dict()


@router.get("/cases")
async def list_cases(
    _: TenantContext = Depends(require_scope("runtime:read")),
) -> ApiResponse[dict[str, object]]:
    cases = fixed_ecommerce_diagnosis_cases()
    return ApiResponse.ok(
        {
            "schema_version": cases[0].schema_version,
            "definition_digest": case_definition_digest(cases),
            "items": [_case_view(case) for case in cases],
        }
    )


@router.post("/replay")
async def replay(
    payload: EcommerceReplayRequest,
    _: TenantContext = Depends(require_scope("runtime:run")),
) -> ApiResponse[dict[str, object]]:
    cases = fixed_ecommerce_diagnosis_cases()
    if payload.case_id is not None:
        cases = tuple(case for case in cases if case.case_id == payload.case_id)
        if not cases:
            raise ApiServiceError(
                "ECOMMERCE_CASE_NOT_FOUND",
                "ecommerce diagnosis case was not found",
                404,
            )
    report = run_ecommerce_replay(cases)
    passed = sum(item.oracle_passed for item in report.evaluations)
    return ApiResponse.ok(
        {
            "definition_digest": report.definition_digest,
            "case_count": report.aggregate.case_count,
            "passed_count": passed,
            "success_rate": report.aggregate.oracle_pass_rate,
            "aggregate": report.aggregate.to_dict(),
            "items": [_evaluation_view(item) for item in report.evaluations],
        }
    )


@router.post("/cases/{case_id}/trajectory")
async def capture_trajectory(
    case_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("runtime:learn")),
) -> ApiResponse[dict[str, object]]:
    service = getattr(request.app.state, "ecommerce_skill_trace_service", None)
    if not isinstance(service, EcommerceSkillTraceService):
        raise ApiServiceError(
            "ECOMMERCE_SKILL_TRACE_UNAVAILABLE",
            "ecommerce Skill trace persistence is unavailable",
            503,
        )
    try:
        result = await service.capture(tenant_id=tenant.tenant_id, case_id=case_id)
    except ValueError as exc:
        raise ApiServiceError(
            "ECOMMERCE_CASE_NOT_FOUND",
            "ecommerce diagnosis case was not found",
            404,
        ) from exc
    return ApiResponse.ok(result)


__all__ = ["router"]
