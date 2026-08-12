"""Phase 1 OpsTask fact APIs and persisted-event SSE replay."""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from athena.api.auth import TenantContext
from athena.api.idempotency import (
    IdempotencyConflictError,
    IdempotencyManager,
    get_idempotency_key,
)
from athena.api.rbac import require_scope
from athena.api.response import ApiResponse
from athena.api.routes._deps import get_idempotency, get_ops_task_service
from athena.api.services import ApiServiceError
from athena.api.task_store import EventCursorExpiredError
from athena.agent.workflow.state import OpsTaskPhase, OpsTaskStatus
from athena.application.ops_task_service import OpsTaskService
from athena.application.durable_ops_task_service import DurableOpsTaskService
from athena.api.repositories import DurableIdempotencyConflictError
from athena.observability.trace_context import get_traceparent

router = APIRouter(prefix="/api/ops/tasks", tags=["ops-tasks"])


class OpsTaskCreateRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=1000)
    environment_id: str = Field(min_length=1, max_length=120)
    namespace: str = Field(min_length=1, max_length=120)


class OpsTaskInputRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


def _not_found() -> ApiServiceError:
    return ApiServiceError("OPS_TASK_NOT_FOUND", "ops task not found", status_code=404)


def _required_idempotency_key(request: Request) -> str:
    try:
        key = get_idempotency_key(request, required=True)
    except ValueError as exc:
        raise ApiServiceError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotency-Key header is required",
            status_code=400,
        ) from exc
    assert key is not None
    return key


def _idempotency_conflict(exc: IdempotencyConflictError) -> ApiServiceError:
    return ApiServiceError(
        "IDEMPOTENCY_KEY_REUSED",
        "Idempotency-Key was already used for a different request",
        status_code=409,
    )


@router.get("")
async def list_tasks(
    request: Request,
    status: OpsTaskStatus | None = Query(default=None),
    phase: OpsTaskPhase | None = Query(default=None),
    environment_id: str | None = Query(default=None, min_length=1),
    cursor: str | None = Query(default=None, min_length=1),
    limit: int = Query(default=50, ge=1, le=100),
    service: OpsTaskService = Depends(get_ops_task_service),
    tenant: TenantContext = Depends(require_scope("ops:read")),
) -> ApiResponse[dict[str, object]]:
    durable: DurableOpsTaskService | None = getattr(
        request.app.state, "durable_ops_task_service", None
    )
    if durable is not None:
        durable_tasks = await durable.list(
            tenant,
            status=status.value if status is not None else None,
            environment_id=environment_id,
            limit=100,
        )
        durable_tasks = [
            item
            for item in durable_tasks
            if (phase is None or item.phase == phase.value)
            and (cursor is None or item.task_id < cursor)
        ]
        page = durable_tasks[:limit]
        return ApiResponse.ok(
            {
                "items": [durable.state_view(item) for item in page],
                "next_cursor": page[-1].task_id if len(durable_tasks) > limit else None,
            }
        )
    tasks = sorted(service.list(tenant), key=lambda item: item.task_id, reverse=True)
    tasks = [
        item
        for item in tasks
        if (status is None or item.status is status)
        and (phase is None or item.phase is phase)
        and (environment_id is None or item.environment_id == environment_id)
        and (cursor is None or item.task_id < cursor)
    ]
    page = tasks[:limit]
    return ApiResponse.ok(
        {
            "items": [service.state_view(item) for item in page],
            "next_cursor": page[-1].task_id if len(tasks) > limit else None,
        }
    )


