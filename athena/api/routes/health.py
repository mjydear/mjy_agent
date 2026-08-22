"""Liveness and readiness probes for the Runtime service."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from athena.config import production_readiness_issues

router = APIRouter(tags=["health"])


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    state = request.app.state
    if getattr(state, "draining", False):
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "ready": False, "reason_code": "DRAINING"},
        )
    settings = state.settings
    issues = production_readiness_issues(settings)
    components = [
        {
            "component": "configuration",
            "status": "healthy" if not issues else "unavailable",
            "reason_code": issues[0] if issues else None,
        },
        dict(getattr(state, "cache_component", {"component": "cache", "status": "unknown"})),
        {
            "component": "runtime",
            "status": "healthy" if getattr(state, "agent_runtime", None) else "unavailable",
            "reason_code": None,
        },
    ]
    cache = getattr(state, "cache", None)
    if cache is not None:
        try:
            cache.get("__readyz__")
        except Exception:  # noqa: BLE001
            components[1].update(status="unavailable", reason_code="CACHE_CONNECTION_FAILED")
    database = getattr(state, "database", None)
    if database is not None:
        components.append({"component": "database", "status": "configured", "reason_code": None})
    ready = not issues and all(item.get("status") not in {"unavailable"} for item in components)
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "ready": ready,
            "profile": settings.runtime.profile,
            "reason_code": issues[0] if issues else None,
            "components": components,
        },
    )
