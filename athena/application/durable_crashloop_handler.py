"""Readonly CrashLoop task handler used by the durable worker process."""

from __future__ import annotations

import asyncio

from athena.api.repositories import EvidenceRepository, PersistedTask
from athena.application.durable_worker import WorkerOutcome
from athena.config import AthenaSettings
from athena.tools.cloud.k8s.client import K8sReadOnlyClient
from athena.tools.cloud.k8s.diagnose import K8sReadOnlyDiagnoser
from athena.tools.cloud.prometheus import PrometheusQueryClient


_WORKFLOW_FINDING_MARKERS: dict[str, tuple[str, ...]] = {
    "crashloop": ("crashloop", "frequentrestart", "restart"),
    "pod_pending": ("pod pending", "pending", "failedscheduling", "unschedulable"),
    "image_pull": ("imagepullbackoff", "errimagepull", "image pull"),
    "resource_pressure": (
        "cpu / memory",
        "resource",
        "pressure",
        "insufficient",
        "oom",
        "scheduling",
    ),
}


class DurableCrashLoopHandler:
    """Run one bounded readonly Pod diagnosis.

    The legacy class name is retained for import compatibility. ``workflow_type``
    scopes the returned report and Evidence source while keeping the same
    fail-closed, readonly client stack.
    """

    def __init__(
        self,
        settings: AthenaSettings,
        evidence: EvidenceRepository | None = None,
        *,
        workflow_type: str = "crashloop",
    ) -> None:
        if workflow_type not in _WORKFLOW_FINDING_MARKERS:
            raise ValueError(f"unsupported readonly diagnosis workflow: {workflow_type}")
        self.workflow_type = workflow_type
        k8s = settings.ops.kubernetes
        prometheus = settings.ops.prometheus
        self._client = K8sReadOnlyClient(
            mode=settings.ops.mode,
            kubeconfig=k8s.kubeconfig,
            context=k8s.context,
            namespace_allowlist=k8s.namespace_allowlist,
            timeout=k8s.timeout,
            fallback_policy=(
                "fail_closed" if settings.ops.mode == "real" else k8s.fallback_policy
            ),
        )
        self._diagnoser = K8sReadOnlyDiagnoser(
            self._client,
            PrometheusQueryClient(
                enabled=prometheus.enabled,
                base_url=prometheus.base_url,
                timeout_seconds=prometheus.timeout_seconds,
                fallback_policy=(
                    "fail_closed"
                    if settings.ops.mode == "real" and prometheus.enabled
                    else prometheus.fallback_policy
                ),
            ),
        )
        self._evidence = evidence

    async def __call__(self, task: PersistedTask) -> WorkerOutcome:
        namespace = task.scope.get("namespace")
        if not isinstance(namespace, str) or not namespace.strip():
            return WorkerOutcome(
                state={"error_code": "ENV_SCOPE_INVALID"},
                phase="report",
                status="failed",
                event_type="task.failed",
            )
        report = await asyncio.to_thread(
            self._diagnoser.build_report,
            namespace,
            self.workflow_type == "crashloop",
        )
        report_data = self._scoped_report(report.to_dict())
        evidence_ids: list[str] = []
        if self._evidence is not None:
            evidence = await self._evidence.create(
                tenant_id=task.tenant_id,
                task_id=task.task_id,
                evidence_type="resource_snapshot",
                source=f"k8s.{self.workflow_type}.diagnose",
                data_origin=self._client.last_data_origin,
                summary=(
                    f"Readonly {self.workflow_type} diagnosis for namespace {namespace}"
                ),
                content=report_data,
            )
            evidence_ids.append(evidence.evidence_id)
        root_causes = [
            finding
            for finding in report_data.get("findings", [])
            if isinstance(finding, dict)
            and str(finding.get("severity", "")) in {"critical", "high", "medium"}
        ]
        return WorkerOutcome(
            state={
                "readonly_report": report_data,
                "root_causes": root_causes,
                "data_origin": self._client.last_data_origin,
                "evidence_ids": evidence_ids,
            },
            phase="report",
            status="succeeded",
            event_type="task.completed",
        )

    def _scoped_report(self, report: dict[str, object]) -> dict[str, object]:
        """Keep only findings and evidence relevant to the selected workflow."""
        scoped = dict(report)
        raw_findings = report.get("findings", [])
        findings = [
            finding
            for finding in raw_findings
            if isinstance(finding, dict) and self._matches_workflow(finding)
        ]
        scoped["findings"] = findings
        scoped["workflow_type"] = self.workflow_type

        actions: list[str] = []
        raw_evidence: list[str] = []
        for finding in findings:
            actions_value = finding.get("recommended_actions", [])
            for action in actions_value if isinstance(actions_value, list) else []:
                if isinstance(action, str) and action not in actions:
                    actions.append(action)
            evidence_value = finding.get("evidence", [])
            for item in evidence_value if isinstance(evidence_value, list) else []:
                if isinstance(item, str) and item not in raw_evidence:
                    raw_evidence.append(item)
        scoped["actions"] = actions
        scoped["raw_evidence"] = raw_evidence

        metrics = report.get("metrics")
        scoped_metrics = dict(metrics) if isinstance(metrics, dict) else {}
        scoped_metrics["finding_count"] = len(findings)
        scoped_metrics["workflow_type"] = self.workflow_type
        scoped["metrics"] = scoped_metrics
        namespace = str(report.get("namespace", ""))
        scoped["summary"] = (
            f"Readonly {self.workflow_type} diagnosis for namespace {namespace}: "
            f"{len(findings)} relevant finding(s)"
        )
        return scoped

    def _matches_workflow(self, finding: dict[str, object]) -> bool:
        markers = _WORKFLOW_FINDING_MARKERS[self.workflow_type]
        searchable = " ".join(
            str(finding.get(key, ""))
            for key in ("symptom", "evidence", "probable_causes", "recommended_actions")
        ).lower()
        return any(marker in searchable for marker in markers)
