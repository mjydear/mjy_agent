"""Tenant-scoped CloudOps environment APIs."""

from typing import Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from athena.api.auth import TenantContext
from athena.api.rbac import require_scope
from athena.api.repositories import EnvironmentRepository, PersistedEnvironment
from athena.api.response import ApiResponse
from athena.api.services import ApiServiceError

router = APIRouter(prefix="/api/environments", tags=["environments"])
_CAPABILITIES = {
    "kubernetes": ("k8s.workload.read", "k8s.logs.read", "metrics.query"),
    "prometheus": ("metrics.query",),
}


class EnvironmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    environment_type: Literal["kubernetes", "prometheus"]
    provider: str = Field(min_length=1, max_length=80)
    mode: Literal["live", "replay", "mock"]
    scope: dict[str, object] = Field(default_factory=dict)
    credential_ref: str | None = Field(default=None, max_length=256)


class EnvironmentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    scope: dict[str, object] | None = None
    credential_ref: str | None = Field(default=None, max_length=256)


def _repo(request: Request) -> EnvironmentRepository:
    repo = getattr(request.app.state, "environment_repository", None)
    if repo is None:
        raise ApiServiceError(
            "ENVIRONMENT_STORE_UNAVAILABLE",
            "environment persistence is not configured",
            503,
        )
    return repo


def _view(item: PersistedEnvironment) -> dict[str, object]:
    return {
        "id": item.environment_id,
        "name": item.name,
        "type": item.environment_type,
        "provider": item.provider,
        "mode": item.mode,
        "scope": item.scope,
        "credential_ref": item.credential_ref,
        "capabilities": item.capabilities,
        "status": item.status,
        "last_checked_at": item.last_checked_at,
    }


@router.get("")
async def list_environments(
    request: Request, tenant: TenantContext = Depends(require_scope("ops:read"))
) -> ApiResponse[dict[str, object]]:
    return ApiResponse.ok(
        {"items": [_view(item) for item in await _repo(request).list(tenant.tenant_id)]}
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_environment(
    payload: EnvironmentCreate,
    request: Request,
    tenant: TenantContext = Depends(require_scope("ops:run")),
) -> ApiResponse[dict[str, object]]:
    item = await _repo(request).create(
        tenant.tenant_id,
        name=payload.name.strip(),
        environment_type=payload.environment_type,
        provider=payload.provider.strip(),
        mode=payload.mode,
        scope=payload.scope,
        credential_ref=payload.credential_ref,
        capabilities=_CAPABILITIES[payload.environment_type],
    )
    return ApiResponse.ok(_view(item))


@router.get("/{environment_id}")
async def get_environment(
    environment_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("ops:read")),
) -> ApiResponse[dict[str, object]]:
    item = await _repo(request).get(tenant.tenant_id, environment_id)
    if item is None:
        raise ApiServiceError("ENVIRONMENT_NOT_FOUND", "environment not found", 404)
    return ApiResponse.ok(_view(item))


@router.patch("/{environment_id}")
async def update_environment(
    environment_id: str,
    payload: EnvironmentUpdate,
    request: Request,
    tenant: TenantContext = Depends(require_scope("ops:run")),
) -> ApiResponse[dict[str, object]]:
    item = await _repo(request).update(
        tenant.tenant_id,
        environment_id,
        name=payload.name.strip() if payload.name else None,
        scope=payload.scope,
        credential_ref=payload.credential_ref,
    )
    if item is None:
        raise ApiServiceError("ENVIRONMENT_NOT_FOUND", "environment not found", 404)
    return ApiResponse.ok(_view(item))


@router.delete("/{environment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_environment(
    environment_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("ops:run")),
) -> Response:
    if not await _repo(request).delete(tenant.tenant_id, environment_id):
        raise ApiServiceError("ENVIRONMENT_NOT_FOUND", "environment not found", 404)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{environment_id}/test")
async def test_environment(
    environment_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("ops:read")),
) -> ApiResponse[dict[str, object]]:
    repo = _repo(request)
    item = await repo.get(tenant.tenant_id, environment_id)
    if item is None:
        raise ApiServiceError("ENVIRONMENT_NOT_FOUND", "environment not found", 404)
    origin, reason = "unavailable", "ENVIRONMENT_CLIENT_UNAVAILABLE"
    try:
        if item.environment_type == "kubernetes":
            client = request.app.state.ops_k8s_client
            namespaces = item.scope.get("namespaces", ["default"])
            namespace = (
                namespaces[0]
                if isinstance(namespaces, list) and namespaces
                else "default"
            )
            client.list_pods(str(namespace))
            origin, reason = client.last_data_origin, client.last_error_code
        else:
            client = request.app.state.ops_prometheus_client
            client.query("environment-test", "up")
            origin, reason = client.last_data_origin, client.last_error_code
    except Exception:  # Provider details must not cross the API boundary.
        reason = reason or "ENVIRONMENT_CONNECTION_FAILED"
    expected = "live" if item.mode == "live" else item.mode
    status_value = "available" if origin == expected else "unavailable"
    item = await repo.set_status(tenant.tenant_id, environment_id, status_value)
    assert item is not None
    result = _view(item)
    result.update({"active_backend": origin, "reason_code": reason})
    return ApiResponse.ok(result)


@router.post("/{environment_id}/sync")
async def sync_environment(
    environment_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("ops:read")),
) -> ApiResponse[dict[str, object]]:
    """Refresh the provider connection state without accepting client capabilities."""
    return await test_environment(environment_id, request, tenant)