@router.post("")
async def create_task(
    payload: OpsTaskCreateRequest,
    request: Request,
    service: OpsTaskService = Depends(get_ops_task_service),
    idempotency: IdempotencyManager = Depends(get_idempotency),
    tenant: TenantContext = Depends(require_scope("ops:run")),
) -> ApiResponse[dict[str, object]]:
    key = _required_idempotency_key(request)
    request_hash = idempotency.request_hash(payload.model_dump(mode="json"))
    durable: DurableOpsTaskService | None = getattr(
        request.app.state, "durable_ops_task_service", None
    )
    if durable is not None:
        try:
            state, _ = await durable.create(
                tenant,
                payload.objective,
                payload.environment_id,
                payload.namespace,
                idempotency_key=key,
                request_hash=request_hash,
                traceparent=get_traceparent(),
            )
        except PermissionError as exc:
            raise ApiServiceError(
                "ENV_SCOPE_DENIED", "namespace is not authorized", status_code=403
            ) from exc
        except DurableIdempotencyConflictError as exc:
            raise ApiServiceError(
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used for a different request",
                status_code=409,
            ) from exc
        return ApiResponse.ok(durable.state_view(state))

    def create_and_schedule() -> dict[str, object]:
        state = service.create(
            tenant, payload.objective, payload.environment_id, payload.namespace
        )
        result = service.state_view(state)
        service.schedule(request.app.state.task_manager, tenant, state.task_id)
        return result

    try:
        result, _ = idempotency.execute_once(
            tenant.tenant_id,
            key,
            operation="ops-task:create",
            request_hash=request_hash,
            factory=create_and_schedule,
        )
    except PermissionError as exc:
        raise ApiServiceError(
            "ENV_SCOPE_DENIED", "namespace is not authorized", status_code=403
        ) from exc
    except IdempotencyConflictError as exc:
        raise _idempotency_conflict(exc) from exc
    return ApiResponse.ok(result)


@router.get("/{task_id}")
async def get_task(
    task_id: str,
    request: Request,
    service: OpsTaskService = Depends(get_ops_task_service),
    tenant: TenantContext = Depends(require_scope("ops:read")),
) -> ApiResponse[dict[str, object]]:
    durable: DurableOpsTaskService | None = getattr(
        request.app.state, "durable_ops_task_service", None
    )
    if durable is not None:
        state = await durable.get(tenant, task_id)
        if state is None:
            raise _not_found()
        return ApiResponse.ok(await durable.detail_view(tenant, state))
    state = service.get(tenant, task_id)
    if state is None:
        raise _not_found()
    return ApiResponse.ok(service.detail_view(tenant, state))


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    request: Request,
    service: OpsTaskService = Depends(get_ops_task_service),
    idempotency: IdempotencyManager = Depends(get_idempotency),
    tenant: TenantContext = Depends(require_scope("ops:cancel")),
) -> ApiResponse[dict[str, object]]:
    key = _required_idempotency_key(request)
    request_hash = idempotency.request_hash({"task_id": task_id, "command": "cancel"})
    durable: DurableOpsTaskService | None = getattr(
        request.app.state, "durable_ops_task_service", None
    )
    if durable is not None:
        try:
            state, _ = await durable.cancel(
                tenant,
                task_id,
                idempotency_key=key,
                request_hash=request_hash,
            )
        except DurableIdempotencyConflictError as exc:
            raise ApiServiceError(
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used for a different request",
                status_code=409,
            ) from exc
        if state is None:
            raise _not_found()
        return ApiResponse.ok(durable.state_view(state))
    try:
        result, _ = idempotency.execute_once(
            tenant.tenant_id,
            key,
            operation=f"ops-task:{task_id}:cancel",
            request_hash=request_hash,
            factory=lambda: service.state_view(service.cancel(tenant, task_id)),
        )
    except KeyError as exc:
        raise _not_found() from exc
    except IdempotencyConflictError as exc:
        raise _idempotency_conflict(exc) from exc
    return ApiResponse.ok(result)


