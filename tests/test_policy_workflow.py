"""Bounded structured decisions and one-action policy workflow ticks."""

from __future__ import annotations

from dataclasses import replace

import pytest

from athena.agent.context import ContextManager
from athena.agent.policy.agent import PolicyAgent, PolicyDecisionError
from athena.agent.policy.contracts import (
    ActionDecision,
    EnvironmentMode,
    ExecutionProfile,
)
from athena.agent.workflow.crashloop import CrashLoopDiagnosisWorkflow
from athena.agent.workflow.runner import WorkflowRunner
from athena.agent.workflow.state import (
    OpsTaskPhase,
    OpsTaskState,
    OpsTaskStatus,
    TaskBudget,
)
from athena.api.auth import TenantContext
from athena.api.task_store import (
    EvidenceStore,
    TaskEventRepository,
    TaskStateRepository,
)
from athena.infra.cache import InMemoryCache
from athena.infra.llm import LLMResponse
from athena.tools import ToolRegistry, ToolRuntime, ToolRuntimeContext
from athena.tools.cloud.k8s.tools import (
    K8S_READONLY_LEGACY_TOOL_NAMES,
    K8S_READONLY_TOOL_SPECS,
)


def _tenant() -> TenantContext:
    return TenantContext(tenant_id="tenant-a", api_key=None, roles=("*",))


def _state() -> OpsTaskState:
    return OpsTaskState(
        task_id="task-1",
        tenant_id="tenant-a",
        objective="diagnose CrashLoopBackOff",
        environment_id="env-prod",
        environment_mode=EnvironmentMode.LIVE,
        scope={"namespace": "payment"},
        tenant_policy_snapshot={"readonly": True},
        budget=TaskBudget(
            remaining_steps=2, remaining_tokens=6000, remaining_time_ms=30000
        ),
        execution_profile=ExecutionProfile.BOUNDED_POLICY_LOOP,
    )


@pytest.mark.asyncio
async def test_policy_agent_rejects_llm_action_outside_context() -> None:
    class FakeLLM:
        async def complete(self, messages: object) -> LLMResponse:
            return LLMResponse(
                content='{"action":"k8s.rollout.restart","arguments":{},"reason_code":"BAD"}',
                model="fake",
            )

    context = ContextManager().build(_state(), (), ())
    agent = PolicyAgent(FakeLLM())

    with pytest.raises(PolicyDecisionError):
        await agent.decide(context)


@pytest.mark.asyncio
async def test_policy_agent_uses_rule_fallback_when_llm_is_unavailable() -> None:
    class FailingLLM:
        async def complete(self, messages: object) -> LLMResponse:
            raise RuntimeError("provider unavailable")

    spec = next(item for item in K8S_READONLY_TOOL_SPECS if item.name == "k8s.pod.list")
    context = ContextManager().build(_state(), (), (spec,))
    agent = PolicyAgent(
        FailingLLM(),
        fallback=lambda _: ActionDecision(
            action="k8s.pod.list", arguments={}, reason_code="RULES_ONLY"
        ),
    )

    decision = await agent.decide(context)

    assert decision.reason_code == "RULES_ONLY"


@pytest.mark.asyncio
async def test_runner_persists_one_readonly_action_per_tick() -> None:
    cache = InMemoryCache(namespace="policy-workflow")
    tenant = _tenant()
    task_repository = TaskStateRepository(cache)
    task_repository.save(tenant, _state())
    events = TaskEventRepository(cache)
    evidence = EvidenceStore(cache)
    registry = ToolRegistry()

    @registry.register
    def k8s_list_pods(namespace: str = "default") -> str:
        """Legacy readonly pod adapter."""
        return '[{"name":"payment-api", "phase":"Running"}]'

    spec = next(item for item in K8S_READONLY_TOOL_SPECS if item.name == "k8s.pod.list")
    runtime = ToolRuntime(
        registry,
        {spec.name: spec},
        {spec.name: K8S_READONLY_LEGACY_TOOL_NAMES[spec.name]},
    )
    policy = PolicyAgent(
        fallback=lambda context: ActionDecision(
            action="k8s.pod.list",
            arguments={"namespace": "payment"},
            reason_code="INITIAL_COLLECTION",
        )
    )
    runner = WorkflowRunner(
        task_repository,
        events,
        evidence,
        ContextManager(),
        policy,
        runtime,
    )

    result = await runner.tick(
        tenant,
        "task-1",
        ToolRuntimeContext(
            tenant_id="tenant-a",
            environment_id="env-prod",
            allowed_capabilities=frozenset({"k8s.workload.read"}),
            allowed_namespaces=frozenset({"payment"}),
        ),
        (spec,),
    )

    assert result.status is OpsTaskStatus.RUNNING
    assert result.budget.remaining_steps == 1
    assert result.completed_actions[0].action == "k8s.pod.list"
    assert [event.event_type for event in events.list_after(tenant, "task-1")] == [
        "task.started",
        "context.compressed",
        "decision.recorded",
        "tool.started",
        "tool.finished",
    ]


