"""Phase 1 cache adapters for task facts, evidence, and task events."""

from __future__ import annotations

import pytest

from athena.agent.policy.contracts import (
    ActionDecision,
    DataOrigin,
    EnvironmentMode,
    ExecutionProfile,
    ToolCallV2,
)
from athena.agent.workflow.state import (
    OpsTaskPhase,
    OpsTaskState,
    OpsTaskStatus,
    TaskBudget,
)
from athena.api.auth import TenantContext
from athena.api.task_store import (
    CacheEvidenceSink,
    EvidenceStore,
    TaskEventRepository,
    TaskStateConflictError,
    TaskStateRepository,
)
from athena.infra.cache import InMemoryCache
from athena.tools import ToolRegistry, ToolRuntime, ToolRuntimeContext
from athena.tools.cloud.k8s.tools import (
    K8S_READONLY_LEGACY_TOOL_NAMES,
    K8S_READONLY_TOOL_SPECS,
)


def _tenant(tenant_id: str) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, api_key=None, roles=("*",))


def _state() -> OpsTaskState:
    return OpsTaskState(
        task_id="ops-task-1",
        tenant_id="tenant-a",
        objective="diagnose payment CrashLoopBackOff",
        environment_id="env-prod",
        environment_mode=EnvironmentMode.LIVE,
        scope={"namespace": "payment"},
        tenant_policy_snapshot={"readonly": True},
        budget=TaskBudget(
            remaining_steps=5, remaining_tokens=6000, remaining_time_ms=30000
        ),
        execution_profile=ExecutionProfile.BOUNDED_POLICY_LOOP,
        completed_actions=(
            ActionDecision(
                action="k8s.pod.list",
                arguments={"namespace": "payment"},
                reason_code="INITIAL_COLLECTION",
            ),
        ),
    )


def test_task_state_repository_is_tenant_scoped_and_versioned() -> None:
    repository = TaskStateRepository(InMemoryCache(namespace="ops-state"))
    tenant_a = _tenant("tenant-a")
    tenant_b = _tenant("tenant-b")
    initial = _state()

    repository.save(tenant_a, initial)
    assert repository.load(tenant_b, initial.task_id) is None

    running = initial.transition_to(OpsTaskStatus.RUNNING, OpsTaskPhase.COLLECT)
    repository.save(tenant_a, running, expected_state_version=initial.state_version)
    loaded = repository.load(tenant_a, initial.task_id)

    assert loaded == running
    assert loaded is not None
    assert loaded.completed_actions[0].reason_code == "INITIAL_COLLECTION"
    with pytest.raises(TaskStateConflictError):
        repository.save(tenant_a, running, expected_state_version=initial.state_version)


def test_task_state_repository_cancel_is_state_machine_controlled() -> None:
    repository = TaskStateRepository(InMemoryCache(namespace="ops-cancel"))
    tenant = _tenant("tenant-a")
    state = _state().transition_to(OpsTaskStatus.RUNNING)
    repository.save(tenant, state)

    cancelled = repository.request_cancel(tenant, state.task_id)

    assert cancelled.status is OpsTaskStatus.CANCELLED
    assert cancelled.state_version == state.state_version + 1
    assert repository.request_cancel(tenant, state.task_id) == cancelled


def test_task_event_repository_returns_ordered_incremental_events() -> None:
    repository = TaskEventRepository(InMemoryCache(namespace="ops-events"))
    tenant = _tenant("tenant-a")

    first = repository.append(
        tenant, "ops-task-1", "task.started", {"phase": "collect"}
    )
    second = repository.append(
        tenant, "ops-task-1", "tool.finished", {"tool": "k8s.pod.list"}
    )

    assert first.sequence == 1
    assert second.sequence == 2
    assert repository.list_after(tenant, "ops-task-1", after_sequence=1) == (second,)
    assert repository.list_after(_tenant("tenant-b"), "ops-task-1") == ()


def test_evidence_store_keeps_metadata_content_and_tenant_boundaries() -> None:
    store = EvidenceStore(InMemoryCache(namespace="ops-evidence"))
    tenant_a = _tenant("tenant-a")
    evidence = store.create(
        tenant_a,
        task_id="ops-task-1",
        evidence_type="event",
        source="k8s.events.list",
        data_origin=DataOrigin.LIVE,
        summary="BackOff event observed",
        content={"reason": "BackOff", "data_origin": "live"},
    )

    assert evidence.content_ref == f"cache-evidence://{evidence.id}"
    assert store.get(tenant_a, evidence.id) == evidence
    assert store.get_content(tenant_a, evidence.id) == {
        "reason": "BackOff",
        "data_origin": "live",
    }
    assert store.list_for_task(tenant_a, "ops-task-1") == (evidence,)
    assert store.get(_tenant("tenant-b"), evidence.id) is None
    assert store.get_content(_tenant("tenant-b"), evidence.id) is None


@pytest.mark.asyncio
async def test_cache_evidence_sink_persists_runtime_results_with_explicit_origin() -> (
    None
):
    cache = InMemoryCache(namespace="ops-runtime-evidence")
    tenant = _tenant("tenant-a")
    store = EvidenceStore(cache)
    registry = ToolRegistry()

    @registry.register
    def k8s_list_events(namespace: str = "default", pod_name: str = "") -> str:
        """Legacy readonly events adapter."""
        return '[{"reason": "BackOff", "data_origin": "live"}]'

    spec = next(
        item for item in K8S_READONLY_TOOL_SPECS if item.name == "k8s.events.list"
    )
    runtime = ToolRuntime(
        registry,
        {spec.name: spec},
        {spec.name: K8S_READONLY_LEGACY_TOOL_NAMES[spec.name]},
        evidence_sink=CacheEvidenceSink(store, tenant, DataOrigin.LIVE),
    )

    result = await runtime.invoke(
        ToolCallV2(
            call_id="call-events",
            task_id="ops-task-1",
            tenant_id=tenant.tenant_id,
            tool_name=spec.name,
            arguments={"namespace": "payment"},
        ),
        ToolRuntimeContext(
            tenant_id=tenant.tenant_id,
            environment_id="env-prod",
            allowed_capabilities=frozenset({"k8s.events.read"}),
            allowed_namespaces=frozenset({"payment"}),
        ),
    )

    assert result.evidence_refs
    stored = store.get(tenant, result.evidence_refs[0])
    assert stored is not None
    assert stored.data_origin is DataOrigin.LIVE
    assert stored.source == "k8s.events.list"