@router.post("/{task_id}/input")
async def add_input(
    task_id: str,
    payload: OpsTaskInputRequest,
    request: Request,
    service: OpsTaskService = Depends(get_ops_task_service),
    idempotency: IdempotencyManager = Depends(get_idempotency),
    tenant: TenantContext = Depends(require_scope("ops:run")),
) -> ApiResponse[dict[str, str]]:
    key = _required_idempotency_key(request)
    request_hash = idempotency.request_hash(
        {"task_id": task_id, "content": payload.content}
    )
    durable: DurableOpsTaskService | None = getattr(
        request.app.state, "durable_ops_task_service", None
    )
    if durable is not None:
        try:
            state, _ = await durable.add_input(
                tenant,
                task_id,
                payload.content,
                idempotency_key=key,
                request_hash=request_hash,
            )
        except ValueError as exc:
            raise ApiServiceError(
                str(exc), "task is not waiting for input", 409
            ) from exc
        except DurableIdempotencyConflictError as exc:
            raise ApiServiceError(
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used for a different request",
                status_code=409,
            ) from exc
        if state is None:
            raise _not_found()
        return ApiResponse.ok({"status": "accepted"})

    def accept_input() -> dict[str, str]:
        service.add_input(tenant, task_id, payload.content)
        service.schedule(request.app.state.task_manager, tenant, task_id)
        return {"status": "accepted"}

    try:
        result, _ = idempotency.execute_once(
            tenant.tenant_id,
            key,
            operation=f"ops-task:{task_id}:input",
            request_hash=request_hash,
            factory=accept_input,
        )
    except KeyError as exc:
        raise _not_found() from exc
    except ValueError as exc:
        raise ApiServiceError(str(exc), "task is not waiting for input", 409) from exc
    except IdempotencyConflictError as exc:
        raise _idempotency_conflict(exc) from exc
    return ApiResponse.ok(result)