@pytest.mark.asyncio
async def test_runner_completes_after_last_successful_budgeted_action() -> None:
    state = _state()
    state = OpsTaskState(**{**state.__dict__, "budget": TaskBudget(1, 6000, 30000)})
    cache = InMemoryCache(namespace="policy-workflow-terminal")
    tenant = _tenant()
    tasks = TaskStateRepository(cache)
    tasks.save(tenant, state)
    events = TaskEventRepository(cache)
    registry = ToolRegistry()

    @registry.register
    def k8s_list_pods(namespace: str = "default") -> str:
        return "[]"

    spec = next(item for item in K8S_READONLY_TOOL_SPECS if item.name == "k8s.pod.list")
    runner = WorkflowRunner(
        tasks,
        events,
        EvidenceStore(cache),
        ContextManager(),
        PolicyAgent(
            fallback=lambda _: ActionDecision(
                action=spec.name,
                arguments={"namespace": "payment"},
                reason_code="INITIAL_COLLECTION",
            )
        ),
        ToolRuntime(
            registry,
            {spec.name: spec},
            {spec.name: K8S_READONLY_LEGACY_TOOL_NAMES[spec.name]},
        ),
    )

    result = await runner.tick(
        tenant,
        state.task_id,
        ToolRuntimeContext(
            tenant_id=state.tenant_id,
            environment_id=state.environment_id,
            allowed_capabilities=frozenset({"k8s.workload.read"}),
            allowed_namespaces=frozenset({"payment"}),
        ),
        (spec,),
    )

    assert result.status is OpsTaskStatus.SUCCEEDED
    assert [event.event_type for event in events.list_after(tenant, state.task_id)][
        -1
    ] == "task.completed"


@pytest.mark.asyncio
async def test_runner_persists_queued_zero_budget_as_failed() -> None:
    state = replace(_state(), budget=TaskBudget(0, 6000, 30000))
    cache = InMemoryCache(namespace="policy-workflow-zero-budget")
    tenant = _tenant()
    tasks = TaskStateRepository(cache)
    tasks.save(tenant, state)
    events = TaskEventRepository(cache)
    runner = WorkflowRunner(
        tasks,
        events,
        EvidenceStore(cache),
        ContextManager(),
        PolicyAgent(),
        ToolRuntime(ToolRegistry(), {}, {}),
    )

    result = await runner.tick(
        tenant,
        state.task_id,
        ToolRuntimeContext(
            tenant_id=state.tenant_id,
            environment_id=state.environment_id,
            allowed_namespaces=frozenset({"payment"}),
        ),
        (),
    )

    assert result.status is OpsTaskStatus.FAILED
    assert result.phase is OpsTaskPhase.REPORT
    persisted_events = events.list_after(tenant, state.task_id)
    assert [event.event_type for event in persisted_events] == [
        "task.started",
        "task.failed",
    ]
    assert persisted_events[-1].data["error_code"] == "TASK_STEP_BUDGET_EXHAUSTED"


@pytest.mark.asyncio
async def test_rules_only_escalates_when_crashloop_is_not_observed() -> None:
    workflow = CrashLoopDiagnosisWorkflow()
    state = replace(
        _state(),
        facts=(
            {
                "action": "k8s.pod.list",
                "evidence_ids": ["evidence-pods"],
                "data_origin": "live",
                "crashloop_pods": [],
            },
        ),
    )
    cache = InMemoryCache(namespace="policy-workflow-rules-escalation")
    tenant = _tenant()
    tasks = TaskStateRepository(cache)
    tasks.save(tenant, state)
    events = TaskEventRepository(cache)
    specs = workflow.available_tools()
    runner = WorkflowRunner(
        tasks,
        events,
        EvidenceStore(cache),
        ContextManager(),
        PolicyAgent(fallback=workflow.rules_only_decision),
        ToolRuntime(
            ToolRegistry(),
            {spec.name: spec for spec in specs},
            K8S_READONLY_LEGACY_TOOL_NAMES,
        ),
        workflow,
    )

    result = await runner.tick(
        tenant,
        state.task_id,
        ToolRuntimeContext(
            tenant_id=state.tenant_id,
            environment_id=state.environment_id,
            allowed_capabilities=workflow.required_capabilities,
            allowed_namespaces=frozenset({"payment"}),
        ),
        specs,
    )

    assert result.status is OpsTaskStatus.FAILED
    assert events.list_after(tenant, state.task_id)[-1].data["error_code"] == (
        "CRASHLOOP_NOT_OBSERVED"
    )
