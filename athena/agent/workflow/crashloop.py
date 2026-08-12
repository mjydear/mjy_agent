"""Deterministic, evidence-bound CrashLoopBackOff workflow policy."""

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


class CrashLoopEscalation(PolicyDecisionError):
    """Rules-only collection cannot produce an evidence-backed diagnosis."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class CrashLoopDiagnosisWorkflow:
    """Expose and sequence only the readonly actions required for diagnosis."""

    required_capabilities = frozenset(
        {"k8s.workload.read", "k8s.events.read", "k8s.logs.read"}
    )

    def available_tools(self) -> tuple[ToolSpecV2, ...]:
        return K8S_READONLY_TOOL_SPECS

    def rules_only_decision(self, context: DecisionContext) -> ActionDecision:
        """Choose the next readonly action from persisted, verified facts only."""
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
                reason_code="RULES_COLLECT_PODS",
                confidence=1.0,
            )

        crashloop_pods = pod_list.get("crashloop_pods")
        if not isinstance(crashloop_pods, list) or not crashloop_pods:
            raise CrashLoopEscalation(
                "CRASHLOOP_NOT_OBSERVED",
                "no CrashLoopBackOff pod was observed in readonly evidence",
            )
        pod_name = str(crashloop_pods[0])

        if self._latest_fact(facts, "k8s.pod.get") is None:
            return ActionDecision(
                action="k8s.pod.get",
                arguments={"namespace": namespace, "name": pod_name},
                reason_code="RULES_INSPECT_CRASHLOOP_POD",
                confidence=1.0,
            )
        if self._latest_fact(facts, "k8s.events.list") is None:
            return ActionDecision(
                action="k8s.events.list",
                arguments={"namespace": namespace, "pod_name": pod_name},
                reason_code="RULES_COLLECT_POD_EVENTS",
                confidence=1.0,
            )
        if self._latest_fact(facts, "k8s.logs.read") is None:
            return ActionDecision(
                action="k8s.logs.read",
                arguments={"namespace": namespace, "name": pod_name, "tail_lines": 80},
                reason_code="RULES_COLLECT_POD_LOGS",
                confidence=1.0,
            )
        raise CrashLoopEscalation(
            "WORKFLOW_ESCALATION_REQUIRED",
            "readonly evidence collection ended without a terminal diagnosis",
        )

    def fact_from_result(
        self, decision: ActionDecision, result: ToolResultV2
    ) -> dict[str, JSONValue] | None:
        """Derive bounded facts from the actual tool result, never from model text."""
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
            items = self._items(data)
            crashloop_pods = sorted(
                str(item.get("name"))
                for item in items
                if "crashloop" in str(item.get("status", "")).lower()
                and str(item.get("name", "")).strip()
            )
            fact["crashloop_pods"] = crashloop_pods
        elif decision.action == "k8s.pod.get":
            item = data.get("item") if isinstance(data, Mapping) else None
            if not isinstance(item, Mapping):
                item = data if isinstance(data, Mapping) else {}
            fact["crashloop_observed"] = (
                "crashloop" in str(item.get("status", "")).lower()
            )
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
            fact["backoff_observed"] = any(
                "backoff" in reason.lower() for reason in reasons
            )
        elif decision.action == "k8s.logs.read":
            content = data.get("content") if isinstance(data, Mapping) else data
            root_cause = self._root_cause_from_log(str(content or ""))
            if root_cause is not None and result.evidence_refs:
                fact["root_cause"] = root_cause
                fact["root_cause_evidence_ids"] = list(result.evidence_refs)
        return fact

    def terminal_error(self, state: OpsTaskState) -> str | None:
        """Require a direct log signal and Evidence reference before success."""
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
        if pod_details and pod_details[-1].get("crashloop_observed") is False:
            return "EVIDENCE_CONFLICT"
        event_facts = [
            fact for fact in state.facts if fact.get("action") == "k8s.events.list"
        ]
        if event_facts and event_facts[-1].get("backoff_observed") is False:
            return "EVIDENCE_CONFLICT"
        root_cause_facts = [
            fact
            for fact in state.facts
            if fact.get("action") == "k8s.logs.read" and fact.get("root_cause")
        ]
        if not root_cause_facts:
            return "WORKFLOW_ESCALATION_REQUIRED"
        latest = root_cause_facts[-1]
        evidence_ids = latest.get("root_cause_evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            return "WORKFLOW_EVIDENCE_REQUIRED"
        if latest.get("data_origin") != expected_origin:
            return "EVIDENCE_ORIGIN_MISMATCH"
        return None

    @staticmethod
    def is_complete(state: OpsTaskState) -> bool:
        """Collection is terminal once the bounded log observation is present."""
        return any(fact.get("action") == "k8s.logs.read" for fact in state.facts)

    @staticmethod
    def _namespace(context: DecisionContext) -> str:
        task = context.payload.get("task")
        scope = task.get("scope") if isinstance(task, Mapping) else None
        namespace = scope.get("namespace") if isinstance(scope, Mapping) else None
        if not isinstance(namespace, str) or not namespace.strip():
            raise CrashLoopEscalation(
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
    def _root_cause_from_log(content: str) -> str | None:
        normalized = content.lower()
        if "database" in normalized and any(
            marker in normalized
            for marker in ("failed to connect", "connection refused")
        ):
            return "Database connection failure reported by the container log"
        direct_signals = (
            (
                ("connection refused",),
                "Connection refusal reported by the container log",
            ),
            (
                ("oomkilled", "out of memory"),
                "Memory exhaustion reported by the container log",
            ),
            (
                ("permission denied",),
                "Permission failure reported by the container log",
            ),
            (("no such file",), "Missing file reported by the container log"),
        )
        for markers, summary in direct_signals:
            if any(marker in normalized for marker in markers):
                return summary
        return None