@router.get("/{task_id}/events")
async def task_events(
    task_id: str,
    request: Request,
    after_seq: int = Query(default=0, ge=0),
    follow: bool = Query(default=True),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    service: OpsTaskService = Depends(get_ops_task_service),
    tenant: TenantContext = Depends(require_scope("ops:read")),
) -> StreamingResponse:
    if last_event_id and last_event_id.isdigit():
        after_seq = max(after_seq, int(last_event_id))
    durable: DurableOpsTaskService | None = getattr(
        request.app.state, "durable_ops_task_service", None
    )
    if durable is not None:
        if await durable.get(tenant, task_id) is None:
            raise _not_found()

        async def durable_stream():
            cursor = after_seq
            last_output = time.monotonic()
            terminal_seen_at: float | None = None
            while True:
                if await request.is_disconnected():
                    return
                events = await durable.events_after(tenant, task_id, cursor)
                for event in events:
                    payload = json.dumps(
                        {
                            "sequence": event["sequence"],
                            "data": event["data"],
                            "created_at": event["created_at"].isoformat(),
                        },
                        ensure_ascii=False,
                    )
                    cursor = int(event["sequence"])
                    last_output = time.monotonic()
                    yield (
                        f"id: {cursor}\n"
                        f"event: {event['event_type']}\n"
                        f"data: {payload}\n\n"
                    )
                state = await durable.get(tenant, task_id)
                terminal = state is None or state.status in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }
                if terminal:
                    now = time.monotonic()
                    if events or terminal_seen_at is None:
                        terminal_seen_at = now
                    elif now - terminal_seen_at >= 0.2:
                        return
                else:
                    terminal_seen_at = None
                if not follow:
                    return
                if time.monotonic() - last_output >= 15:
                    last_output = time.monotonic()
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.1)

        return StreamingResponse(
            durable_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )
    if service.get(tenant, task_id) is None:
        raise _not_found()
    try:
        service.events_after(tenant, task_id, after_seq)
    except EventCursorExpiredError as exc:
        raise ApiServiceError(
            "EVENT_CURSOR_EXPIRED",
            "event cursor is no longer retained; reload task detail",
            status_code=409,
        ) from exc

    async def stream():
        cursor = after_seq
        last_output = time.monotonic()
        terminal_seen_at: float | None = None
        while True:
            if await request.is_disconnected():
                return
            try:
                events = service.events_after(tenant, task_id, cursor)
            except EventCursorExpiredError:
                yield (
                    "event: error\n" 'data: {"error_code":"EVENT_CURSOR_EXPIRED"}\n\n'
                )
                return
            for event in events:
                payload = json.dumps(
                    {
                        "sequence": event.sequence,
                        "data": event.data,
                        "created_at": event.created_at.isoformat(),
                    },
                    ensure_ascii=False,
                )
                cursor = event.sequence
                last_output = time.monotonic()
                yield (
                    f"id: {event.sequence}\n"
                    f"event: {event.event_type}\n"
                    f"data: {payload}\n\n"
                )

            state = service.get(tenant, task_id)
            terminal = state is None or state.status.value in {
                "succeeded",
                "failed",
                "cancelled",
            }
            if terminal:
                now = time.monotonic()
                if events or terminal_seen_at is None:
                    terminal_seen_at = now
                elif now - terminal_seen_at >= 0.2:
                    return
            else:
                terminal_seen_at = None
            if not follow:
                return
            if time.monotonic() - last_output >= 15:
                last_output = time.monotonic()
                yield ": keepalive\n\n"
            await asyncio.sleep(0.1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{task_id}/evidence")
async def task_evidence(
    task_id: str,
    request: Request,
    service: OpsTaskService = Depends(get_ops_task_service),
    tenant: TenantContext = Depends(require_scope("ops:read")),
) -> ApiResponse[dict[str, object]]:
    durable: DurableOpsTaskService | None = getattr(
        request.app.state, "durable_ops_task_service", None
    )
    evidence_repository = getattr(
        request.app.state, "durable_evidence_repository", None
    )
    if durable is not None and evidence_repository is not None:
        if await durable.get(tenant, task_id) is None:
            raise _not_found()
        evidence = await evidence_repository.list_for_task(tenant.tenant_id, task_id)
        return ApiResponse.ok(
            {
                "items": [
                    {
                        "id": item.evidence_id,
                        "type": item.evidence_type,
                        "source": item.source,
                        "data_origin": item.data_origin,
                        "summary": item.summary,
                        "content_ref": item.content_ref,
                        "content_hash": item.content_hash,
                    }
                    for item in evidence
                ]
            }
        )
    try:
        evidence = service.evidence_for_task(tenant, task_id)
    except KeyError as exc:
        raise _not_found() from exc
    return ApiResponse.ok(
        {
            "items": [
                {
                    "id": item.id,
                    "type": item.type,
                    "source": item.source,
                    "data_origin": item.data_origin.value,
                    "summary": item.summary,
                    "content_ref": item.content_ref,
                }
                for item in evidence
            ]
        }
    )


@router.get("/{task_id}/report")
async def task_report(
    task_id: str,
    request: Request,
    service: OpsTaskService = Depends(get_ops_task_service),
    tenant: TenantContext = Depends(require_scope("ops:read")),
) -> ApiResponse[dict[str, object]]:
    durable: DurableOpsTaskService | None = getattr(
        request.app.state, "durable_ops_task_service", None
    )
    evidence_repository = getattr(
        request.app.state, "durable_evidence_repository", None
    )
    if durable is not None:
        state = await durable.get(tenant, task_id)
        if state is None:
            raise _not_found()
        evidence_count = 0
        if evidence_repository is not None:
            evidence_count = len(
                await evidence_repository.list_for_task(tenant.tenant_id, task_id)
            )
        return ApiResponse.ok(
            {
                "task": await durable.detail_view(tenant, state),
                "evidence_count": evidence_count,
                "root_causes": state.state.get("root_causes", []),
            }
        )
    try:
        return ApiResponse.ok(service.report(tenant, task_id))
    except KeyError as exc:
        raise _not_found() from exc
