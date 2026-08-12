"""Tenant-scoped Web Console LLM configuration endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Response, status

from athena.api.auth import TenantContext, require_tenant
from athena.api.llm_config_store import LLMConfigStateError, StoredLLMConfig
from athena.api.routes._deps import get_service
from athena.api.schemas import (
    LLMConfigCreate,
    LLMConfigPublic,
    LLMConfigRotate,
    LLMConfigTestResult,
)
from athena.api.services import ApiServiceError, AthenaWebService

router = APIRouter(prefix="/api/llm/configs", tags=["llm-configs"])


def _public(config: StoredLLMConfig) -> LLMConfigPublic:
    suffix = getattr(config, "credential_suffix", None)
    return LLMConfigPublic(
        config_id=config.config_id,
        provider=config.provider,
        display_name=config.display_name,
        model=config.model,
        base_url=config.base_url,
        has_api_key=bool(config.credential_ref),
        masked_api_key=f"****{suffix}" if suffix else None,
        enabled=config.enabled,
        is_default=config.is_default,
        status=config.status,
    )


def _not_found() -> ApiServiceError:
    return ApiServiceError(
        "LLM_CONFIG_NOT_FOUND", "LLM configuration was not found", status_code=404
    )


def _test_result(
    config: StoredLLMConfig, *, success: bool, reason_code: str
) -> LLMConfigTestResult:
    return LLMConfigTestResult(
        **_public(config).model_dump(), success=success, reason_code=reason_code
    )


async def _probe_connection(config: StoredLLMConfig, api_key: str | None) -> None:
    """Perform one bounded provider request without persisting its prompt or result."""
    from litellm import completion

    request: dict[str, object] = {
        "model": config.model,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "api_key": api_key,
        "temperature": 0,
        "max_tokens": 1,
        "timeout": 10,
    }
    if config.base_url:
        request["api_base"] = config.base_url
    await asyncio.to_thread(completion, **request)


@router.get("", response_model=list[LLMConfigPublic])
def list_configs(
    service: AthenaWebService = Depends(get_service),
    tenant: TenantContext = Depends(require_tenant),
) -> list[LLMConfigPublic]:
    return [_public(config) for config in service.llm_config_store.list(tenant)]


@router.post("", response_model=LLMConfigPublic, status_code=status.HTTP_201_CREATED)
def create_config(
    payload: LLMConfigCreate,
    service: AthenaWebService = Depends(get_service),
    tenant: TenantContext = Depends(require_tenant),
) -> LLMConfigPublic:
    provider = payload.provider.strip().lower()
    api_key = (
        payload.api_key.get_secret_value().strip()
        if payload.api_key is not None
        else ""
    ) or None
    base_url = (payload.base_url or "").strip() or None
    if api_key is not None and len(api_key) > 300:
        raise ApiServiceError(
            "LLM_CREDENTIAL_INVALID", "API Key 超过允许长度", status_code=400
        )
    if provider != "ollama" and not api_key:
        raise ApiServiceError("LLM_CREDENTIAL_REQUIRED", "该提供商需要 API Key")
    if provider in {"ollama", "openai_compatible"} and not base_url:
        raise ApiServiceError("LLM_BASE_URL_REQUIRED", "该提供商需要 Base URL")
    return _public(
        service.llm_config_store.create(
            tenant,
            provider=provider,
            display_name=payload.display_name.strip(),
            model=payload.model.strip(),
            api_key=api_key,
            base_url=base_url,
            enabled=payload.enabled,
            is_default=payload.is_default,
            status="available",
        )
    )


@router.post("/{config_id}/test", response_model=LLMConfigTestResult)
async def test_config(
    config_id: str,
    service: AthenaWebService = Depends(get_service),
    tenant: TenantContext = Depends(require_tenant),
) -> LLMConfigTestResult:
    store = service.llm_config_store
    config = store.get(tenant, config_id)
    if config is None:
        raise _not_found()

    try:
        api_key = store.resolve_api_key(tenant, config)
        if config.provider != "ollama" and not api_key:
            raise ValueError("LLM credential is unavailable")
        await _probe_connection(config, api_key)
    except Exception:  # Provider details may contain credentials; never return them.
        checked = store.record_connection_result(tenant, config_id, success=False)
        if checked is None:
            raise _not_found()
        return _test_result(checked, success=False, reason_code="LLM_CONNECTION_FAILED")

    checked = store.record_connection_result(tenant, config_id, success=True)
    if checked is None:
        raise _not_found()
    return _test_result(checked, success=True, reason_code="LLM_CONNECTION_AVAILABLE")


@router.post("/{config_id}/default", response_model=LLMConfigPublic)
def set_default_config(
    config_id: str,
    service: AthenaWebService = Depends(get_service),
    tenant: TenantContext = Depends(require_tenant),
) -> LLMConfigPublic:
    try:
        config = service.llm_config_store.set_default(tenant, config_id)
    except LLMConfigStateError as exc:
        raise ApiServiceError(
            exc.error_code,
            "LLM configuration cannot be selected as default",
            status_code=409,
        ) from exc
    if config is None:
        raise _not_found()
    return _public(config)


@router.post("/{config_id}/disable", response_model=LLMConfigPublic)
def disable_config(
    config_id: str,
    service: AthenaWebService = Depends(get_service),
    tenant: TenantContext = Depends(require_tenant),
) -> LLMConfigPublic:
    config = service.llm_config_store.disable(tenant, config_id)
    if config is None:
        raise _not_found()
    return _public(config)


@router.post("/{config_id}/rotate", response_model=LLMConfigPublic)
def rotate_config_credential(
    config_id: str,
    payload: LLMConfigRotate,
    service: AthenaWebService = Depends(get_service),
    tenant: TenantContext = Depends(require_tenant),
) -> LLMConfigPublic:
    api_key = payload.api_key.get_secret_value().strip()
    if not api_key or len(api_key) > 300:
        raise ApiServiceError(
            "LLM_CREDENTIAL_INVALID", "LLM credential is invalid", status_code=400
        )
    try:
        config = service.llm_config_store.rotate_credential(tenant, config_id, api_key)
    except Exception as exc:  # Do not expose a secret-store implementation error.
        raise ApiServiceError(
            "LLM_CREDENTIAL_ROTATION_FAILED",
            "LLM credential rotation failed",
            status_code=500,
        ) from exc
    if config is None:
        raise _not_found()
    return _public(config)


@router.delete("/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_config(
    config_id: str,
    service: AthenaWebService = Depends(get_service),
    tenant: TenantContext = Depends(require_tenant),
) -> Response:
    if not service.llm_config_store.delete(tenant, config_id):
        raise ApiServiceError("LLM_CONFIG_NOT_FOUND", "模型配置不存在", status_code=404)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
