"""Deterministic, evidence-bound PodPending workflow policy."""

from __future__ import annotations

from collections.abc import Mapping

from athena.agent.context.manager import DecisionContext
from athena.agent.policy.agent import PolicyDecisionError
from athena.agent.policy.contracts import (
    ActionDecision,
    ToolResultV2,
    ToolSpecV2,
    ToolStatus,
)
from athena.agent.workflow.state import OpsTaskState
from athena.tools.cloud.k8s.tools import K8S_READONLY_TOOL_SPECS
from athena.types import JSONValue


class PodPendingEscalation(PolicyDecisionError):
    """Readonly evidence could not prove a bounded PodPending diagnosis."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class PodPendingDiagnosisWorkflow:
    """Diagnose Pending pods with workload and event evidence only."""

    required_capabilities = frozenset({"k8s.workload.read", "k8s.events.read"})

    def available_tools(self) -> tuple[ToolSpecV2, ...]:
        return tuple(
            spec
            for spec in K8S_READONLY_TOOL_SPECS
            if set(spec.required_capabilities).issubset(self.required_capabilities)
        )

    def rules_only_decision(self, context: DecisionContext) -> ActionDecision:
        namespace = self._namespace(context)
        raw_facts = context.payload.get("verified_facts")
        if not isinstance(raw_facts, list):
            raw_facts = []
        facts = tuple(item for item in raw_facts if isinstance(item, Mapping))

        pod_list = self._latest_fact(facts, "k8s.pod.list")
        if pod_list is None:
            return ActionDecision(
                action="k8s.pod.list",
                arguments={"namespace": namespace},
                reason_code="RULES_COLLECT_PENDING_PODS",
                confidence=1.0,
            )

        pending_pods = pod_list.get("pending_pods")
        if not isinstance(pending_pods, list) or not pending_pods:
            raise PodPendingEscalation(
                "POD_PENDING_NOT_OBSERVED",
                "no Pending pod was observed in readonly evidence",
            )
        pod_name = str(pending_pods[0])

        if self._latest_fact(facts, "k8s.pod.get") is None:
            return ActionDecision(
                action="k8s.pod.get",
                arguments={"namespace": namespace, "name": pod_name},
                reason_code="RULES_INSPECT_PENDING_POD",
                confidence=1.0,
            )
        if self._latest_fact(facts, "k8s.events.list") is None:
            return ActionDecision(
                action="k8s.events.list",
                arguments={"namespace": namespace, "pod_name": pod_name},
                reason_code="RULES_COLLECT_PENDING_EVENTS",
                confidence=1.0,
            )
        raise PodPendingEscalation(
            "WORKFLOW_ESCALATION_REQUIRED",
            "readonly evidence collection ended without a terminal diagnosis",
        )

    def fact_from_result(
        self, decision: ActionDecision, result: ToolResultV2
    ) -> dict[str, JSONValue] | None:
        if result.status is not ToolStatus.SUCCEEDED:
            return None
        fact: dict[str, JSONValue] = {
            "action": decision.action,
            "evidence_ids": list(result.evidence_refs),
        }
        data = result.data
        origin = data.get("data_origin") if isinstance(data, Mapping) else None
        if isinstance(origin, str):
            fact["data_origin"] = origin

        if decision.action == "k8s.pod.list":
            fact["pending_pods"] = sorted(
                str(item.get("name"))
                for item in self._items(data)
                if str(item.get("status", "")).lower() == "pending"
                and str(item.get("name", "")).strip()
            )
        elif decision.action == "k8s.pod.get":
            item = data.get("item") if isinstance(data, Mapping) else None
            if not isinstance(item, Mapping):
                item = data if isinstance(data, Mapping) else {}
            fact["pending_observed"] = str(item.get("status", "")).lower() == "pending"
            fact["pod"] = str(item.get("name", decision.arguments.get("name", "")))
        elif decision.action == "k8s.events.list":
            reasons = sorted(
                {
                    str(item.get("reason"))
                    for item in self._items(data)
                    if str(item.get("reason", "")).strip()
                }
            )
            fact["event_reasons"] = reasons
            fact["root_cause"] = self._root_cause_from_events(reasons)
        return fact

    def terminal_error(self, state: OpsTaskState) -> str | None:
        expected_origin = state.environment_mode.value
        observed_origins = {
            str(fact.get("data_origin"))
            for fact in state.facts
            if isinstance(fact.get("data_origin"), str)
        }
        if any(origin != expected_origin for origin in observed_origins):
            return "EVIDENCE_ORIGIN_MISMATCH"
        pod_details = [
            fact for fact in state.facts if fact.get("action") == "k8s.pod.get"
        ]
        if pod_details and pod_details[-1].get("pending_observed") is False:
            return "EVIDENCE_CONFLICT"
        event_facts = [
            fact for fact in state.facts if fact.get("action") == "k8s.events.list"
        ]
        if not event_facts:
            return "WORKFLOW_EVIDENCE_REQUIRED"
        latest = event_facts[-1]
        if not latest.get("root_cause"):
            return "WORKFLOW_ESCALATION_REQUIRED"
        if not latest.get("evidence_ids"):
            return "WORKFLOW_EVIDENCE_REQUIRED"
        return None

    @staticmethod
    def is_complete(state: OpsTaskState) -> bool:
        return any(fact.get("action") == "k8s.events.list" for fact in state.facts)

    @staticmethod
    def _namespace(context: DecisionContext) -> str:
        task = context.payload.get("task")
        scope = task.get("scope") if isinstance(task, Mapping) else None
        namespace = scope.get("namespace") if isinstance(scope, Mapping) else None
        if not isinstance(namespace, str) or not namespace.strip():
            raise PodPendingEscalation(
                "ENV_SCOPE_INVALID", "task namespace is unavailable"
            )
        return namespace

    @staticmethod
    def _latest_fact(
        facts: tuple[Mapping[str, object], ...], action: str
    ) -> Mapping[str, object] | None:
        return next(
            (fact for fact in reversed(facts) if fact.get("action") == action), None
        )

    @staticmethod
    def _items(data: JSONValue | None) -> tuple[Mapping[str, object], ...]:
        raw_items = data.get("items") if isinstance(data, Mapping) else data
        if not isinstance(raw_items, list):
            return ()
        return tuple(item for item in raw_items if isinstance(item, Mapping))

    @staticmethod
    def _root_cause_from_events(reasons: list[str]) -> str | None:
        normalized = " ".join(reasons).lower()
        if "failedscheduling" in normalized or "unschedulable" in normalized:
            return "Scheduler could not place the pod"
        if "imagepullbackoff" in normalized or "errimagepull" in normalized:
            return "Image pull failure prevented the pod from starting"
        if "failedmount" in normalized:
            return "Volume mount failure prevented the pod from starting"
        return None
